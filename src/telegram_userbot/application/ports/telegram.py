"""Telegram side-effect port kept free of Telethon types."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue


@dataclass(frozen=True, slots=True)
class TelegramTextRequest:
    account_id: AccountId
    conversation_id: ConversationId
    run_id: RunId
    random_id: int
    text: SensitiveValue[str]


@dataclass(frozen=True, slots=True)
class TelegramSendReceipt:
    telegram_message_id: int


@dataclass(frozen=True, slots=True)
class TelegramReadRequest:
    account_id: AccountId
    conversation_id: ConversationId
    max_telegram_message_id: int


@dataclass(frozen=True, slots=True)
class TelegramReadReceipt:
    max_telegram_message_id: int


class TelegramTypingAction(StrEnum):
    START = "start"
    REFRESH = "refresh"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class TelegramTypingRequest:
    account_id: AccountId
    conversation_id: ConversationId
    action: TelegramTypingAction


class TelegramGatewayError(RuntimeError):
    """Content-free error safe to persist in attempt records."""

    error_code = "telegram_error"


class TelegramFloodWaitError(TelegramGatewayError):
    error_code = "flood_wait"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(self.error_code)
        self.retry_after_seconds = retry_after_seconds


class TelegramTransientError(TelegramGatewayError):
    error_code = "transient"


class TelegramPermanentError(TelegramGatewayError):
    error_code = "permanent"


class TelegramSendUnknownError(TelegramGatewayError):
    error_code = "send_unknown"


@runtime_checkable
class TelegramGateway(Protocol):
    async def send_text(self, request: TelegramTextRequest) -> TelegramSendReceipt: ...

    async def acknowledge_read(self, request: TelegramReadRequest) -> TelegramReadReceipt: ...

    async def set_typing(self, request: TelegramTypingRequest) -> None: ...
