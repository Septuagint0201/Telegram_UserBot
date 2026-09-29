"""Framework-neutral records for account-scoped Telegram peer and media lookup."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from telegram_userbot.domain.messaging import MediaKind
from telegram_userbot.domain.shared.time import require_aware


@dataclass(frozen=True, slots=True)
class TelegramPrivatePeerObservation:
    account_id: UUID
    managed_telegram_user_id: int
    telegram_user_id: int
    access_hash: int
    username: str | None
    display_name: str | None
    observed_is_contact: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.account_id.int == 0:
            raise ValueError("Telegram peer account identity is invalid")
        if (
            type(self.managed_telegram_user_id) is not int
            or type(self.telegram_user_id) is not int
            or self.managed_telegram_user_id <= 0
            or self.telegram_user_id <= 0
            or self.managed_telegram_user_id == self.telegram_user_id
        ):
            raise ValueError("Telegram private peer identity is invalid")
        if type(self.access_hash) is not int or not -(1 << 63) <= self.access_hash < (1 << 63):
            raise ValueError("Telegram peer access hash is invalid")
        if self.username is not None and (
            not self.username or len(self.username) > 64 or self.username != self.username.strip()
        ):
            raise ValueError("Telegram peer username is invalid")
        if self.display_name is not None and (
            not self.display_name
            or len(self.display_name) > 255
            or self.display_name != self.display_name.strip()
        ):
            raise ValueError("Telegram peer display name is invalid")
        if type(self.observed_is_contact) is not bool:
            raise TypeError("Telegram contact observation must be boolean")
        object.__setattr__(self, "observed_at", require_aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class TelegramPeerBinding:
    account_id: UUID
    conversation_id: UUID
    telegram_user_id: int
    access_hash: int

    def __post_init__(self) -> None:
        if self.account_id.int == 0 or self.conversation_id.int == 0:
            raise ValueError("Telegram peer binding scope is invalid")
        if type(self.telegram_user_id) is not int or self.telegram_user_id <= 0:
            raise ValueError("Telegram peer binding identity is invalid")
        if type(self.access_hash) is not int or not -(1 << 63) <= self.access_hash < (1 << 63):
            raise ValueError("Telegram peer binding access hash is invalid")


@dataclass(frozen=True, slots=True)
class TelegramMediaBinding:
    account_id: UUID
    conversation_id: UUID
    message_id: UUID
    revision_no: int
    position: int
    kind: MediaKind
    opaque_file_reference: str
    declared_mime: str
    declared_size: int | None

    def __post_init__(self) -> None:
        if self.account_id.int == 0 or self.conversation_id.int == 0 or self.message_id.int == 0:
            raise ValueError("Telegram media binding scope is invalid")
        if self.revision_no <= 0 or self.position < 0 or not self.kind.image_download_eligible:
            raise ValueError("Telegram media binding source is invalid")
        if not self.opaque_file_reference or len(self.opaque_file_reference) > 16_384:
            raise ValueError("Telegram media reference is invalid")
        if self.declared_mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("Telegram media MIME is invalid")
        if self.declared_size is not None and self.declared_size < 0:
            raise ValueError("Telegram media declared size is invalid")


__all__ = [
    "TelegramMediaBinding",
    "TelegramPeerBinding",
    "TelegramPrivatePeerObservation",
]
