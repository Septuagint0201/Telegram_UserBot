"""Durable media lifecycle state matching private filesystem side effects."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.media.storage import StoredMedia
from telegram_userbot.adapters.persistence.schema import media_objects, message_media
from telegram_userbot.domain.shared.time import require_aware

DEFAULT_MEDIA_DELETE_LEASE = timedelta(minutes=5)
MEDIA_DELETE_RETRY_BASE = timedelta(minutes=1)
MEDIA_DELETE_RETRY_CAP = timedelta(hours=1)


def _media_delete_backoff(attempt_count: int) -> timedelta:
    if attempt_count <= 0:
        raise ValueError("media delete attempt count must be positive")
    multiplier = 1 << min(attempt_count - 1, 10)
    return min(MEDIA_DELETE_RETRY_BASE * multiplier, MEDIA_DELETE_RETRY_CAP)


@dataclass(frozen=True, slots=True)
class MediaDeletionLease:
    object_id: UUID
    account_id: UUID
    storage_key: str
    sha256: bytes
    fencing_token: int
    attempt_count: int


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit_cleanup_boundary(self) -> None:
        """Commit content-free cleanup state before or after a filesystem side effect."""

        await self._session.commit()

    async def create_pending(
        self,
        *,
        object_id: UUID,
        account_id: UUID,
        object_kind: str,
        parent_object_id: UUID | None,
        created_at: datetime,
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
        kind = await self._session.scalar(
            select(media_objects.c.object_kind).where(
                media_objects.c.id == object_id,
                media_objects.c.account_id == account_id,
                media_objects.c.status == "pending",
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
    ) -> tuple[MediaDeletionLease, ...]:
        current_time = require_aware(now, "now")
        if limit <= 0 or limit > 100 or lease <= timedelta(0) or lease > timedelta(minutes=15):
            raise ValueError("media deletion claim policy is invalid")
        unreferenced = ~media_objects.c.id.in_(
            select(message_media.c.media_object_id).where(
                message_media.c.media_object_id.is_not(None)
            )
        )
        eligible = or_(
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
                        unreferenced,
                        media_objects.c.storage_key.is_not(None),
                        media_objects.c.sha256.is_not(None),
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
                    unreferenced,
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
                    storage_key=cast(str, row["storage_key"]),
                    sha256=cast(bytes, row["sha256"]),
                    fencing_token=cast(int, fencing_token),
                    attempt_count=cast(int, row["delete_attempt_count"]) + 1,
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
    ) -> bool:
        current_time = require_aware(now, "now")
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
        return result.rowcount == 1
