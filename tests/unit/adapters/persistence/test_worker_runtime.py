from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.worker_runtime import (
    DURABLE_JOB_TOPIC,
    WorkerJobRepository,
    WorkerOutboxRepository,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class _Mappings:
    def mappings(self) -> _Mappings:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(())

    def one_or_none(self) -> None:
        return None

    def all(self) -> list[object]:
        return []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_outbox_claim_is_topic_scoped_and_accepts_safe_old_wakeups() -> None:
    session = AsyncMock()
    session.execute.return_value = _Mappings()

    assert await WorkerOutboxRepository(cast(AsyncSession, session)).due_wakeups(now=NOW) == ()

    compiled = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    statement = str(compiled)
    assert "JOIN background_jobs" in statement
    assert "transactional_outbox.topic =" in statement
    assert compiled.params["topic_1"] == DURABLE_JOB_TOPIC
    assert (
        "background_jobs.dispatch_generation >= transactional_outbox.aggregate_version" in statement
    )
    assert "model_configuration" not in statement
    assert "proactive" not in statement


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_outbox_terminal_updates_cannot_touch_another_topic() -> None:
    session = AsyncMock()
    session.execute.return_value = type("Result", (), {"rowcount": 1})()
    repository = WorkerOutboxRepository(cast(AsyncSession, session))

    assert await repository.mark_published(outbox_id=7, now=NOW)
    published = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert published.params["topic_1"] == DURABLE_JOB_TOPIC

    assert await repository.record_failure(
        outbox_id=8,
        error_code="REDIS_JOB_ENQUEUE_FAILED",
    )
    failed = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert failed.params["topic_1"] == DURABLE_JOB_TOPIC


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disk_critical_excludes_explicit_proactive_queue_from_publish_and_claim() -> None:
    session = AsyncMock()
    session.execute.return_value = _Mappings()
    outbox = WorkerOutboxRepository(cast(AsyncSession, session))

    assert (
        await outbox.due_wakeups(
            now=NOW,
            allow_proactive=False,
        )
        == ()
    )
    published = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert "background_jobs.queue_name !=" in str(published)
    assert "proactive" in published.params.values()

    session.reset_mock()
    session.execute.return_value = _Mappings()
    assert (
        await WorkerJobRepository(cast(AsyncSession, session)).claim_notification(
            job_id=UUID(int=1),
            dispatch_generation=1,
            owner=UUID(int=2),
            now=NOW,
            allow_proactive=False,
        )
        is None
    )
    claimed = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert "background_jobs.queue_name !=" in str(claimed)
    assert "proactive" in claimed.params.values()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disk_blocked_does_not_query_or_claim_any_new_work() -> None:
    session = AsyncMock()
    outbox = WorkerOutboxRepository(cast(AsyncSession, session))
    assert await outbox.due_wakeups(now=NOW, allow_work=False) == ()
    session.execute.assert_not_awaited()

    assert (
        await WorkerJobRepository(cast(AsyncSession, session)).claim_notification(
            job_id=UUID(int=1),
            dispatch_generation=1,
            owner=UUID(int=2),
            now=NOW,
            allow_work=False,
        )
        is None
    )
    session.execute.assert_not_awaited()

    assert (
        await WorkerJobRepository(cast(AsyncSession, session)).rebuild_due_notifications(
            now=NOW,
            allow_work=False,
        )
        == 0
    )
    session.execute.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notification_rebuild_excludes_only_explicit_proactive_queue() -> None:
    session = AsyncMock()
    session.execute.return_value = _Mappings()

    assert (
        await WorkerJobRepository(cast(AsyncSession, session)).rebuild_due_notifications(
            now=NOW,
            allow_proactive=False,
        )
        == 0
    )

    rebuilt = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert "background_jobs.queue_name !=" in str(rebuilt)
    assert "proactive" in rebuilt.params.values()
