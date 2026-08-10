"""SQLAlchemy repositories for M1 canonical durable facts."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import RowMapping, and_, case, insert, null, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    AccountRecord,
    ConversationRecord,
    JobRecord,
    JobState,
    NewJobRecord,
    OutboxRecord,
)
from telegram_userbot.adapters.persistence.schema import (
    accounts,
    background_jobs,
    conversations,
    message_revisions,
    messages,
    transactional_outbox,
)


def _account(row: RowMapping) -> AccountRecord:
    return AccountRecord(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        display_label=row["display_label"],
        status=row["status"],
        default_timezone=row["default_timezone"],
    )


def _conversation(row: RowMapping) -> ConversationRecord:
    return ConversationRecord(
        id=row["id"],
        account_id=row["account_id"],
        mode_version=row["mode_version"],
        content_revision=row["content_revision"],
        base_mode_override=row["base_mode_override"],
        contact_paused=row["contact_paused"],
    )


def _job(row: RowMapping) -> JobRecord:
    return JobRecord(
        id=row["id"],
        account_id=row["account_id"],
        queue_name=row["queue_name"],
        job_type=row["job_type"],
        state=JobState(row["state"]),
        priority=row["priority"],
        payload=cast(dict[str, Any], row["payload"]),
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        version=row["version"],
        fencing_token=row["fencing_token"],
        dispatch_generation=row["dispatch_generation"],
    )


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: AccountRecord) -> None:
        await self._session.execute(
            insert(accounts).values(
                id=record.id,
                telegram_user_id=record.telegram_user_id,
                display_label=record.display_label,
                status=record.status,
                default_timezone=record.default_timezone,
            )
        )

    async def get(self, account_id: UUID) -> AccountRecord | None:
        row = (
            (await self._session.execute(select(accounts).where(accounts.c.id == account_id)))
            .mappings()
            .one_or_none()
        )
        return None if row is None else _account(row)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, conversation_id: UUID) -> ConversationRecord | None:
        row = (
            (
                await self._session.execute(
                    select(conversations).where(conversations.c.id == conversation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _conversation(row)

    async def compare_and_set_mode(
        self,
        *,
        conversation_id: UUID,
        expected_version: int,
        base_mode_override: str | None,
        contact_paused: bool,
        now: datetime,
    ) -> ConversationRecord | None:
        statement = (
            update(conversations)
            .where(
                conversations.c.id == conversation_id,
                conversations.c.mode_version == expected_version,
            )
            .values(
                base_mode_override=base_mode_override,
                contact_paused=contact_paused,
                mode_version=conversations.c.mode_version + 1,
                updated_at=now,
            )
            .returning(*conversations.c)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _conversation(row)


class DurableJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, job: NewJobRecord) -> UUID:
        statement = (
            postgresql_insert(background_jobs)
            .values(
                id=job.id,
                account_id=job.account_id,
                queue_name=job.queue_name,
                job_type=job.job_type,
                idempotency_key=job.idempotency_key,
                payload_schema_version=1,
                payload=job.payload,
                available_at=job.available_at,
                priority=job.priority,
                max_attempts=job.max_attempts,
            )
            .on_conflict_do_nothing(constraint="uq_background_jobs_idempotency")
            .returning(background_jobs.c.id)
        )
        inserted = await self._session.scalar(statement)
        if inserted is not None:
            await self._add_wakeup(job.id, 1, job.account_id)
            return cast(UUID, inserted)
        existing = await self._session.scalar(
            select(background_jobs.c.id).where(
                background_jobs.c.queue_name == job.queue_name,
                background_jobs.c.idempotency_key == job.idempotency_key,
            )
        )
        if existing is None:
            raise RuntimeError("idempotent job disappeared inside transaction")
        return cast(UUID, existing)

    async def _add_wakeup(self, job_id: UUID, generation: int, account_id: UUID | None) -> None:
        await self._session.execute(
            postgresql_insert(transactional_outbox)
            .values(
                account_id=account_id,
                topic="durable_job.available",
                aggregate_type="background_job",
                aggregate_id=str(job_id),
                aggregate_version=generation,
                payload_schema_version=1,
                payload={"job_id": str(job_id), "dispatch_generation": generation},
            )
            .on_conflict_do_nothing(constraint="uq_transactional_outbox_generation")
        )

    async def claim_next(
        self,
        *,
        queue_name: str,
        owner: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> JobRecord | None:
        candidate = (
            await self._session.execute(
                select(background_jobs.c.id)
                .where(
                    background_jobs.c.queue_name == queue_name,
                    background_jobs.c.state.in_((JobState.PENDING, JobState.RETRY_WAIT)),
                    background_jobs.c.available_at <= now,
                )
                .order_by(
                    background_jobs.c.priority.desc(),
                    background_jobs.c.available_at,
                    background_jobs.c.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None
        row = (
            (
                await self._session.execute(
                    update(background_jobs)
                    .where(background_jobs.c.id == candidate)
                    .values(
                        state=JobState.LEASED,
                        lease_owner=owner,
                        lease_expires_at=now + lease_duration,
                        attempt_count=background_jobs.c.attempt_count + 1,
                        version=background_jobs.c.version + 1,
                        fencing_token=background_jobs.c.fencing_token + 1,
                        updated_at=now,
                    )
                    .returning(*background_jobs.c)
                )
            )
            .mappings()
            .one()
        )
        return _job(row)

    async def renew(
        self,
        *,
        job_id: UUID,
        owner: UUID,
        fencing_token: int,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.id == job_id,
                    background_jobs.c.state == JobState.LEASED,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == fencing_token,
                    background_jobs.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + lease_duration,
                    version=background_jobs.c.version + 1,
                    updated_at=now,
                )
            ),
        )
        return result.rowcount == 1

    async def complete(
        self,
        *,
        job_id: UUID,
        owner: UUID,
        fencing_token: int,
        now: datetime,
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.id == job_id,
                    background_jobs.c.state == JobState.LEASED,
                    background_jobs.c.lease_owner == owner,
                    background_jobs.c.fencing_token == fencing_token,
                )
                .values(
                    state=JobState.SUCCEEDED,
                    lease_owner=None,
                    lease_expires_at=None,
                    version=background_jobs.c.version + 1,
                    updated_at=now,
                    completed_at=now,
                )
            ),
        )
        return result.rowcount == 1

    async def recover_expired(self, *, now: datetime) -> int:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.state == JobState.LEASED,
                    background_jobs.c.lease_expires_at <= now,
                )
                .values(
                    state=case(
                        (
                            background_jobs.c.attempt_count >= background_jobs.c.max_attempts,
                            JobState.DEAD_LETTER,
                        ),
                        else_=JobState.RETRY_WAIT,
                    ),
                    lease_owner=None,
                    lease_expires_at=None,
                    available_at=now,
                    version=background_jobs.c.version + 1,
                    updated_at=now,
                    last_error_code="lease_expired",
                )
            ),
        )
        return result.rowcount

    async def rebuild_notifications(self, *, queue_name: str) -> int:
        rows = (
            await self._session.execute(
                update(background_jobs)
                .where(
                    background_jobs.c.queue_name == queue_name,
                    background_jobs.c.state.in_((JobState.PENDING, JobState.RETRY_WAIT)),
                )
                .values(
                    dispatch_generation=background_jobs.c.dispatch_generation + 1,
                    version=background_jobs.c.version + 1,
                )
                .returning(
                    background_jobs.c.id,
                    background_jobs.c.account_id,
                    background_jobs.c.dispatch_generation,
                )
            )
        ).all()
        for job_id, account_id, generation in rows:
            await self._add_wakeup(job_id, generation, account_id)
        return len(rows)


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_batch(self, *, limit: int = 100) -> Sequence[OutboxRecord]:
        if limit < 1 or limit > 1000:
            raise ValueError("outbox batch limit must be between 1 and 1000")
        rows = (
            await self._session.execute(
                select(transactional_outbox)
                .where(transactional_outbox.c.published_at.is_(None))
                .order_by(transactional_outbox.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).mappings()
        return tuple(
            OutboxRecord(
                id=row["id"],
                topic=row["topic"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                aggregate_version=row["aggregate_version"],
                payload=cast(dict[str, Any], row["payload"]),
            )
            for row in rows
        )

    async def mark_published(self, *, outbox_id: int, now: datetime) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(transactional_outbox)
                .where(
                    transactional_outbox.c.id == outbox_id,
                    transactional_outbox.c.published_at.is_(None),
                )
                .values(
                    published_at=now,
                    publish_attempts=transactional_outbox.c.publish_attempts + 1,
                    last_error_code=None,
                )
            ),
        )
        return result.rowcount == 1

    async def record_failure(self, *, outbox_id: int, error_code: str) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(transactional_outbox)
                .where(transactional_outbox.c.id == outbox_id)
                .values(
                    publish_attempts=transactional_outbox.c.publish_attempts + 1,
                    last_error_code=error_code,
                )
            ),
        )
        return result.rowcount == 1


class MessageRedactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def redact_message(
        self,
        *,
        account_id: UUID,
        message_id: UUID,
        reason: str,
        now: datetime,
        cleanup_job: NewJobRecord,
    ) -> int:
        if reason not in {"telegram_delete", "contact_purge", "account_wipe", "policy"}:
            raise ValueError("unknown redaction reason")
        if cleanup_job.account_id != account_id or cleanup_job.payload != {
            "message_id": str(message_id)
        }:
            raise ValueError("redaction cleanup job must bind the exact message scope")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(message_revisions)
                .where(
                    message_revisions.c.account_id == account_id,
                    message_revisions.c.message_id == message_id,
                    message_revisions.c.redacted_at.is_(None),
                )
                .values(
                    text_content=None,
                    caption=None,
                    entities=null(),
                    content_sha256=None,
                    redacted_at=now,
                    redaction_reason=reason,
                )
            ),
        )
        await self._session.execute(
            update(messages)
            .where(messages.c.account_id == account_id, messages.c.id == message_id)
            .values(is_tombstone=True, deleted_at=now, last_observed_at=now)
        )
        await DurableJobRepository(self._session).create(cleanup_job)
        return result.rowcount

    async def visible_current_text(
        self, *, account_id: UUID, message_id: UUID
    ) -> tuple[str | None, str | None] | None:
        statement = (
            select(message_revisions.c.text_content, message_revisions.c.caption)
            .select_from(
                messages.join(
                    message_revisions,
                    and_(
                        messages.c.id == message_revisions.c.message_id,
                        messages.c.account_id == message_revisions.c.account_id,
                        messages.c.current_revision_no == message_revisions.c.revision_no,
                    ),
                )
            )
            .where(
                messages.c.account_id == account_id,
                messages.c.id == message_id,
                messages.c.is_tombstone.is_(False),
                message_revisions.c.redacted_at.is_(None),
            )
        )
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else (row.text_content, row.caption)
