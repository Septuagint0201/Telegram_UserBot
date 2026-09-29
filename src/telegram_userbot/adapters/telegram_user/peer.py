"""Telethon entity admission and DB-bound outbound peer construction."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import PeerAdmission
from telegram_userbot.adapters.telegram_user.telethon_updates import TelethonUpdateScope
from telegram_userbot.application.ports.telegram_peer import (
    TelegramPeerBinding,
    TelegramPrivatePeerObservation,
)
from telegram_userbot.domain.messaging import PeerKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId


class TelethonEntityClient(Protocol):
    async def get_entity(self, entity: object) -> object: ...


PrivatePeerAdmitter = Callable[[TelegramPrivatePeerObservation], Awaitable[TelegramPeerBinding]]
DeletedMessagePeerLookup = Callable[[int], Awaitable[TelegramPeerBinding | None]]
ExistingPeerLookup = Callable[[int], Awaitable[TelegramPeerBinding | None]]
OutboundPeerLookup = Callable[[UUID, UUID], Awaitable[TelegramPeerBinding | None]]


class TelethonPeerResolverError(RuntimeError):
    """Stable, content-free peer resolution failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TelethonPeerAdmissionResolver:
    def __init__(  # noqa: PLR0913 - each lookup is a distinct trust boundary
        self,
        *,
        client: TelethonEntityClient,
        account_id: UUID,
        managed_telegram_user_id: int,
        admit_private: PrivatePeerAdmitter,
        lookup_existing: ExistingPeerLookup,
        lookup_deleted_message: DeletedMessagePeerLookup,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._managed_telegram_user_id = managed_telegram_user_id
        self._admit_private = admit_private
        self._lookup_existing = lookup_existing
        self._lookup_deleted_message = lookup_deleted_message
        self._now = now

    async def __call__(  # noqa: PLR0911 - unsupported peer classes fail closed explicitly
        self, scope: TelethonUpdateScope
    ) -> PeerAdmission:
        chat_id = scope.telegram_chat_id
        if scope.peer_kind_hint is PeerKind.SELF:
            return PeerAdmission(self._account_id, None, PeerKind.SELF, chat_id)
        if scope.peer_kind_hint in {PeerKind.GROUP, PeerKind.CHANNEL}:
            return PeerAdmission(self._account_id, None, scope.peer_kind_hint, chat_id)
        if chat_id is None:
            binding = await self._lookup_deleted_message(scope.telegram_message_id)
            return (
                self._from_binding(binding)
                if binding is not None
                else PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, None)
            )
        if scope.peer_kind_hint is not PeerKind.PRIVATE_USER or chat_id <= 0:
            return PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, chat_id)
        sender_id = scope.sender_telegram_peer_id
        if sender_id is not None and sender_id not in {
            chat_id,
            self._managed_telegram_user_id,
        }:
            return PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, chat_id)
        existing = await self._lookup_existing(chat_id)
        if existing is not None:
            return self._from_binding(existing, expected_telegram_user_id=chat_id)
        try:
            entity = await self._client.get_entity(types.PeerUser(chat_id))
        except Exception:
            return PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, chat_id)
        if not isinstance(entity, types.User) or entity.id != chat_id:
            return PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, chat_id)
        if entity.bot is True:
            return PeerAdmission(self._account_id, None, PeerKind.BOT, chat_id)
        if entity.is_self is True or entity.id == self._managed_telegram_user_id:
            return PeerAdmission(self._account_id, None, PeerKind.SELF, chat_id)
        if not isinstance(entity.access_hash, int) or isinstance(entity.access_hash, bool):
            return PeerAdmission(self._account_id, None, PeerKind.UNKNOWN, chat_id)
        username = entity.username.strip()[:64] if isinstance(entity.username, str) else None
        username = username or None
        names = tuple(
            value.strip()
            for value in (entity.first_name, entity.last_name)
            if isinstance(value, str) and value.strip()
        )
        display_name = " ".join(names)[:255] or None
        binding = await self._admit_private(
            TelegramPrivatePeerObservation(
                account_id=self._account_id,
                managed_telegram_user_id=self._managed_telegram_user_id,
                telegram_user_id=entity.id,
                access_hash=entity.access_hash,
                username=username,
                display_name=display_name,
                observed_is_contact=entity.contact is True,
                observed_at=self._now(),
            )
        )
        return self._from_binding(binding, expected_telegram_user_id=chat_id)

    def _from_binding(
        self,
        binding: TelegramPeerBinding,
        *,
        expected_telegram_user_id: int | None = None,
    ) -> PeerAdmission:
        if binding.account_id != self._account_id:
            raise TelethonPeerResolverError("TELEGRAM_PEER_BINDING_ACCOUNT_MISMATCH")
        if (
            expected_telegram_user_id is not None
            and binding.telegram_user_id != expected_telegram_user_id
        ):
            raise TelethonPeerResolverError("TELEGRAM_PEER_BINDING_IDENTITY_MISMATCH")
        return PeerAdmission(
            binding.account_id,
            binding.conversation_id,
            PeerKind.PRIVATE_USER,
            binding.telegram_user_id,
        )


class TelethonBoundPeerResolver:
    """Construct an InputPeer only from an exact current-account DB binding."""

    def __init__(self, lookup: OutboundPeerLookup) -> None:
        self._lookup = lookup

    async def __call__(self, account_id: AccountId, conversation_id: ConversationId) -> object:
        binding = await self._lookup(account_id.value, conversation_id.value)
        if binding is None:
            raise TelethonPeerResolverError("TELEGRAM_OUTBOUND_PEER_NOT_BOUND")
        if (
            binding.account_id != account_id.value
            or binding.conversation_id != conversation_id.value
        ):
            raise TelethonPeerResolverError("TELEGRAM_OUTBOUND_PEER_SCOPE_MISMATCH")
        return types.InputPeerUser(binding.telegram_user_id, binding.access_hash)


__all__ = [
    "TelethonBoundPeerResolver",
    "TelethonEntityClient",
    "TelethonPeerAdmissionResolver",
    "TelethonPeerResolverError",
]
