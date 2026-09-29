"""Focused fake-based tests for production process lifecycle boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.processes.worker as worker_module
from telegram_userbot.adapters.persistence.records import JobRecord, JobState, OutboxRecord
from telegram_userbot.adapters.queue.redis import RedisRuntimeError
from telegram_userbot.platform.health.disk import DiskAdmission, disk_admission
from telegram_userbot.processes.worker import (
    DurableJobConsumer,
    DurableOutboxPublisher,
    WorkerProcessError,
    wake_durable_job,
)
from telegram_userbot.processes.worker_executors import (
    JobExecutionContext,
    JobExecutionError,
    WorkerExecutorRegistry,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
OWNER_ID = UUID("01900000-0000-7000-8000-000000000001")
JOB_ID = UUID("01900000-0000-7000-8000-000000000002")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000003")


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, state: object) -> None:
        self.state = state

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _Sessions:
    def __init__(self, state: object) -> None:
        self._state = state
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return _Session(self._state)


def _leased_job(*, attempt_count: int = 1) -> JobRecord:
    return JobRecord(
        id=JOB_ID,
        account_id=ACCOUNT_ID,
        queue_name="worker",
        job_type="memory.refresh_completed_turn",
        state=JobState.LEASED,
        priority=0,
        payload={},
        attempt_count=attempt_count,
        max_attempts=5,
        available_at=NOW,
        lease_owner=OWNER_ID,
        lease_expires_at=NOW + timedelta(seconds=60),
        version=2,
        fencing_token=1,
        dispatch_generation=1,
    )


@dataclass(slots=True)
class _JobState:
    claimed: JobRecord | None
    succeed_result: bool = True
    fail_result: JobState | None = JobState.RETRY_WAIT
    claim_calls: list[dict[str, object]] = field(default_factory=list)
    succeed_calls: list[dict[str, object]] = field(default_factory=list)
    fail_calls: list[dict[str, object]] = field(default_factory=list)


class _JobRepository:
    def __init__(self, session: _Session) -> None:
        self._state = cast(_JobState, session.state)

    async def claim_notification(self, **kwargs: object) -> JobRecord | None:
        self._state.claim_calls.append(kwargs)
        return self._state.claimed

    async def renew(self, **_kwargs: object) -> bool:
        return True

    async def succeed(self, **kwargs: object) -> bool:
        self._state.succeed_calls.append(kwargs)
        return self._state.succeed_result

    async def fail_or_retry(self, **kwargs: object) -> JobState | None:
        self._state.fail_calls.append(kwargs)
        return self._state.fail_result


class _Registry:
    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure
        self.contexts: list[JobExecutionContext] = []

    async def execute(self, context: JobExecutionContext) -> None:
        self.contexts.append(context)
        if self._failure is not None:
            raise self._failure


def _consumer(
    state: _JobState,
    registry: _Registry,
    *,
    admission: DiskAdmission | None = None,
) -> DurableJobConsumer:
    return DurableJobConsumer(
        sessions=cast(async_sessionmaker[AsyncSession], _Sessions(state)),
        registry=cast(WorkerExecutorRegistry, registry),
        owner=OWNER_ID,
        cpu_heavy=asyncio.Semaphore(1),
        now=lambda: NOW,
        jitter=lambda: 0.25,
        admission=lambda: (
            admission or disk_admission(total_bytes=100 * 1024**3, available_bytes=50 * 1024**3)
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_consumer_claims_and_completes_only_its_fenced_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _JobState(claimed=_leased_job())
    registry = _Registry()
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _JobRepository)

    await _consumer(state, registry).consume(job_id=str(JOB_ID), dispatch_generation=1)

    assert state.claim_calls == [
        {
            "job_id": JOB_ID,
            "dispatch_generation": 1,
            "owner": OWNER_ID,
            "now": NOW,
            "allow_work": True,
            "allow_proactive": True,
        }
    ]
    assert [context.job.id for context in registry.contexts] == [JOB_ID]
    assert state.succeed_calls == [{"job": _leased_job(), "owner": OWNER_ID, "now": NOW}]
    assert state.fail_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_retryable"),
    [
        (JobExecutionError("WORKER_INPUT_INVALID", retryable=False), "WORKER_INPUT_INVALID", False),
        (RuntimeError("synthetic executor fault"), "WORKER_EXECUTION_FAILED", True),
    ],
)
async def test_durable_consumer_persists_executor_failure_without_declaring_success(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    state = _JobState(claimed=_leased_job())
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _JobRepository)

    await _consumer(state, _Registry(failure)).consume(job_id=str(JOB_ID), dispatch_generation=1)

    assert state.succeed_calls == []
    assert len(state.fail_calls) == 1
    failure_call = state.fail_calls[0]
    assert failure_call["job"] == _leased_job()
    assert failure_call["owner"] == OWNER_ID
    assert failure_call["now"] == NOW
    assert failure_call["error_code"] == expected_code
    assert failure_call["retryable"] is expected_retryable
    assert failure_call["retry_delay"] == timedelta(seconds=1.25)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_consumer_rejects_invalid_notification_before_database_access() -> None:
    state = _JobState(claimed=_leased_job())
    consumer = _consumer(state, _Registry())

    with pytest.raises(JobExecutionError, match="WORKER_NOTIFICATION_INVALID"):
        await consumer.consume(job_id="not-a-uuid", dispatch_generation=1)
    with pytest.raises(JobExecutionError, match="WORKER_NOTIFICATION_INVALID"):
        await consumer.consume(job_id=str(JOB_ID), dispatch_generation=0)

    assert state.claim_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_consumer_reports_lost_completion_fence_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _JobState(claimed=_leased_job(), succeed_result=False)
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _JobRepository)

    with pytest.raises(JobExecutionError, match="WORKER_JOB_FENCE_LOST") as captured:
        await _consumer(state, _Registry()).consume(job_id=str(JOB_ID), dispatch_generation=1)

    assert captured.value.retryable
    assert len(state.succeed_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_arq_wakeup_requires_real_consumer_and_forwards_only_typed_arguments() -> None:
    with pytest.raises(WorkerProcessError, match="WORKER_ARQ_CONTEXT_INVALID"):
        await wake_durable_job({}, str(JOB_ID), 1)

    consumer = cast(Any, object.__new__(DurableJobConsumer))
    calls: list[tuple[str, int]] = []

    async def consume(*, job_id: str, dispatch_generation: int) -> None:
        calls.append((job_id, dispatch_generation))

    consumer.consume = consume
    await wake_durable_job({"durable_consumer": consumer}, str(JOB_ID), 2)

    assert calls == [(str(JOB_ID), 2)]


@dataclass(slots=True)
class _WakeupState:
    records: tuple[OutboxRecord, ...]
    due_calls: list[dict[str, object]] = field(default_factory=list)
    published: list[int] = field(default_factory=list)
    failures: list[tuple[int, str]] = field(default_factory=list)


class _WakeupRepository:
    def __init__(self, session: _Session) -> None:
        self._state = cast(_WakeupState, session.state)

    async def due_wakeups(self, **kwargs: object) -> tuple[OutboxRecord, ...]:
        self._state.due_calls.append(kwargs)
        return self._state.records

    async def record_failure(self, *, outbox_id: int, error_code: str) -> bool:
        self._state.failures.append((outbox_id, error_code))
        return True

    async def mark_published(self, *, outbox_id: int, now: datetime) -> bool:
        assert now == NOW
        self._state.published.append(outbox_id)
        return True


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def publish(self, record: OutboxRecord) -> None:
        self.calls.append(record.id)
        if record.id == 2:
            raise TypeError("synthetic invalid durable payload")
        if record.id == 3:
            raise RedisRuntimeError("synthetic Redis fault")


def _outbox_record(outbox_id: int) -> OutboxRecord:
    return OutboxRecord(
        id=outbox_id,
        topic="durable_job.available",
        aggregate_type="background_job",
        aggregate_id=str(JOB_ID),
        aggregate_version=1,
        payload={"job_id": str(JOB_ID), "dispatch_generation": 1},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_outbox_isolates_invalid_and_transient_wakeups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WakeupState((_outbox_record(1), _outbox_record(2), _outbox_record(3)))
    notifier = _Notifier()
    monkeypatch.setattr(worker_module, "WorkerOutboxRepository", _WakeupRepository)
    publisher = DurableOutboxPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _Sessions(state)),
        notifier=cast(Any, notifier),
        now=lambda: NOW,
    )

    assert await publisher.publish_once() == 1
    assert notifier.calls == [1, 2, 3]
    assert state.published == [1]
    assert state.failures == [
        (2, "DURABLE_JOB_INVALID"),
        (3, "REDIS_JOB_ENQUEUE_FAILED"),
    ]


@pytest.mark.unit
def test_executor_registry_rejects_malformed_or_non_callable_entries() -> None:
    async def executor(_context: JobExecutionContext) -> None:
        return None

    with pytest.raises(ValueError, match="worker executor registry is invalid"):
        WorkerExecutorRegistry(cast(Any, {"memory": executor}))
    with pytest.raises(ValueError, match="worker executor registry is invalid"):
        WorkerExecutorRegistry({"memory.refresh": cast(Any, object())})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_registry_uses_exact_dispatch_and_fails_closed_for_unknown_jobs() -> None:
    executed: list[UUID] = []

    async def executor(context: JobExecutionContext) -> None:
        executed.append(context.job.id)

    registry = WorkerExecutorRegistry({"memory.refresh_completed_turn": executor})
    context = JobExecutionContext(
        job=_leased_job(),
        sessions=cast(async_sessionmaker[AsyncSession], _Sessions(object())),
        lease_lost=asyncio.Event(),
        cpu_heavy=asyncio.Semaphore(1),
    )

    await registry.execute(context)

    assert registry.job_types == {"memory.refresh_completed_turn"}
    assert executed == [JOB_ID]
    with pytest.raises(JobExecutionError, match="WORKER_EXECUTOR_UNAVAILABLE") as captured:
        await registry.execute(
            replace(context, job=replace(context.job, job_type="memory.unknown"))
        )
    assert not captured.value.retryable
