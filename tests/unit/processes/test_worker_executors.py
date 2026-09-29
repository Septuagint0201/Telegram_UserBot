"""Fake-first coverage of durable memory worker executor boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.processes.worker_executors as executors_module
from telegram_userbot.adapters.persistence.records import JobRecord, JobState
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.processes.worker_executors import (
    ErasureReconciliationExecutor,
    JobExecutionContext,
    JobExecutionError,
    MemoryRefreshExecutor,
    MemoryReviewExecutor,
    build_worker_executor_registry,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000101")
JOB_ID = UUID("01900000-0000-7000-8000-000000000102")
CONVERSATION_ID = UUID("01900000-0000-7000-8000-000000000103")
TURN_ID = UUID("01900000-0000-7000-8000-000000000104")
MESSAGE_ID = UUID("01900000-0000-7000-8000-000000000105")


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Result:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[object, ...] | None:
        return self._row


class _Session:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row
        self.statements: list[object] = []
        self.begin_calls = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        self.begin_calls += 1
        return _Transaction()

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._row)


class _Sessions:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.session = _Session(row)
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return self.session


@dataclass(frozen=True, slots=True)
class _RefreshCase:
    job_type: str
    payload: dict[str, Any]
    row: tuple[object, ...]
    job_kind: str
    event_range: EventRange


def _job(
    *,
    job_type: str,
    payload: dict[str, Any],
    account_id: UUID | None = ACCOUNT_ID,
) -> JobRecord:
    return JobRecord(
        id=JOB_ID,
        account_id=account_id,
        queue_name="worker",
        job_type=job_type,
        state=JobState.LEASED,
        priority=0,
        payload=payload,
        attempt_count=1,
        max_attempts=5,
        available_at=NOW,
        lease_owner=UUID("01900000-0000-7000-8000-000000000106"),
        lease_expires_at=NOW,
        version=1,
        fencing_token=1,
        dispatch_generation=1,
    )


def _context(
    job: JobRecord,
    sessions: _Sessions,
    *,
    lease_lost: bool = False,
) -> JobExecutionContext:
    fence = asyncio.Event()
    if lease_lost:
        fence.set()
    return JobExecutionContext(
        job=job,
        sessions=cast(async_sessionmaker[AsyncSession], sessions),
        lease_lost=fence,
        cpu_heavy=asyncio.Semaphore(1),
    )


def _install_refresh_repository(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, object]],
) -> None:
    class Repository:
        def __init__(self, _session: _Session) -> None:
            return None

        async def refresh_pending_job(self, **kwargs: object) -> UUID:
            calls.append(kwargs)
            return JOB_ID

    monkeypatch.setattr(executors_module, "MemoryRepository", Repository)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "lease_lost", "code", "retryable"),
    [
        (
            _job(
                job_type="memory.refresh_completed_turn",
                payload={"turn_id": str(TURN_ID)},
            ),
            True,
            "WORKER_JOB_FENCE_LOST",
            True,
        ),
        (
            _job(
                job_type="memory.refresh_completed_turn",
                payload={"turn_id": str(TURN_ID)},
                account_id=None,
            ),
            False,
            "WORKER_JOB_SCOPE_INVALID",
            False,
        ),
        (
            _job(job_type="memory.unknown", payload={"turn_id": str(TURN_ID)}),
            False,
            "WORKER_EXECUTOR_MISMATCH",
            False,
        ),
    ],
)
async def test_memory_refresh_rejects_fence_scope_and_executor_mismatch_without_io(
    job: JobRecord,
    lease_lost: bool,
    code: str,
    retryable: bool,
) -> None:
    sessions = _Sessions((CONVERSATION_ID, 10, 12))

    with pytest.raises(JobExecutionError, match=rf"^{code}$") as raised:
        await MemoryRefreshExecutor(now=lambda: NOW)(_context(job, sessions, lease_lost=lease_lost))

    assert raised.value.retryable is retryable
    assert sessions.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_erasure_reconciliation_executor_passes_only_scoped_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions(None)
    calls: list[dict[str, object]] = []

    class Repository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def reconcile_erasure_request(self, **kwargs: object) -> bool:
            calls.append(kwargs)
            return True

    monkeypatch.setattr(executors_module, "MemoryRepository", Repository)
    request_id = UUID("01900000-0000-7000-8000-000000000107")
    await ErasureReconciliationExecutor(erasure_scope_secret=SensitiveValue(b"e" * 32))(
        _context(
            _job(
                job_type="memory.reconcile_erasure",
                payload={"request_id": str(request_id)},
            ),
            sessions,
        )
    )
    assert sessions.calls == 1
    assert calls
    assert calls[0]["account_id"] == ACCOUNT_ID
    assert calls[0]["request_id"] == request_id
    assert calls[0]["erasure_scope_secret"] == b"e" * 32


@pytest.mark.unit
def test_worker_registry_contains_erasure_reconciliation_executor() -> None:
    registry = build_worker_executor_registry(erasure_scope_secret=SensitiveValue(b"e" * 32))
    assert "memory.reconcile_erasure" in registry.job_types


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job",
    [
        _job(job_type="memory.refresh_completed_turn", payload={}),
        _job(
            job_type="memory.refresh_completed_turn",
            payload={"turn_id": str(UUID(int=0))},
        ),
        _job(job_type="memory.refresh_completed_turn", payload={"turn_id": 1}),
        _job(
            job_type="memory.refresh_completed_turn",
            payload={"turn_id": "not-a-uuid"},
        ),
        _job(
            job_type="memory.refresh_completed_turn",
            payload={"turn_id": str(TURN_ID), "extra": "value"},
        ),
        _job(
            job_type="memory.reconcile_message_delete",
            payload={"turn_id": str(TURN_ID)},
        ),
    ],
)
async def test_memory_refresh_rejects_noncanonical_payloads_without_opening_a_session(
    job: JobRecord,
) -> None:
    sessions = _Sessions((CONVERSATION_ID, 10, 12))

    with pytest.raises(JobExecutionError, match=r"^WORKER_JOB_PAYLOAD_INVALID$") as raised:
        await MemoryRefreshExecutor(now=lambda: NOW)(_context(job, sessions))

    assert not raised.value.retryable
    assert sessions.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _RefreshCase(
            job_type="memory.refresh_completed_turn",
            payload={"turn_id": str(TURN_ID)},
            row=(CONVERSATION_ID, 10, 14),
            job_kind="episode",
            event_range=EventRange(10, 14),
        ),
        _RefreshCase(
            job_type="memory.reconcile_message_delete",
            payload={"message_id": str(MESSAGE_ID)},
            row=(CONVERSATION_ID, 20),
            job_kind="reconciliation",
            event_range=EventRange(20, 20),
        ),
    ],
)
async def test_memory_refresh_translates_source_rows_into_durable_memory_jobs(
    monkeypatch: pytest.MonkeyPatch,
    case: _RefreshCase,
) -> None:
    sessions = _Sessions(case.row)
    refresh_calls: list[dict[str, object]] = []
    _install_refresh_repository(monkeypatch, refresh_calls)

    await MemoryRefreshExecutor(now=lambda: NOW)(
        _context(_job(job_type=case.job_type, payload=case.payload), sessions)
    )

    assert sessions.calls == 1
    assert sessions.session.begin_calls == 1
    assert len(sessions.session.statements) == 1
    assert refresh_calls == [
        {
            "account_id": ACCOUNT_ID,
            "conversation_id": CONVERSATION_ID,
            "job_kind": case.job_kind,
            "event_range": case.event_range,
            "estimated_input_tokens": 0,
            "now": NOW,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "row"),
    [
        (_job(job_type="memory.refresh_completed_turn", payload={"turn_id": str(TURN_ID)}), None),
        (
            _job(job_type="memory.refresh_completed_turn", payload={"turn_id": str(TURN_ID)}),
            (CONVERSATION_ID, None, 14),
        ),
        (
            _job(job_type="memory.refresh_completed_turn", payload={"turn_id": str(TURN_ID)}),
            (CONVERSATION_ID, 10, None),
        ),
        (
            _job(
                job_type="memory.reconcile_message_delete",
                payload={"message_id": str(MESSAGE_ID)},
            ),
            None,
        ),
        (
            _job(
                job_type="memory.reconcile_message_delete",
                payload={"message_id": str(MESSAGE_ID)},
            ),
            (CONVERSATION_ID, None),
        ),
    ],
)
async def test_memory_refresh_fails_closed_when_the_source_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    job: JobRecord,
    row: tuple[object, ...] | None,
) -> None:
    sessions = _Sessions(row)
    refresh_calls: list[dict[str, object]] = []
    _install_refresh_repository(monkeypatch, refresh_calls)

    with pytest.raises(JobExecutionError, match=r"^WORKER_MEMORY_SOURCE_MISSING$") as raised:
        await MemoryRefreshExecutor(now=lambda: NOW)(_context(job, sessions))

    assert not raised.value.retryable
    assert sessions.calls == 1
    assert refresh_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job",
    [
        _job(job_type="memory.review_action", payload={}, account_id=None),
        _job(job_type="memory.review_action", payload={"unexpected": "value"}),
    ],
)
async def test_memory_review_rejects_invalid_scope_or_payload_before_runtime_construction(
    job: JobRecord,
) -> None:
    sessions = _Sessions(None)

    with pytest.raises(JobExecutionError, match=r"^WORKER_JOB_SCOPE_INVALID$") as raised:
        await MemoryReviewExecutor(erasure_scope_secret=SensitiveValue(b"x" * 32))(
            _context(job, sessions)
        )

    assert not raised.value.retryable
    assert sessions.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_review_rejects_a_lost_fence_before_runtime_construction() -> None:
    sessions = _Sessions(None)
    job = _job(job_type="memory.review_action", payload={})

    with pytest.raises(JobExecutionError, match=r"^WORKER_JOB_FENCE_LOST$") as raised:
        await MemoryReviewExecutor(erasure_scope_secret=SensitiveValue(b"x" * 32))(
            _context(job, sessions, lease_lost=True)
        )

    assert raised.value.retryable
    assert sessions.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_review_constructs_its_runtime_with_the_bound_secret_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions(None)
    secret = SensitiveValue(b"x" * 32)
    construction_calls: list[tuple[object, SensitiveValue[bytes]]] = []
    account_calls: list[UUID] = []

    class ReviewRuntime:
        def __init__(
            self,
            session_factory: object,
            *,
            erasure_scope_secret: SensitiveValue[bytes],
        ) -> None:
            construction_calls.append((session_factory, erasure_scope_secret))

        async def run_once(self, *, account_id: UUID) -> None:
            account_calls.append(account_id)

    monkeypatch.setattr(executors_module, "MemoryReviewRuntimeService", ReviewRuntime)

    await MemoryReviewExecutor(erasure_scope_secret=secret)(
        _context(_job(job_type="memory.review_action", payload={}), sessions)
    )

    assert construction_calls == [(sessions, secret)]
    assert account_calls == [ACCOUNT_ID]
    assert sessions.calls == 0


@pytest.mark.unit
def test_worker_executor_registry_builds_one_shared_refresh_and_one_review_executor() -> None:
    registry = build_worker_executor_registry(erasure_scope_secret=SensitiveValue(b"x" * 32))

    assert registry.job_types == {
        "memory.refresh_completed_turn",
        "memory.reconcile_message_delete",
        "memory.review_action",
        "memory.reconcile_erasure",
    }
    refresh_turn = registry._executors["memory.refresh_completed_turn"]
    refresh_delete = registry._executors["memory.reconcile_message_delete"]
    review = registry._executors["memory.review_action"]
    assert isinstance(refresh_turn, MemoryRefreshExecutor)
    assert refresh_turn is refresh_delete
    assert isinstance(review, MemoryReviewExecutor)
