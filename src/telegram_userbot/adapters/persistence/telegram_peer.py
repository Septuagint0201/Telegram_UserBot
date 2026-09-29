"""Account-scoped PostgreSQL admission and lookup for Telegram private peers."""

from collections.abc import Callable
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    account_peers,
    accounts,
    contacts,
    conversations,
    message_media,
    message_revisions,
    messages,
    telegram_peers,
)
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.application.ports.telegram_peer import (
    TelegramMediaBinding,
    TelegramPeerBinding,
    TelegramPrivatePeerObservation,
)
from telegram_userbot.domain.messaging import MediaKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId


class TelegramPeerAdmissionError(RuntimeError):
    """Stable, content-free admission failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PostgresTelegramPeerRepository:
    def __init__(self, session: AsyncSession, *, new_uuid: Callable[[], UUID]) -> None:
        self._session = session
        self._new_uuid = new_uuid

    async def admit_private(
        self, observation: TelegramPrivatePeerObservation
    ) -> TelegramPeerBinding:
        account = (
            (
                await self._session.execute(
                    select(
                        accounts.c.telegram_user_id,
                        accounts.c.status,
                        func.scope_metadata_blocked(
                            "account_peers",
                            func.jsonb_build_object(
                                "account_id",
                                accounts.c.id,
                                "peer_id",
                                select(telegram_peers.c.id)
                                .where(
                                    telegram_peers.c.peer_type == "user",
                                    telegram_peers.c.telegram_peer_id
                                    == observation.telegram_user_id,
                                )
                                .scalar_subquery(),
                            ),
                        ).label("erasure_blocked"),
                    )
                    .where(
                        accounts.c.id == observation.account_id,
                        accounts.c.deleted_at.is_(None),
                    )
                    .with_for_update(key_share=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if account is None or account["status"] != "active":
            raise TelegramPeerAdmissionError("TELEGRAM_ACCOUNT_NOT_ACTIVE")
        if account["telegram_user_id"] != observation.managed_telegram_user_id:
            raise TelegramPeerAdmissionError("TELEGRAM_ACCOUNT_IDENTITY_MISMATCH")
        if account.get("erasure_blocked", False):
            raise TelegramPeerAdmissionError("TELEGRAM_CONTACT_NOT_ADMISSIBLE")

        peer_id = await self._session.scalar(
            postgresql_insert(telegram_peers)
            .values(
                id=self._new_uuid(),
                peer_type="user",
                telegram_peer_id=observation.telegram_user_id,
                is_bot=False,
                created_at=observation.observed_at,
            )
            .on_conflict_do_update(
                index_elements=[telegram_peers.c.peer_type, telegram_peers.c.telegram_peer_id],
                set_={"is_bot": False},
            )
            .returning(telegram_peers.c.id)
        )
        if not isinstance(peer_id, UUID):
            raise TelegramPeerAdmissionError("TELEGRAM_PEER_UPSERT_FAILED")

        account_peer_id = await self._session.scalar(
            postgresql_insert(account_peers)
            .values(
                id=self._new_uuid(),
                account_id=observation.account_id,
                peer_id=peer_id,
                access_hash=observation.access_hash,
                username=observation.username,
                display_name=observation.display_name,
                observed_is_contact=observation.observed_is_contact,
                last_observed_at=observation.observed_at,
                metadata_schema_version=1,
                metadata={},
            )
            .on_conflict_do_update(
                index_elements=[account_peers.c.account_id, account_peers.c.peer_id],
                set_={
                    "access_hash": observation.access_hash,
                    "username": observation.username,
                    "display_name": observation.display_name,
                    "observed_is_contact": observation.observed_is_contact,
                    "last_observed_at": observation.observed_at,
                },
            )
            .returning(account_peers.c.id)
        )
        if not isinstance(account_peer_id, UUID):
            raise TelegramPeerAdmissionError("TELEGRAM_ACCOUNT_PEER_UPSERT_FAILED")

        await self._session.execute(
            postgresql_insert(contacts)
            .values(
                id=self._new_uuid(),
                account_id=observation.account_id,
                account_peer_id=account_peer_id,
                automation_status="review",
                proactive_enabled=False,
                created_at=observation.observed_at,
                updated_at=observation.observed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[contacts.c.account_id, contacts.c.account_peer_id]
            )
        )
        contact = (
            (
                await self._session.execute(
                    select(
                        contacts.c.id,
                        contacts.c.automation_status,
                        contacts.c.deleted_at,
                    )
                    .where(
                        contacts.c.account_id == observation.account_id,
                        contacts.c.account_peer_id == account_peer_id,
                    )
                    .with_for_update(of=contacts)
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            contact is None
            or contact["deleted_at"] is not None
            or contact["automation_status"] == "deleting"
        ):
            raise TelegramPeerAdmissionError("TELEGRAM_CONTACT_NOT_ADMISSIBLE")
        contact_id = cast(UUID, contact["id"])

        await self._session.execute(
            postgresql_insert(conversations)
            .values(
                id=self._new_uuid(),
                account_id=observation.account_id,
                contact_id=contact_id,
                account_peer_id=account_peer_id,
                telegram_chat_id=observation.telegram_user_id,
                created_at=observation.observed_at,
                updated_at=observation.observed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[conversations.c.account_id, conversations.c.account_peer_id]
            )
        )
        conversation = (
            (
                await self._session.execute(
                    select(conversations.c.id, conversations.c.deleted_at).where(
                        conversations.c.account_id == observation.account_id,
                        conversations.c.account_peer_id == account_peer_id,
                        conversations.c.telegram_chat_id == observation.telegram_user_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if conversation is None or conversation["deleted_at"] is not None:
            raise TelegramPeerAdmissionError("TELEGRAM_CONVERSATION_NOT_ADMISSIBLE")
        return TelegramPeerBinding(
            observation.account_id,
            cast(UUID, conversation["id"]),
            observation.telegram_user_id,
            observation.access_hash,
        )

    async def find_private_for_deleted_message(
        self, *, account_id: UUID, telegram_message_id: int
    ) -> TelegramPeerBinding | None:
        rows = (
            (
                await self._session.execute(
                    self._binding_query(include_messages=True)
                    .where(
                        messages.c.account_id == account_id,
                        messages.c.telegram_message_id == telegram_message_id,
                    )
                    .limit(2)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            return None
        return self._binding(rows[0])

    async def find_private_by_chat(
        self, *, account_id: UUID, telegram_user_id: int
    ) -> TelegramPeerBinding | None:
        row = (
            (
                await self._session.execute(
                    self._binding_query(include_messages=False)
                    .where(
                        conversations.c.account_id == account_id,
                        telegram_peers.c.telegram_peer_id == telegram_user_id,
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._binding(row)

    async def resolve_outbound(
        self, *, account_id: UUID, conversation_id: UUID
    ) -> TelegramPeerBinding | None:
        row = (
            (
                await self._session.execute(
                    self._binding_query(include_messages=False)
                    .where(
                        conversations.c.account_id == account_id,
                        conversations.c.id == conversation_id,
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._binding(row)

    @staticmethod
    def _binding_query(*, include_messages: bool):  # type: ignore[no-untyped-def]
        source = (
            conversations.join(accounts, conversations.c.account_id == accounts.c.id)
            .join(
                contacts,
                (conversations.c.contact_id == contacts.c.id)
                & (conversations.c.account_id == contacts.c.account_id),
            )
            .join(
                account_peers,
                (conversations.c.account_peer_id == account_peers.c.id)
                & (conversations.c.account_id == account_peers.c.account_id),
            )
            .join(telegram_peers, account_peers.c.peer_id == telegram_peers.c.id)
        )
        if include_messages:
            source = source.join(
                messages,
                (messages.c.conversation_id == conversations.c.id)
                & (messages.c.account_id == conversations.c.account_id),
            )
        return (
            select(
                accounts.c.id.label("account_id"),
                conversations.c.id.label("conversation_id"),
                telegram_peers.c.telegram_peer_id,
                account_peers.c.access_hash,
            )
            .select_from(source)
            .where(
                accounts.c.status == "active",
                accounts.c.deleted_at.is_(None),
                contacts.c.deleted_at.is_(None),
                contacts.c.automation_status != "deleting",
                conversations.c.deleted_at.is_(None),
                telegram_peers.c.peer_type == "user",
                telegram_peers.c.is_bot.is_(False),
                account_peers.c.access_hash.is_not(None),
                conversations.c.telegram_chat_id == telegram_peers.c.telegram_peer_id,
            )
        )

    @staticmethod
    def _binding(row: object) -> TelegramPeerBinding:
        values = cast(dict[str, object], row)
        return TelegramPeerBinding(
            cast(UUID, values["account_id"]),
            cast(UUID, values["conversation_id"]),
            cast(int, values["telegram_peer_id"]),
            cast(int, values["access_hash"]),
        )

    async def resolve_image(
        self, request: TelegramImageDownloadRequest
    ) -> TelegramMediaBinding | None:
        row = (
            (
                await self._session.execute(
                    select(
                        messages.c.account_id,
                        messages.c.conversation_id,
                        messages.c.id.label("message_id"),
                        message_revisions.c.revision_no,
                        message_media.c.position,
                        message_media.c.media_kind,
                        message_media.c.telegram_file_ref,
                        message_media.c.declared_mime,
                        message_media.c.declared_size,
                    )
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
                        messages.c.account_id == request.account_id.value,
                        messages.c.conversation_id == request.conversation_id.value,
                        messages.c.id == request.message_id.value,
                        messages.c.current_revision_no == request.revision_no,
                        messages.c.deleted_at.is_(None),
                        messages.c.is_tombstone.is_(False),
                        message_revisions.c.revision_no == request.revision_no,
                        message_revisions.c.redacted_at.is_(None),
                        message_media.c.position == request.position,
                        message_media.c.media_kind.in_(("photo", "image_document")),
                        message_media.c.media_object_id.is_(None),
                        message_media.c.telegram_file_ref.is_not(None),
                        message_media.c.declared_mime.in_(
                            ("image/jpeg", "image/png", "image/webp")
                        ),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return TelegramMediaBinding(
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            message_id=cast(UUID, row["message_id"]),
            revision_no=cast(int, row["revision_no"]),
            position=cast(int, row["position"]),
            kind=MediaKind(cast(str, row["media_kind"])),
            opaque_file_reference=cast(str, row["telegram_file_ref"]),
            declared_mime=cast(str, row["declared_mime"]),
            declared_size=cast(int | None, row["declared_size"]),
        )

    async def list_pending_images(
        self,
        *,
        account_id: UUID,
        limit: int = 50,
    ) -> tuple[TelegramImageDownloadRequest, ...]:
        """List current image slots that still need their provider-safe copy.

        The canonical message/media row is the durable recovery marker. A crash
        after the Telegram watermark commits but before the filesystem write can
        therefore be repaired without replaying or advancing any Telegram cursor.
        """

        if limit <= 0 or limit > 100:
            raise ValueError("Telegram pending-image limit is invalid")
        rows = (
            (
                await self._session.execute(
                    select(
                        messages.c.account_id,
                        messages.c.conversation_id,
                        messages.c.id.label("message_id"),
                        message_revisions.c.revision_no,
                        message_media.c.position,
                    )
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
                        messages.c.account_id == account_id,
                        messages.c.current_revision_no == message_revisions.c.revision_no,
                        messages.c.deleted_at.is_(None),
                        messages.c.is_tombstone.is_(False),
                        message_revisions.c.redacted_at.is_(None),
                        message_media.c.media_kind.in_(("photo", "image_document")),
                        message_media.c.media_object_id.is_(None),
                        message_media.c.telegram_file_ref.is_not(None),
                        message_media.c.declared_mime.in_(
                            ("image/jpeg", "image/png", "image/webp")
                        ),
                    )
                    .order_by(
                        message_revisions.c.created_at,
                        messages.c.id,
                        message_media.c.position,
                    )
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return tuple(
            TelegramImageDownloadRequest(
                AccountId(cast(UUID, row["account_id"])),
                ConversationId(cast(UUID, row["conversation_id"])),
                MessageId(cast(UUID, row["message_id"])),
                cast(int, row["revision_no"]),
                cast(int, row["position"]),
            )
            for row in rows
        )


__all__ = ["PostgresTelegramPeerRepository", "TelegramPeerAdmissionError"]
