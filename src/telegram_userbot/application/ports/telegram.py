"""Telegram side-effect port; no adapter exists in M0."""

from dataclasses import dataclass
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


@runtime_checkable
class TelegramGateway(Protocol):
    async def send_text(self, request: TelegramTextRequest) -> TelegramSendReceipt: ...
