"""PostgreSQL facts used by the production worker runtime.

Redis notifications deliberately carry no business payload.  Every claim,
retry, and terminal decision is fenced against this repository instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Text, and_, exists, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import JobRecord, JobState, OutboxRecord
from telegram_userbot.adapters.persistence.schema import background_jobs, transactional_outbox
from telegram_userbot.domain.shared.time import require_aware

_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
DURABLE_JOB_TOPIC = "durable_job.available"
WORKER_JOB_QUEUES = frozenset({"memory", "proactive", "maintenance", "worker"})


def _job(row: Any) -> JobRecord:
    return JobRecord(
        id=cast(UUID, row["id"]),
        account_id=cast(UUID | None, row["account_id"]),
        queue_name=cast(str, row["queue_name"]),
        job_type=cast(str, row["job_type"]),
        state=JobState(row["state"]),
        priority=cast(int, row["priority"]),
        payload=cast(dict[str, Any], row["payload"]),
        attempt_count=cast(int, row["attempt_count"]),
        max_attempts=cast(int, row["max_attempts"]),
        available_at=cast(datetime, row["available_at"]),
        lease_owner=cast(UUID | None, row["lease_owner"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        version=cast(int, row["version"]),
        fencing_token=cast(int, row["fencing_token"]),
        dispatch_generation=cast(int, row["dispatch_generation"]),
    )


class WorkerJobRepository:
    """Exact-id job claims and crash recovery for a content-free wake-up."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_notification(  # noqa: PLR0913 - durable fence is explicit
        self,
        *,
        job_id: UUID,
        dispatch_generation: int,
        owner: UUID,
        now: datetime,
        queue_names: frozenset[str] = WORKER_JOB_QUEUES,
        lease_duration: timedelta = timedelta(seconds=60),
        allow_work: bool = True,
        allow_proactive: bool = True,
    ) -> JobRecord | None:
        current_time = require_aware(now, "now")
        if (
            not isinstance(job_id, UUID)
            or job_id.int == 0
            or type(dispatch_generation) is not int
            or dispatch_generation < 1
            or not isinstance(owner, UUID)
            or owner.int == 0
            or not queue_names
            or len(queue_names) > 16
            or any(not isinstance(name, str) or not name for name in queue_names)
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(minutes=15)
            or type(allow_work) is not bool
            or type(allow_proactive) is not bool
        ):
            raise ValueError("worker job claim is invalid")
        if not allow_work:
            return None
        admission = (background_jobs.c.queue_name != "proactive",) if not allow_proactive else ()
        row = (
            (
                await self._session.execute(
                    select(background_jobs)
                    .where(
                        background_jobs.c.id == job_id,
                        background_jobs.c.queue_name.in_(queue_names),
                        *admission,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if (
            row["state"] not in {JobState.PENDING.value, JobState.RETRY_WAIT.value}
            or row["available_at"] > current_time
            or row["attempt_count"] >= row["max_attempts"]
            or row["dispatch_generation"] < dispatch_generation
            or (row["expires_at"] is not None and row["expires_at"] <= current_time)
        ):
            return None
        claimed = (
            (
                await self._session.execute(
                    update(background_jobs)
                    .where(
                        background_jobs.c.id == job_id,
                        background_jobs.c.version == row["version"],
                        background_jobs.c.state == row["state"],
                    )
                    .values(
                        state=JobState.LEASED.value,
                        lease_owner=owner,
                        lease_expires_at=current_time + lease_duration,
                        attempt_count=background_jobs.c.attempt_count + 1,
                        fencing_token=background_jobs.c.fencing_token + 1,
                        version=background_jobs.c.version + 1,
                        updated_at=current_time,
                    )
                    .returning(*background_jobs.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if claimed is None else _job(claimed)

    async def renew(
        self,
        *,
        job: JobRecord,
        owner: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> bool:
        current_time = require_aware(now, "now")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.id == job.id,
                    background_jobs.c.state == JobState.LEASED.value,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == job.fencing_token,
                    background_jobs.c.lease_expires_at > current_time,
                )
                .values(
                    lease_expires_at=current_time + lease_duration,
                    version=background_jobs.c.version + 1,
                    updated_at=current_time,
                )
            ),
        )
        return result.rowcount == 1

    async def succeed(self, *, job: JobRecord, owner: UUID, now: datetime) -> bool:
        return await self._finish(
            job=job,
            owner=owner,
            now=now,
            state=JobState.SUCCEEDED,
            error_code=None,
        )

    async def fail_or_retry(  # noqa: PLR0913 - retry fence is explicit
        self,
        *,
        job: JobRecord,
        owner: UUID,
        now: datetime,
        error_code: str,
        retryable: bool,
        retry_delay: timedelta,
    ) -> JobState | None:
        current_time = require_aware(now, "now")
        if _ERROR_CODE.fullmatch(error_code) is None or retry_delay < timedelta(0):
            raise ValueError("worker job failure policy is invalid")
        row = (
            await self._session.execute(
                select(
                    background_jobs.c.expires_at,
                    background_jobs.c.lease_expires_at,
                )
                .where(
                    background_jobs.c.id == job.id,
                    background_jobs.c.state == JobState.LEASED.value,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == job.fencing_token,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row[1] is None or row[1] <= current_time:
            return None
        expires_at = row[0]
        business_expired = expires_at is not None and expires_at <= current_time
        if not retryable:
            terminal = JobState.FAILED
        elif job.attempt_count >= job.max_attempts or business_expired:
            terminal = JobState.DEAD_LETTER
        else:
            terminal = JobState.RETRY_WAIT
        next_generation = job.dispatch_generation + (1 if terminal is JobState.RETRY_WAIT else 0)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.id == job.id,
                    background_jobs.c.state == JobState.LEASED.value,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == job.fencing_token,
                    background_jobs.c.lease_expires_at > current_time,
                )
                .values(
                    state=terminal.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    available_at=(
                        current_time + retry_delay
                        if terminal is JobState.RETRY_WAIT
                        else job.available_at
                    ),
                    dispatch_generation=next_generation,
                    last_error_code=error_code,
                    version=background_jobs.c.version + 1,
                    updated_at=current_time,
                    completed_at=(None if terminal is JobState.RETRY_WAIT else current_time),
                )
            ),
        )
        if result.rowcount != 1:
            return None
        if terminal is JobState.RETRY_WAIT:
            await self._add_wakeup(job.id, job.account_id, next_generation)
        return terminal

    async def _finish(
        self,
        *,
        job: JobRecord,
        owner: UUID,
        now: datetime,
        state: JobState,
        error_code: str | None,
    ) -> bool:
        current_time = require_aware(now, "now")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.id == job.id,
                    background_jobs.c.state == JobState.LEASED.value,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == job.fencing_token,
                    background_jobs.c.lease_expires_at > current_time,
                )
                .values(
                    state=state.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error_code=error_code,
                    version=background_jobs.c.version + 1,
                    updated_at=current_time,
                    completed_at=current_time,
                )
            ),
        )
        return result.rowcount == 1

    async def recover_expired(self, *, now: datetime, limit: int = 100) -> int:
        current_time = require_aware(now, "now")
        if not 1 <= limit <= 1000:
            raise ValueError("worker recovery limit is invalid")
        rows = (
            (
                await self._session.execute(
                    select(background_jobs)
                    .where(
                        background_jobs.c.queue_name.in_(WORKER_JOB_QUEUES),
                        background_jobs.c.state == JobState.LEASED.value,
                        background_jobs.c.lease_expires_at <= current_time,
                    )
                    .order_by(background_jobs.c.lease_expires_at, background_jobs.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        recovered = 0
        for row in rows:
            retryable = row["attempt_count"] < row["max_attempts"] and (
                row["expires_at"] is None or row["expires_at"] > current_time
            )
            state = JobState.RETRY_WAIT if retryable else JobState.DEAD_LETTER
            generation = row["dispatch_generation"] + (1 if retryable else 0)
            result = cast(
                CursorResult[Any],
                await self._session.execute(
                    update(background_jobs)
                    .where(
                        background_jobs.c.id == row["id"],
                        background_jobs.c.state == JobState.LEASED.value,
                        background_jobs.c.fencing_token == row["fencing_token"],
                        background_jobs.c.lease_expires_at <= current_time,
                    )
                    .values(
                        state=state.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        available_at=current_time,
                        dispatch_generation=generation,
                        last_error_code="WORKER_LEASE_EXPIRED",
                        version=background_jobs.c.version + 1,
                        updated_at=current_time,
                        completed_at=None if retryable else current_time,
                    )
                ),
            )
            if result.rowcount != 1:
                continue
            recovered += 1
            if retryable:
                await self._add_wakeup(row["id"], row["account_id"], generation)
        return recovered

    async def rebuild_due_notifications(
        self,
        *,
        now: datetime,
        limit: int = 100,
        allow_work: bool = True,
        allow_proactive: bool = True,
    ) -> int:
        current_time = require_aware(now, "now")
        if (
            not 1 <= limit <= 1000
            or type(allow_work) is not bool
            or type(allow_proactive) is not bool
        ):
            raise ValueError("worker notification rebuild limit is invalid")
        if not allow_work:
            return 0
        admission = (background_jobs.c.queue_name != "proactive",) if not allow_proactive else ()
        rows = (
            (
                await self._session.execute(
                    select(background_jobs)
                    .where(
                        background_jobs.c.queue_name.in_(WORKER_JOB_QUEUES),
                        *admission,
                        background_jobs.c.state.in_(
                            (JobState.PENDING.value, JobState.RETRY_WAIT.value)
                        ),
                        background_jobs.c.available_at <= current_time,
                        background_jobs.c.attempt_count < background_jobs.c.max_attempts,
                        or_(
                            background_jobs.c.expires_at.is_(None),
                            background_jobs.c.expires_at > current_time,
                        ),
                        ~exists(
                            select(transactional_outbox.c.id).where(
                                transactional_outbox.c.topic == DURABLE_JOB_TOPIC,
                                transactional_outbox.c.aggregate_type == "background_job",
                                transactional_outbox.c.aggregate_id
                                == sql_cast(background_jobs.c.id, Text),
                                transactional_outbox.c.published_at.is_(None),
                            )
                        ),
                    )
                    .order_by(
                        background_jobs.c.priority.desc(),
                        background_jobs.c.available_at,
                        background_jobs.c.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        rebuilt = 0
        for row in rows:
            generation = row["dispatch_generation"] + 1
            result = cast(
                CursorResult[Any],
                await self._session.execute(
                    update(background_jobs)
                    .where(
                        background_jobs.c.id == row["id"],
                        background_jobs.c.version == row["version"],
                        background_jobs.c.state == row["state"],
                    )
                    .values(
                        dispatch_generation=generation,
                        version=background_jobs.c.version + 1,
                        updated_at=current_time,
                    )
                ),
            )
            if result.rowcount != 1:
                continue
            await self._add_wakeup(row["id"], row["account_id"], generation)
            rebuilt += 1
        return rebuilt

    async def pending_count(self, *, now: datetime) -> int:
        current_time = require_aware(now, "now")
        value = await self._session.scalar(
            select(background_jobs.c.id)
            .where(
                background_jobs.c.queue_name.in_(WORKER_JOB_QUEUES),
                background_jobs.c.state.in_((JobState.PENDING.value, JobState.RETRY_WAIT.value)),
                background_jobs.c.available_at <= current_time,
            )
            .limit(1001)
        )
        # This cheap readiness probe only distinguishes empty/non-empty. Queue
        # depth metrics use a separate bounded operations query.
        return 0 if value is None else 1

    async def _add_wakeup(
        self,
        job_id: UUID,
        account_id: UUID | None,
        generation: int,
    ) -> None:
        await self._session.execute(
            postgresql_insert(transactional_outbox)
            .values(
                account_id=account_id,
                topic=DURABLE_JOB_TOPIC,
                aggregate_type="background_job",
                aggregate_id=str(job_id),
                aggregate_version=generation,
                payload_schema_version=1,
                payload={"job_id": str(job_id), "dispatch_generation": generation},
            )
            .on_conflict_do_nothing(constraint="uq_transactional_outbox_generation")
        )


class WorkerOutboxRepository:
    """Select only due worker wakeups; leave every other outbox topic untouched."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def due_wakeups(
        self,
        *,
        now: datetime,
        limit: int = 100,
        allow_work: bool = True,
        allow_proactive: bool = True,
    ) -> Sequence[OutboxRecord]:
        current_time = require_aware(now, "now")
        if (
            not 1 <= limit <= 1000
            or type(allow_work) is not bool
            or type(allow_proactive) is not bool
        ):
            raise ValueError("worker outbox limit is invalid")
        if not allow_work:
            return ()
        admission = (background_jobs.c.queue_name != "proactive",) if not allow_proactive else ()
        rows = (
            await self._session.execute(
                select(transactional_outbox)
                .select_from(
                    transactional_outbox.join(
                        background_jobs,
                        and_(
                            transactional_outbox.c.aggregate_type == "background_job",
                            transactional_outbox.c.aggregate_id
                            == sql_cast(background_jobs.c.id, Text),
                        ),
                    )
                )
                .where(
                    transactional_outbox.c.topic == DURABLE_JOB_TOPIC,
                    transactional_outbox.c.published_at.is_(None),
                    background_jobs.c.queue_name.in_(WORKER_JOB_QUEUES),
                    *admission,
                    background_jobs.c.available_at <= current_time,
                    background_jobs.c.dispatch_generation
                    >= transactional_outbox.c.aggregate_version,
                    background_jobs.c.state.in_(
                        (JobState.PENDING.value, JobState.RETRY_WAIT.value)
                    ),
                )
                .order_by(transactional_outbox.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True, of=transactional_outbox)
            )
        ).mappings()
        return tuple(
            OutboxRecord(
                id=cast(int, row["id"]),
                topic=cast(str, row["topic"]),
                aggregate_type=cast(str, row["aggregate_type"]),
                aggregate_id=cast(str, row["aggregate_id"]),
                aggregate_version=cast(int, row["aggregate_version"]),
                payload=cast(dict[str, Any], row["payload"]),
                payload_schema_version=cast(int, row["payload_schema_version"]),
                account_id=cast(UUID | None, row["account_id"]),
            )
            for row in rows
        )

    async def mark_published(self, *, outbox_id: int, now: datetime) -> bool:
        current_time = require_aware(now, "now")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(transactional_outbox)
                .where(
                    transactional_outbox.c.id == outbox_id,
                    transactional_outbox.c.topic == DURABLE_JOB_TOPIC,
                    transactional_outbox.c.published_at.is_(None),
                )
                .values(
                    published_at=current_time,
                    publish_attempts=transactional_outbox.c.publish_attempts + 1,
                    last_error_code=None,
                )
            ),
        )
        return result.rowcount == 1

    async def record_failure(self, *, outbox_id: int, error_code: str) -> bool:
        if _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("worker outbox error code is invalid")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(transactional_outbox)
                .where(
                    transactional_outbox.c.id == outbox_id,
                    transactional_outbox.c.topic == DURABLE_JOB_TOPIC,
                    transactional_outbox.c.published_at.is_(None),
                )
                .values(
                    publish_attempts=transactional_outbox.c.publish_attempts + 1,
                    last_error_code=error_code,
                )
            ),
        )
        return result.rowcount == 1


__all__ = [
    "DURABLE_JOB_TOPIC",
    "WORKER_JOB_QUEUES",
    "WorkerJobRepository",
    "WorkerOutboxRepository",
]
