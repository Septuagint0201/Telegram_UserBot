"""Post-canonical Telegram image ingestion into private original/provider storage."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid7

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.media.storage import PrivateMediaStore, StoredMedia
from telegram_userbot.adapters.media.validation import ImageIngestionError, ImageIngestor
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.adapters.persistence.schema import (
    media_objects,
    message_media,
    message_revisions,
    messages,
)
from telegram_userbot.application.ports.media import (
    TelegramImageDownloadRequest,
    TelegramImageSource,
)
from telegram_userbot.application.ports.telegram import TelegramPermanentError
from telegram_userbot.application.ports.telegram_peer import TelegramMediaBinding

MediaBindingLoader = Callable[
    [TelegramImageDownloadRequest], Awaitable[TelegramMediaBinding | None]
]


class TelegramImageIngestStatus(StrEnum):
    READY = "ready"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TelegramImageIngestOutcome:
    status: TelegramImageIngestStatus
    error_code: str | None = None
    original_object_id: UUID | None = None
    provider_object_id: UUID | None = None


class TelegramImageIngestionService:
    """Run only after canonical ingest committed; failures never undo that message."""

    def __init__(  # noqa: PLR0913 - side-effect boundaries are intentionally explicit
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        load_binding: MediaBindingLoader,
        source: TelegramImageSource,
        ingestor: ImageIngestor,
        store: PrivateMediaStore,
        new_uuid: Callable[[], UUID] = uuid7,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._load_binding = load_binding
        self._source = source
        self._ingestor = ingestor
        self._store = store
        self._new_uuid = new_uuid
        self._now = now

    async def ingest(  # noqa: PLR0911,PLR0912,PLR0915 - linear durable side-effect workflow
        self, request: TelegramImageDownloadRequest
    ) -> TelegramImageIngestOutcome:
        target = await self._load_binding(request)
        if target is None:
            return TelegramImageIngestOutcome(TelegramImageIngestStatus.SKIPPED)
        if (
            target.account_id != request.account_id.value
            or target.conversation_id != request.conversation_id.value
            or target.message_id != request.message_id.value
            or target.revision_no != request.revision_no
            or target.position != request.position
        ):
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_binding_scope_mismatch",
            )
        original_id = self._new_uuid()
        try:
            async with self._session_factory() as session, session.begin():
                await MediaRepository(session).create_pending(
                    object_id=original_id,
                    account_id=target.account_id,
                    object_kind="original",
                    parent_object_id=None,
                    created_at=self._now(),
                    source_message_id=target.message_id,
                    source_revision_no=target.revision_no,
                )
        except Exception:
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_state_create_failed",
                original_object_id=original_id,
            )

        try:
            image = await self._ingestor.ingest(
                self._source.iter_image(request),
                declared_mime=target.declared_mime,
                declared_size=target.declared_size,
            )
            original = await self._write_in_thread(
                lambda: self._store.store_original(
                    account_id=target.account_id,
                    object_id=original_id,
                    image=image,
                ),
                object_id=original_id,
                account_id=target.account_id,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._reject(original_id, target.account_id, "image_ingest_cancelled")
            )
            raise
        except ImageIngestionError as error:
            await self._reject_terminal(
                request=request,
                target=target,
                object_id=original_id,
                code=error.code,
            )
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.REJECTED,
                error.code,
                original_object_id=original_id,
            )
        except TelegramPermanentError:
            await self._reject_terminal(
                request=request,
                target=target,
                object_id=original_id,
                code="image_source_rejected",
            )
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.REJECTED,
                "image_source_rejected",
                original_object_id=original_id,
            )
        except Exception:
            await self._reject(original_id, target.account_id, "image_ingest_failed")
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_ingest_failed",
                original_object_id=original_id,
            )

        try:
            original_ready = await self._mark_ready(original_id, target.account_id, original)
        except asyncio.CancelledError:
            await asyncio.shield(self._discard_pending(original_id, target.account_id, original))
            raise
        if not original_ready:
            await self._delete_stored(original)
            await self._reject(
                original_id,
                target.account_id,
                "image_original_commit_failed",
            )
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_original_commit_failed",
                original_object_id=original_id,
            )

        provider_id = self._new_uuid()
        provider: StoredMedia | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await MediaRepository(session).create_pending(
                    object_id=provider_id,
                    account_id=target.account_id,
                    object_kind="provider_copy",
                    parent_object_id=original_id,
                    created_at=self._now(),
                    source_message_id=target.message_id,
                    source_revision_no=target.revision_no,
                )
            provider = await self._write_in_thread(
                lambda: self._store.store_provider_copy(
                    account_id=target.account_id,
                    object_id=provider_id,
                    image=image,
                ),
                object_id=provider_id,
                account_id=target.account_id,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._discard_provider_attempt(
                    provider_id,
                    target.account_id,
                    provider,
                    original_id,
                    "image_provider_copy_cancelled",
                )
            )
            raise
        except Exception:
            await self._reject(provider_id, target.account_id, "image_provider_copy_failed")
            await self._expire_unattached_original(
                original_id,
                target.account_id,
                "image_provider_copy_failed",
            )
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_provider_copy_failed",
                original_object_id=original_id,
                provider_object_id=provider_id,
            )

        attached = False
        try:
            async with self._session_factory() as session, session.begin():
                repository = MediaRepository(session)
                ready = await repository.mark_ready(
                    object_id=provider_id,
                    account_id=target.account_id,
                    stored=provider,
                    ready_at=self._now(),
                )
                if ready:
                    attached = await self._attach_if_current(
                        session,
                        request=request,
                        target=target,
                        media_object_id=provider_id,
                    )
                if not attached:
                    # Roll back the pending -> ready transition as well. The file is
                    # deleted below and the still-pending row is durably rejected.
                    self._abort_attachment()
        except asyncio.CancelledError:
            await asyncio.shield(
                self._discard_provider_attempt(
                    provider_id,
                    target.account_id,
                    provider,
                    original_id,
                    "image_provider_attach_cancelled",
                )
            )
            raise
        except Exception:
            attached = False
        if not attached:
            await self._delete_stored(provider)
            await self._reject(
                provider_id,
                target.account_id,
                "image_provider_attach_failed",
            )
            await self._expire_unattached_original(
                original_id,
                target.account_id,
                "image_provider_attach_failed",
            )
            return TelegramImageIngestOutcome(
                TelegramImageIngestStatus.FAILED,
                "image_provider_attach_failed",
                original_object_id=original_id,
                provider_object_id=provider_id,
            )
        return TelegramImageIngestOutcome(
            TelegramImageIngestStatus.READY,
            original_object_id=original_id,
            provider_object_id=provider_id,
        )

    async def _attach_if_current(
        self,
        session: AsyncSession,
        *,
        request: TelegramImageDownloadRequest,
        target: TelegramMediaBinding,
        media_object_id: UUID,
    ) -> bool:
        """Lock and revalidate the exact current media slot before attachment."""

        row = (
            (
                await session.execute(
                    select(message_media.c.id)
                    .select_from(
                        messages.join(
                            message_revisions,
                            (message_revisions.c.message_id == messages.c.id)
                            & (message_revisions.c.account_id == messages.c.account_id),
                        ).join(
                            message_media,
                            (message_media.c.message_revision_id == message_revisions.c.id)
                            & (message_media.c.account_id == message_revisions.c.account_id),
                        )
                    )
                    .where(
                        messages.c.id == target.message_id,
                        messages.c.account_id == target.account_id,
                        messages.c.conversation_id == target.conversation_id,
                        messages.c.current_revision_no == target.revision_no,
                        messages.c.deleted_at.is_(None),
                        messages.c.is_tombstone.is_(False),
                        message_revisions.c.revision_no == target.revision_no,
                        message_revisions.c.redacted_at.is_(None),
                        message_media.c.position == request.position,
                        message_media.c.media_kind.in_(("photo", "image_document")),
                        message_media.c.media_object_id.is_(None),
                    )
                    .with_for_update(of=(messages, message_revisions, message_media))
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        attached = await session.scalar(
            update(message_media)
            .where(
                message_media.c.id == row["id"],
                message_media.c.account_id == target.account_id,
                message_media.c.media_object_id.is_(None),
            )
            .values(media_object_id=media_object_id)
            .returning(message_media.c.id)
        )
        return isinstance(attached, UUID)

    @staticmethod
    def _abort_attachment() -> None:
        raise RuntimeError("image_provider_attach_rejected")

    async def _mark_ready(self, object_id: UUID, account_id: UUID, stored: StoredMedia) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                return await MediaRepository(session).mark_ready(
                    object_id=object_id,
                    account_id=account_id,
                    stored=stored,
                    ready_at=self._now(),
                )
        except Exception:
            return False

    async def _expire_unattached_original(
        self,
        object_id: UUID,
        account_id: UUID,
        code: str,
    ) -> None:
        """Make an unattached original immediately eligible for durable cleanup."""

        with suppress(Exception):
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(media_objects)
                    .where(
                        media_objects.c.id == object_id,
                        media_objects.c.account_id == account_id,
                        media_objects.c.object_kind == "original",
                        media_objects.c.status == "ready",
                    )
                    .values(
                        expires_at=self._now(),
                        validation_error_code=code,
                    )
                )

    async def _discard_pending(
        self,
        object_id: UUID,
        account_id: UUID,
        stored: StoredMedia,
    ) -> None:
        await self._delete_stored(stored)
        await self._reject(object_id, account_id, "image_ingest_cancelled")

    async def _discard_provider_attempt(
        self,
        provider_id: UUID,
        account_id: UUID,
        provider: StoredMedia | None,
        original_id: UUID,
        code: str,
    ) -> None:
        if provider is not None:
            await self._delete_stored(provider)
        await self._reject(provider_id, account_id, code)
        await self._expire_unattached_original(original_id, account_id, code)

    async def _write_in_thread(
        self,
        write: Callable[[], StoredMedia],
        *,
        object_id: UUID,
        account_id: UUID,
    ) -> StoredMedia:
        """Wait out a cancelled thread write, then hash-delete its completed file."""

        task = asyncio.create_task(asyncio.to_thread(write))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stored: StoredMedia | None = None
            with suppress(Exception):
                stored = await asyncio.shield(task)
            if stored is not None:
                await asyncio.shield(self._delete_stored(stored))
            await asyncio.shield(self._reject(object_id, account_id, "image_ingest_cancelled"))
            raise

    async def _reject(self, object_id: UUID, account_id: UUID, code: str) -> None:
        with suppress(Exception):
            async with self._session_factory() as session, session.begin():
                await MediaRepository(session).mark_rejected(
                    object_id=object_id,
                    account_id=account_id,
                    error_code=code,
                )

    async def _reject_terminal(
        self,
        *,
        request: TelegramImageDownloadRequest,
        target: TelegramMediaBinding,
        object_id: UUID,
        code: str,
    ) -> None:
        """Persist one terminal validation outcome on the exact current media slot."""

        with suppress(Exception):
            async with self._session_factory() as session, session.begin():
                rejected = await MediaRepository(session).mark_rejected(
                    object_id=object_id,
                    account_id=target.account_id,
                    error_code=code,
                )
                if rejected:
                    await self._attach_if_current(
                        session,
                        request=request,
                        target=target,
                        media_object_id=object_id,
                    )

    async def _delete_stored(self, stored: StoredMedia) -> None:
        with suppress(Exception):
            await asyncio.to_thread(
                self._store.delete_verified,
                storage_key=stored.storage_key,
                expected_sha256=stored.sha256,
            )


__all__ = [
    "TelegramImageIngestOutcome",
    "TelegramImageIngestStatus",
    "TelegramImageIngestionService",
]
