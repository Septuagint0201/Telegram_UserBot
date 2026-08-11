"""Deterministic Telegram fake used by replay and crash-recovery tests."""

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from telegram_userbot.application.ports.telegram import (
    TelegramFloodWaitError,
    TelegramGateway,
    TelegramPermanentError,
    TelegramReadReceipt,
    TelegramReadRequest,
    TelegramSendReceipt,
    TelegramSendUnknownError,
    TelegramTextRequest,
    TelegramTransientError,
    TelegramTypingRequest,
)


class FakeSendOutcome(StrEnum):
    SUCCESS = "success"
    FLOOD_WAIT = "flood_wait"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN_BEFORE_ACCEPT = "unknown_before_accept"
    UNKNOWN_AFTER_ACCEPT = "unknown_after_accept"


@dataclass(frozen=True, slots=True)
class FakeAcceptedSend:
    random_id: int
    telegram_message_id: int
    text: str


@dataclass(slots=True)
class ReplayTelegramGateway(TelegramGateway):
    outcomes: deque[FakeSendOutcome] = field(default_factory=deque)
    accepted_by_random_id: dict[int, FakeAcceptedSend] = field(default_factory=dict)
    send_requests: list[TelegramTextRequest] = field(default_factory=list)
    read_requests: list[TelegramReadRequest] = field(default_factory=list)
    typing_requests: list[TelegramTypingRequest] = field(default_factory=list)
    next_message_id: int = 1000

    async def send_text(self, request: TelegramTextRequest) -> TelegramSendReceipt:
        self.send_requests.append(request)
        accepted = self.accepted_by_random_id.get(request.random_id)
        if accepted is not None:
            return TelegramSendReceipt(accepted.telegram_message_id)
        outcome = self.outcomes.popleft() if self.outcomes else FakeSendOutcome.SUCCESS
        if outcome is FakeSendOutcome.FLOOD_WAIT:
            raise TelegramFloodWaitError(7)
        if outcome is FakeSendOutcome.TRANSIENT:
            raise TelegramTransientError("fake_transient")
        if outcome is FakeSendOutcome.PERMANENT:
            raise TelegramPermanentError("fake_permanent")
        if outcome is FakeSendOutcome.UNKNOWN_BEFORE_ACCEPT:
            raise TelegramSendUnknownError("fake_unknown_before_accept")

        telegram_message_id = self.next_message_id
        self.next_message_id += 1
        self.accepted_by_random_id[request.random_id] = FakeAcceptedSend(
            request.random_id,
            telegram_message_id,
            request.text.reveal_for_use(),
        )
        if outcome is FakeSendOutcome.UNKNOWN_AFTER_ACCEPT:
            raise TelegramSendUnknownError("fake_unknown_after_accept")
        return TelegramSendReceipt(telegram_message_id)

    async def acknowledge_read(self, request: TelegramReadRequest) -> TelegramReadReceipt:
        self.read_requests.append(request)
        return TelegramReadReceipt(request.max_telegram_message_id)

    async def set_typing(self, request: TelegramTypingRequest) -> None:
        self.typing_requests.append(request)

    def observed_message_id(self, random_id: int) -> int | None:
        accepted = self.accepted_by_random_id.get(random_id)
        return None if accepted is None else accepted.telegram_message_id
