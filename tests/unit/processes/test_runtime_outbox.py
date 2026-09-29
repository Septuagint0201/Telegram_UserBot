from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.records import OutboxRecord
from telegram_userbot.adapters.queue.redis import (
    RedisRuntimeError,
    RuntimeGenerationMarker,
)
from telegram_userbot.processes import runtime_outbox
from telegram_userbot.processes.runtime_outbox import (
    RuntimeMarkerObserver,
    RuntimeOutboxMarkerPublisher,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID(int=1)
COMMAND_ID = UUID(int=2)


class _Context:
    async def __aenter__(self) -> _Context:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@dataclass(slots=True)
class _OutboxState:
    records: list[OutboxRecord]
    published: set[int] = field(default_factory=set)
    failures: list[tuple[int, str]] = field(default_factory=list)
    factory_calls: int = 0


class _Session:
    def __init__(self, state: _OutboxState) -> None:
        self.state = state

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> _Context:
        return _Context()


class _SessionFactory:
    def __init__(self, state: _OutboxState) -> None:
        self.state = state

    def __call__(self) -> _Session:
        self.state.factory_calls += 1
        return _Session(self.state)


class _FakeOutboxRepository:
    def __init__(self, session: _Session) -> None:
        self._state = session.state

    async def claim_batch(self, *, limit: int, topics: frozenset[str]) -> tuple[OutboxRecord, ...]:
        return tuple(
            record
            for record in self._state.records
            if record.id not in self._state.published and record.topic in topics
        )[:limit]

    async def mark_published(self, *, outbox_id: int, now: datetime) -> bool:
        del now
        if outbox_id in self._state.published:
            return False
        self._state.published.add(outbox_id)
        return True

    async def record_failure(self, *, outbox_id: int, error_code: str) -> bool:
        self._state.failures.append((outbox_id, error_code))
        return True


class _PublisherRedis:
    def __init__(self, *, fail_ids: set[int] | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.published: list[RuntimeGenerationMarker] = []

    async def publish_generation_marker(self, record: OutboxRecord) -> RuntimeGenerationMarker:
        if record.id in self.fail_ids:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_WRITE_FAILED")
        marker = RuntimeGenerationMarker.from_outbox(record)
        self.published.append(marker)
        return marker


def _record(outbox_id: int, *, topic: str = "control.command.requested") -> OutboxRecord:
    return OutboxRecord(
        id=outbox_id,
        topic=topic,
        aggregate_type="control_command",
        aggregate_id=str(COMMAND_ID),
        aggregate_version=1,
        payload={"command_id": str(COMMAND_ID)},
        account_id=ACCOUNT_ID,
    )


@pytest.mark.unit
async def test_publisher_is_owner_gated_and_marks_only_after_marker_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _OutboxState([_record(1), _record(2)])
    monkeypatch.setattr(runtime_outbox, "OutboxRepository", _FakeOutboxRepository)
    redis = _PublisherRedis()
    publisher = RuntimeOutboxMarkerPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory(state)),
        redis=cast(Any, redis),
        now=lambda: NOW,
        owns_relay=lambda: True,
    )

    assert await publisher.publish_once() == 2
    assert state.published == {1, 2}
    assert [marker.outbox_id for marker in redis.published] == [1, 2]

    blocked = RuntimeOutboxMarkerPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory(state)),
        redis=cast(Any, redis),
        owns_relay=lambda: False,
    )
    factory_calls = state.factory_calls
    assert await blocked.publish_once() == 0
    assert state.factory_calls == factory_calls


@pytest.mark.unit
async def test_publisher_keeps_unpublished_row_on_redis_failure_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _OutboxState([_record(3)])
    monkeypatch.setattr(runtime_outbox, "OutboxRepository", _FakeOutboxRepository)
    redis = _PublisherRedis(fail_ids={3})
    publisher = RuntimeOutboxMarkerPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory(state)),
        redis=cast(Any, redis),
        owns_relay=lambda: True,
    )

    assert await publisher.publish_once() == 0
    assert state.published == set()
    assert state.failures == [(3, "REDIS_RUNTIME_MARKER_WRITE_FAILED")]

    redis.fail_ids.clear()
    publisher._retry_after[3] = 0.0
    assert await publisher.publish_once() == 1
    assert state.published == {3}


@dataclass(slots=True)
class _ObserverRedis:
    markers: dict[str, RuntimeGenerationMarker]
    failed_topics: set[str] = field(default_factory=set)

    async def read_generation_marker(self, topic: str) -> RuntimeGenerationMarker | None:
        if topic in self.failed_topics:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_READ_FAILED")
        return self.markers.get(topic)


def _marker(outbox_id: int, topic: str) -> RuntimeGenerationMarker:
    return RuntimeGenerationMarker(
        outbox_id=outbox_id,
        topic=topic,
        aggregate_type="control_command",
        aggregate_id=COMMAND_ID,
        aggregate_version=1,
        payload={"command_id": str(COMMAND_ID)},
        payload_schema_version=1,
        account_id=ACCOUNT_ID,
    )


@pytest.mark.unit
async def test_observer_isolates_topics_and_does_not_advance_after_handler_failure() -> None:
    topic_a = "control.command.requested"
    topic_b = "control.command.completed"
    redis = _ObserverRedis({topic_a: _marker(10, topic_a), topic_b: _marker(20, topic_b)})
    handled: list[str] = []
    fail_a = True

    async def handle(marker: RuntimeGenerationMarker) -> None:
        nonlocal fail_a
        if marker.topic == topic_a and fail_a:
            fail_a = False
            raise RuntimeError("synthetic handler failure")
        handled.append(marker.topic)

    async def compensate() -> None:
        return None

    observer = RuntimeMarkerObserver(
        redis=cast(Any, redis),
        topics=(topic_a, topic_b),
        handler=handle,
        compensate=compensate,
    )

    assert await observer.observe_once() == 1
    assert handled == [topic_b]
    assert observer._last_seen == {topic_b: 20}

    assert await observer.observe_once() == 1
    assert handled == [topic_b, topic_a]
    assert observer._last_seen == {topic_a: 10, topic_b: 20}


@pytest.mark.unit
async def test_observer_read_failure_does_not_starve_other_topic() -> None:
    failed_topic = "control.command.requested"
    healthy_topic = "control.command.completed"
    redis = _ObserverRedis(
        {healthy_topic: _marker(22, healthy_topic)}, failed_topics={failed_topic}
    )
    handled: list[int] = []

    async def handle(marker: RuntimeGenerationMarker) -> None:
        handled.append(marker.outbox_id)

    observer = RuntimeMarkerObserver(
        redis=cast(Any, redis),
        topics=(failed_topic, healthy_topic),
        handler=handle,
        compensate=lambda: asyncio.sleep(0),
    )
    assert await observer.observe_once() == 1
    assert handled == [22]


@pytest.mark.unit
async def test_observer_ready_requires_initial_compensation_and_stop_clears_readiness() -> None:
    compensation_calls = 0

    async def compensate() -> None:
        nonlocal compensation_calls
        compensation_calls += 1

    observer = RuntimeMarkerObserver(
        redis=cast(Any, _ObserverRedis({})),
        topics=("control.command.completed",),
        handler=lambda marker: asyncio.sleep(0),
        compensate=compensate,
        poll_seconds=0.1,
        compensation_seconds=1,
    )
    task = asyncio.create_task(observer.run())
    for _ in range(20):
        if observer.ready:
            break
        await asyncio.sleep(0.01)
    assert observer.ready
    assert compensation_calls == 1

    observer.stop()
    await task
    assert not observer.ready


@pytest.mark.unit
async def test_observer_cancellation_resets_running_state() -> None:
    observer = RuntimeMarkerObserver(
        redis=cast(Any, _ObserverRedis({})),
        topics=("control.command.completed",),
        handler=lambda marker: asyncio.sleep(0),
        compensate=lambda: asyncio.sleep(0),
        poll_seconds=60,
    )
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not observer.ready
