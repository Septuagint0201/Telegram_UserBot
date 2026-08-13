"""Telegram image download boundary kept free of Telethon types."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId


@dataclass(frozen=True, slots=True)
class TelegramImageDownloadRequest:
    account_id: AccountId
    conversation_id: ConversationId
    message_id: MessageId
    revision_no: int
    position: int

    def __post_init__(self) -> None:
        if self.revision_no <= 0 or self.position < 0:
            raise ValueError("image download source identity is invalid")


@runtime_checkable
class TelegramImageSource(Protocol):
    def iter_image(self, request: TelegramImageDownloadRequest) -> AsyncIterator[bytes]: ...
