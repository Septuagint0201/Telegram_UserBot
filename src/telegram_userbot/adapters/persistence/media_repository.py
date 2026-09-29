"""Durable media lifecycle state matching private filesystem side effects."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.media.storage import StoredMedia
from telegram_userbot.adapters.persistence.schema import (
    accounts,
    context_manifest_items,
    context_manifests,
    data_erasure_requests,
    erasure_media_checks,
    erasure_progress,
    media_objects,
    memory_input_manifest_items,
    memory_input_manifests,
    memory_jobs,
    message_media,
    message_revisions,
    model_runs,
)
from telegram_userbot.domain.shared.time import require_aware

DEFAULT_MEDIA_DELETE_LEASE = timedelta(minutes=5)
MEDIA_DELETE_RETRY_BASE = timedelta(minutes=1)
MEDIA_DELETE_RETRY_CAP = timedelta(hours=1)
MEDIA_DELETE_CRITICAL_AFTER = timedelta(hours=24)
MEDIA_UPLOAD_TIMEOUT = timedelta(minutes=5)


def _media_delete_backoff(attempt_count: int) -> timedelta:
    if attempt_count <= 0:
        raise ValueError("media delete attempt count must be positive")
    multiplier = 1 << min(attempt_count - 1, 10)
    return min(MEDIA_DELETE_RETRY_BASE * multiplier, MEDIA_DELETE_RETRY_CAP)


@dataclass(frozen=True, slots=True)
class MediaDeletionLease:
    object_id: UUID
    account_id: UUID
    storage_key: str | None
    sha256: bytes | None
    fencing_token: int
    attempt_count: int
    first_failed_at: datetime | None = None
    critical_alerted: bool = False


@dataclass(frozen=True, slots=True)
class MediaDeletionOutcome:
    completed: bool
    critical_alert: bool = False


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit_cleanup_boundary(self) -> None:
        """Commit content-free cleanup state before or after a filesystem side effect."""

        await self._session.commit()

    async def account_is_deleting(self, account_id: UUID) -> bool:
        return await self._session.scalar(
            select(accounts.c.status).where(accounts.c.id == account_id)
        ) in ("deleting", "deleted")

    async def inventory_requests(self, account_id: UUID) -> tuple[UUID, ...]:
        # Match the worker's account admission lock while inspecting the filesystem.
        await self._session.execute(
            select(accounts.c.id).where(accounts.c.id == account_id).with_for_update(key_share=True)
        )
        return tuple(
            (
                await self._session.scalars(
                    select(data_erasure_requests.c.id)
                    .join(
                        erasure_progress,
                        erasure_progress.c.request_id == data_erasure_requests.c.id,
                    )
                    .where(
                        data_erasure_requests.c.account_id == account_id,
                        data_erasure_requests.c.scope_type.in_(("contact", "account")),
                        data_erasure_requests.c.state == "derived_cleanup",
                        erasure_progress.c.step_name == "physical_media",
                        erasure_progress.c.state == "completed",
                        ~select(erasure_media_checks.c.request_id)
                        .where(erasure_media_checks.c.request_id == data_erasure_requests.c.id)
                        .exists(),
                    )
                )
            ).all()
        )

    async def inventory_objects(self, account_id: UUID) -> dict[UUID, bool | None]:
        live = func.erasure_live_media(account_id).table_valued("id")
        rows = (
            await self._session.execute(
                select(media_objects.c.id, media_objects.c.status, live.c.id.label("live_id"))
                .outerjoin(live, live.c.id == media_objects.c.id)
                .where(media_objects.c.account_id == account_id)
            )
        ).all()
        return {
            row.id: True if row.status == "deleted" else False if row.live_id else None
            for row in rows
        }

    async def record_inventory(self, requests: tuple[UUID, ...], now: datetime) -> None:
        for request_id in requests:
            await self._session.execute(
                postgresql_insert(erasure_media_checks)
                .values(request_id=request_id, checked_at=now)
                .on_conflict_do_nothing()
            )

    async def ready_bytes(self, *, account_id: UUID) -> int:
        """Count files still present for the account's hard media quota."""

        value = await self._session.scalar(
            select(func.coalesce(func.sum(media_objects.c.byte_size), 0)).where(
                media_objects.c.account_id == account_id,
                media_objects.c.storage_key.is_not(None),
                media_objects.c.status.in_(("ready", "delete_pending", "failed")),
            )
        )
        if type(value) is not int or value < 0:
            raise RuntimeError("media byte total is invalid")
        return value

    async def create_pending(  # noqa: PLR0913 - durable upload provenance is explicit
        self,
        *,
        object_id: UUID,
        account_id: UUID,
        object_kind: str,
        parent_object_id: UUID | None,
        created_at: datetime,
        source_message_id: UUID | None = None,
        source_revision_no: int | None = None,
    ) -> None:
        retention = "media_original_30d" if object_kind == "original" else "media_provider_copy_24h"
        await self._session.execute(
            insert(media_objects).values(
                id=object_id,
                account_id=account_id,
                object_kind=object_kind,
                status="pending",
                parent_object_id=parent_object_id,
                retention_class=retention,
                created_at=created_at,
                source_revision_id=(
                    select(message_revisions.c.id)
                    .where(
                        message_revisions.c.account_id == account_id,
                        message_revisions.c.message_id == source_message_id,
                        message_revisions.c.revision_no == source_revision_no,
                    )
                    .scalar_subquery()
                    if source_message_id is not None
                    else None
                ),
            )
        )

    async def mark_ready(
        self,
        *,
        object_id: UUID,
        account_id: UUID,
        stored: StoredMedia,
        ready_at: datetime,
    ) -> bool:
        # Same account-before-object lock order as scope erasure and media admission.
        await self._session.execute(
            select(accounts.c.id).where(accounts.c.id == account_id).with_for_update(key_share=True)
        )
        kind = await self._session.scalar(
            select(media_objects.c.object_kind).where(
                media_objects.c.id == object_id,
                media_objects.c.account_id == account_id,
                media_objects.c.status == "pending",
                media_objects.c.delete_requested_at.is_(None),
            )
        )
        if kind is None:
            return False
        expires_at = ready_at + (timedelta(days=30) if kind == "original" else timedelta(hours=24))
        row = await self._session.scalar(
            update(media_objects)
            .where(
                media_objects.c.id == object_id,
                media_objects.c.account_id == account_id,
                media_objects.c.status == "pending",
                media_objects.c.delete_requested_at.is_(None),
            )
            .values(
                status="ready",
                storage_key=stored.storage_key,
                sha256=stored.sha256,
                validated_mime=stored.mime_type,
                byte_size=stored.byte_size,
                width=stored.width,
                height=stored.height,
                ready_at=ready_at,
                expires_at=expires_at,
            )
            .returning(media_objects.c.id)
        )
        return row is not None

    async def mark_rejected(
        self,
        *,
        object_id: UUID,
        account_id: UUID,
        error_code: str,
    ) -> bool:
        row = await self._session.scalar(
            update(media_objects)
            .where(
                media_objects.c.id == object_id,
                media_objects.c.account_id == account_id,
                media_objects.c.status == "pending",
            )
            .values(status="rejected", validation_error_code=error_code)
            .returning(media_objects.c.id)
        )
        return row is not None

    async def attach_to_revision(
        self,
        *,
        message_revision_id: UUID,
        position: int,
        media_object_id: UUID,
    ) -> bool:
        account_id = await self._session.scalar(
            select(media_objects.c.account_id)
            .where(
                media_objects.c.id == media_object_id,
                media_objects.c.status == "ready",
            )
            .with_for_update()
        )
        if account_id is None:
            return False
        result = await self._session.execute(
            update(message_media)
            .where(
                message_media.c.message_revision_id == message_revision_id,
                message_media.c.account_id == account_id,
                message_media.c.position == position,
                message_media.c.media_object_id.is_(None),
            )
            .values(media_object_id=media_object_id)
            .returning(message_media.c.id)
        )
        return result.scalar_one_or_none() is not None

    async def claim_expired(
        self,
        *,
        now: datetime,
        limit: int = 50,
        lease: timedelta = DEFAULT_MEDIA_DELETE_LEASE,
        account_id: UUID | None = None,
    ) -> tuple[MediaDeletionLease, ...]:
        current_time = require_aware(now, "now")
        if limit <= 0 or limit > 100 or lease <= timedelta(0) or lease > timedelta(minutes=15):
            raise ValueError("media deletion claim policy is invalid")
        # A canonical message reference is durable metadata, not an infinite
        # filesystem-retention lease.  Only manifests belonging to work that can
        # still read bytes protect an expired object.  Terminal manifests remain
        # auditable after the object is deleted because their hashes and typed
        # references stay in PostgreSQL.
        active_model_read = media_objects.c.id.in_(
            select(context_manifest_items.c.media_object_id)
            .select_from(
                context_manifest_items.join(
                    context_manifests,
                    and_(
                        context_manifests.c.id == context_manifest_items.c.manifest_id,
                        context_manifests.c.account_id == context_manifest_items.c.account_id,
                    ),
                ).join(
                    model_runs,
                    and_(
                        model_runs.c.context_manifest_id == context_manifests.c.id,
                        model_runs.c.account_id == context_manifests.c.account_id,
                    ),
                )
            )
            .where(
                context_manifest_items.c.media_object_id.is_not(None),
                model_runs.c.state.in_(("created", "running", "retry_wait")),
                context_manifests.c.scope_erased_at.is_(None),
            )
        )
        active_memory_read = media_objects.c.id.in_(
            select(memory_input_manifest_items.c.media_object_id)
            .select_from(
                memory_input_manifest_items.join(
                    memory_input_manifests,
                    and_(
                        memory_input_manifests.c.id == memory_input_manifest_items.c.manifest_id,
                        memory_input_manifests.c.account_id
                        == memory_input_manifest_items.c.account_id,
                    ),
                ).join(
                    memory_jobs,
                    and_(
                        memory_jobs.c.input_manifest_id == memory_input_manifests.c.id,
                        memory_jobs.c.account_id == memory_input_manifests.c.account_id,
                    ),
                )
            )
            .where(
                memory_input_manifest_items.c.media_object_id.is_not(None),
                memory_jobs.c.state.in_(("pending", "leased", "running", "retry_wait")),
                memory_input_manifests.c.scope_erased_at.is_(None),
            )
        )
        not_actively_read = ~or_(active_model_read, active_memory_read)
        eligible = or_(
            and_(
                media_objects.c.status.in_(("pending", "rejected", "failed")),
                media_objects.c.delete_next_attempt_at.is_(None),
                or_(
                    media_objects.c.delete_requested_at.is_not(None),
                    media_objects.c.created_at <= current_time - MEDIA_UPLOAD_TIMEOUT,
                ),
            ),
            and_(
                media_objects.c.status == "ready",
                media_objects.c.expires_at <= current_time,
            ),
            and_(
                media_objects.c.status == "delete_pending",
                media_objects.c.delete_lease_expires_at <= current_time,
            ),
            and_(
                media_objects.c.status == "failed",
                media_objects.c.delete_next_attempt_at <= current_time,
            ),
        )
        rows = (
            (
                await self._session.execute(
                    select(media_objects)
                    .where(
                        eligible,
                        *(
                            ()
                            if account_id is None
                            else (media_objects.c.account_id == account_id,)
                        ),
                        not_actively_read,
                        or_(
                            and_(
                                media_objects.c.storage_key.is_not(None),
                                media_objects.c.sha256.is_not(None),
                            ),
                            and_(
                                media_objects.c.storage_key.is_(None),
                                media_objects.c.sha256.is_(None),
                            ),
                        ),
                    )
                    .order_by(
                        media_objects.c.expires_at,
                        media_objects.c.delete_next_attempt_at,
                        media_objects.c.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        leases: list[MediaDeletionLease] = []
        for row in rows:
            fencing_token = await self._session.scalar(
                update(media_objects)
                .where(
                    media_objects.c.id == row["id"],
                    media_objects.c.account_id == row["account_id"],
                    media_objects.c.status == row["status"],
                    media_objects.c.delete_fencing_token == row["delete_fencing_token"],
                    not_actively_read,
                )
                .values(
                    status="delete_pending",
                    delete_requested_at=func.coalesce(
                        media_objects.c.delete_requested_at, current_time
                    ),
                    delete_claimed_at=current_time,
                    delete_lease_expires_at=current_time + lease,
                    delete_fencing_token=media_objects.c.delete_fencing_token + 1,
                    delete_attempt_count=media_objects.c.delete_attempt_count + 1,
                    delete_next_attempt_at=None,
                    delete_error_code=None,
                )
                .returning(media_objects.c.delete_fencing_token)
            )
            if fencing_token is None:
                continue
            leases.append(
                MediaDeletionLease(
                    object_id=cast(UUID, row["id"]),
                    account_id=cast(UUID, row["account_id"]),
                    storage_key=cast(str | None, row["storage_key"]),
                    sha256=cast(bytes | None, row["sha256"]),
                    fencing_token=cast(int, fencing_token),
                    attempt_count=cast(int, row["delete_attempt_count"]) + 1,
                    first_failed_at=cast(datetime | None, row["delete_first_failed_at"]),
                    critical_alerted=row["delete_critical_alerted_at"] is not None,
                )
            )
        return tuple(leases)

    async def finish_deletion(
        self,
        *,
        deletion: MediaDeletionLease,
        deleted: bool,
        now: datetime,
        error_code: str | None = None,
    ) -> MediaDeletionOutcome:
        current_time = require_aware(now, "now")
        first_failed_at = deletion.first_failed_at or current_time
        critical_alert = (
            not deleted
            and deletion.first_failed_at is not None
            and not deletion.critical_alerted
            and current_time >= deletion.first_failed_at + MEDIA_DELETE_CRITICAL_AFTER
        )
        values: dict[str, object] = {
            "status": "deleted" if deleted else "failed",
            "delete_claimed_at": None,
            "delete_lease_expires_at": None,
            "delete_next_attempt_at": (
                None if deleted else current_time + _media_delete_backoff(deletion.attempt_count)
            ),
            "delete_error_code": None if deleted else error_code or "media_delete_failed",
            "deleted_at": current_time if deleted else None,
        }
        if not deleted:
            values["delete_first_failed_at"] = first_failed_at
            if critical_alert:
                values["delete_critical_alerted_at"] = current_time
        if deleted:
            values.update(storage_key=None, sha256=None)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(media_objects)
                .where(
                    media_objects.c.id == deletion.object_id,
                    media_objects.c.account_id == deletion.account_id,
                    media_objects.c.storage_key == deletion.storage_key,
                    media_objects.c.sha256 == deletion.sha256,
                    media_objects.c.status == "delete_pending",
                    media_objects.c.delete_fencing_token == deletion.fencing_token,
                )
                .values(**values)
            ),
        )
        completed = result.rowcount == 1
        return MediaDeletionOutcome(completed, completed and critical_alert)
