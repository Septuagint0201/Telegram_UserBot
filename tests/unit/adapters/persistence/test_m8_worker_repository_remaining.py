from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import JobRecord, JobState, OutboxRecord
from telegram_userbot.adapters.persistence.worker_runtime import (
    DURABLE_JOB_TOPIC,
    WorkerJobRepository,
    WorkerOutboxRepository,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
JOB_ID = UUID("01900000-0000-7000-8000-000000000221")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000222")
OWNER_ID = UUID("01900000-0000-7000-8000-000000000223")


class _Result:
    def __init__(
        self,
        row: object | None = None,
        *,
        rows: list[object] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.row = row
        self.rows = [] if rows is None else rows
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> object | None:
        return self.row

    def all(self) -> list[object]:
        return self.rows

    def __iter__(self) -> Iterator[object]:
        return iter(self.rows)


def _job_row(  # noqa: PLR0913 - synthetic row builder exposes every durable field
    state: JobState = JobState.PENDING,
    *,
    version: int = 1,
    attempt_count: int = 0,
    max_attempts: int = 3,
    available_at: datetime = NOW,
    expires_at: datetime | None = None,
    lease_owner: UUID | None = None,
    lease_expires_at: datetime | None = None,
    fencing_token: int = 1,
    dispatch_generation: int = 1,
) -> dict[str, object]:
    return {
        "id": JOB_ID,
        "account_id": ACCOUNT_ID,
        "queue_name": "worker",
        "job_type": "maintenance",
        "state": state.value,
        "priority": 10,
        "payload": {"kind": "synthetic"},
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "available_at": available_at,
        "expires_at": expires_at,
        "lease_owner": lease_owner,
        "lease_expires_at": lease_expires_at,
        "version": version,
        "fencing_token": fencing_token,
        "dispatch_generation": dispatch_generation,
    }


def _leased_job(*, attempt_count: int = 1, max_attempts: int = 3) -> JobRecord:
    return JobRecord(
        id=JOB_ID,
        account_id=ACCOUNT_ID,
        queue_name="worker",
        job_type="maintenance",
        state=JobState.LEASED,
        priority=10,
        payload={"kind": "synthetic"},
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        available_at=NOW,
        lease_owner=OWNER_ID,
        lease_expires_at=NOW + timedelta(minutes=1),
        version=2,
        fencing_token=4,
        dispatch_generation=2,
    )


def _jobs(session: AsyncMock) -> WorkerJobRepository:
    return WorkerJobRepository(cast(AsyncSession, session))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claim_notification_maps_success_and_rejects_stale_rows() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _Result(_job_row()),
        _Result(
            _job_row(
                JobState.LEASED,
                version=2,
                attempt_count=1,
                lease_owner=OWNER_ID,
                lease_expires_at=NOW + timedelta(minutes=1),
                fencing_token=2,
            )
        ),
    ]
    repository = _jobs(session)

    claimed = await repository.claim_notification(
        job_id=JOB_ID,
        dispatch_generation=1,
        owner=OWNER_ID,
        now=NOW,
    )

    assert claimed is not None
    assert claimed.state is JobState.LEASED
    assert claimed.lease_owner == OWNER_ID
    claim_sql = str(
        session.execute.await_args_list[1]
        .args[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "background_jobs.state" in claim_sql
    assert "fencing_token" in claim_sql

    session.reset_mock()
    session.execute.side_effect = [_Result(_job_row(JobState.SUCCEEDED))]
    assert (
        await repository.claim_notification(
            job_id=JOB_ID,
            dispatch_generation=1,
            owner=OWNER_ID,
            now=NOW,
        )
        is None
    )

    with pytest.raises(ValueError, match="claim is invalid"):
        await repository.claim_notification(
            job_id=JOB_ID,
            dispatch_generation=0,
            owner=OWNER_ID,
            now=NOW,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_renew_and_succeed_are_fenced_by_owner_and_token() -> None:
    session = AsyncMock()
    session.execute.side_effect = [_Result(rowcount=1), _Result(rowcount=0), _Result(rowcount=1)]
    repository = _jobs(session)
    job = _leased_job()

    assert await repository.renew(job=job, owner=OWNER_ID, now=NOW)
    assert not await repository.renew(job=job, owner=OWNER_ID, now=NOW)
    assert await repository.succeed(job=job, owner=OWNER_ID, now=NOW)

    renew_sql = str(
        session.execute.await_args_list[0]
        .args[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "lease_owner" in renew_sql
    assert "fencing_token" in renew_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_or_retry_handles_retryable_terminal_and_cas_conflict_paths() -> None:
    job = _leased_job()

    retry_session = AsyncMock()
    retry_session.execute.side_effect = [
        _Result((None, NOW + timedelta(minutes=1))),
        _Result(rowcount=1),
        _Result(),
    ]
    retry_repo = _jobs(retry_session)
    assert (
        await retry_repo.fail_or_retry(
            job=job,
            owner=OWNER_ID,
            now=NOW,
            error_code="PROVIDER_TIMEOUT",
            retryable=True,
            retry_delay=timedelta(seconds=30),
        )
        is JobState.RETRY_WAIT
    )
    assert retry_session.execute.await_count == 3

    terminal_session = AsyncMock()
    terminal_session.execute.side_effect = [
        _Result((NOW + timedelta(days=1), NOW + timedelta(minutes=1))),
        _Result(rowcount=1),
    ]
    terminal_repo = _jobs(terminal_session)
    assert (
        await terminal_repo.fail_or_retry(
            job=job,
            owner=OWNER_ID,
            now=NOW,
            error_code="BAD_PROVIDER_RESPONSE",
            retryable=False,
            retry_delay=timedelta(0),
        )
        is JobState.FAILED
    )

    dead_session = AsyncMock()
    dead_session.execute.side_effect = [
        _Result((NOW + timedelta(days=1), NOW + timedelta(minutes=1))),
        _Result(rowcount=1),
    ]
    dead_repo = _jobs(dead_session)
    assert (
        await dead_repo.fail_or_retry(
            job=_leased_job(attempt_count=3, max_attempts=3),
            owner=OWNER_ID,
            now=NOW,
            error_code="MAX_ATTEMPTS_REACHED",
            retryable=True,
            retry_delay=timedelta(seconds=1),
        )
        is JobState.DEAD_LETTER
    )

    miss_session = AsyncMock()
    miss_session.execute.side_effect = [_Result((None, NOW - timedelta(seconds=1)))]
    assert (
        await _jobs(miss_session).fail_or_retry(
            job=job,
            owner=OWNER_ID,
            now=NOW,
            error_code="LEASE_LOST",
            retryable=True,
            retry_delay=timedelta(seconds=1),
        )
        is None
    )

    with pytest.raises(ValueError, match="failure policy"):
        await _jobs(AsyncMock()).fail_or_retry(
            job=job,
            owner=OWNER_ID,
            now=NOW,
            error_code="bad-code",
            retryable=True,
            retry_delay=timedelta(0),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recover_and_rebuild_requeue_only_successful_compare_and_sets() -> None:
    expired_retry = _job_row(
        JobState.LEASED,
        attempt_count=1,
        max_attempts=3,
        lease_expires_at=NOW - timedelta(minutes=1),
        fencing_token=5,
        dispatch_generation=2,
    )
    expired_dead = _job_row(
        JobState.LEASED,
        attempt_count=3,
        max_attempts=3,
        expires_at=NOW - timedelta(seconds=1),
        lease_expires_at=NOW - timedelta(minutes=2),
        fencing_token=6,
        dispatch_generation=3,
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _Result(rows=[expired_retry, expired_dead]),
        _Result(rowcount=1),
        _Result(),
        _Result(rowcount=0),
    ]
    repository = _jobs(session)
    assert await repository.recover_expired(now=NOW, limit=10) == 1
    assert session.execute.await_count == 4

    rebuild_row = _job_row(JobState.RETRY_WAIT, dispatch_generation=4)
    rebuild_session = AsyncMock()
    rebuild_session.execute.side_effect = [
        _Result(rows=[rebuild_row]),
        _Result(rowcount=1),
        _Result(),
    ]
    rebuild_repo = _jobs(rebuild_session)
    assert await rebuild_repo.rebuild_due_notifications(now=NOW, limit=5) == 1

    with pytest.raises(ValueError, match="rebuild limit"):
        await rebuild_repo.rebuild_due_notifications(now=NOW, limit=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_count_and_worker_outbox_map_rows_and_terminal_updates() -> None:
    session = AsyncMock()
    session.scalar.side_effect = [None, JOB_ID]
    repository = _jobs(session)
    assert await repository.pending_count(now=NOW) == 0
    assert await repository.pending_count(now=NOW) == 1

    outbox_session = AsyncMock()
    outbox_session.execute.side_effect = [
        _Result(
            rows=[
                {
                    "id": 7,
                    "topic": DURABLE_JOB_TOPIC,
                    "aggregate_type": "background_job",
                    "aggregate_id": str(JOB_ID),
                    "aggregate_version": 2,
                    "payload": {"job_id": str(JOB_ID)},
                    "payload_schema_version": 1,
                    "account_id": ACCOUNT_ID,
                }
            ]
        ),
        _Result(rowcount=1),
        _Result(rowcount=0),
        _Result(rowcount=1),
    ]
    outbox = WorkerOutboxRepository(cast(AsyncSession, outbox_session))
    records = await outbox.due_wakeups(now=NOW)
    assert records == (
        OutboxRecord(
            id=7,
            topic=DURABLE_JOB_TOPIC,
            aggregate_type="background_job",
            aggregate_id=str(JOB_ID),
            aggregate_version=2,
            payload={"job_id": str(JOB_ID)},
            account_id=ACCOUNT_ID,
        ),
    )
    assert await outbox.mark_published(outbox_id=7, now=NOW)
    assert not await outbox.mark_published(outbox_id=7, now=NOW)
    assert await outbox.record_failure(outbox_id=7, error_code="REDIS_UNAVAILABLE")
    with pytest.raises(ValueError, match="error code"):
        await outbox.record_failure(outbox_id=7, error_code="bad code")
