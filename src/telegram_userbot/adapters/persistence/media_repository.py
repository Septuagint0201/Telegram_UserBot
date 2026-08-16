"""Durable media lifecycle state matching private filesystem side effects."""

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.media import StoredMedia
from telegram_userbot.adapters.persistence.schema import media_objects, message_media


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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

    async def claim_expired(self, *, now: datetime, limit: int = 50) -> tuple[UUID, ...]:
        rows = tuple(
            await self._session.scalars(
                select(media_objects.c.id)
                .where(
                    media_objects.c.status == "ready",
                    media_objects.c.expires_at <= now,
                    ~media_objects.c.id.in_(
                        select(message_media.c.media_object_id).where(
                            message_media.c.media_object_id.is_not(None)
                        )
                    ),
                )
                .order_by(media_objects.c.expires_at, media_objects.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        if not rows:
            return ()
        claimed = set(
            await self._session.scalars(
                update(media_objects)
                .where(
                    media_objects.c.id.in_(rows),
                    media_objects.c.status == "ready",
                    ~media_objects.c.id.in_(
                        select(message_media.c.media_object_id).where(
                            message_media.c.media_object_id.is_not(None)
                        )
                    ),
                )
                .values(status="delete_pending", delete_requested_at=now)
                .returning(media_objects.c.id)
            )
        )
        return cast(
            tuple[UUID, ...], tuple(object_id for object_id in rows if object_id in claimed)
        )
