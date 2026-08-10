"""Synthetic fixture factory; outputs are intentionally non-personal."""

from dataclasses import dataclass

from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId, PeerId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from tests.support.fakes import DeterministicIdFactory


@dataclass(frozen=True, slots=True)
class SyntheticMessage:
    account_id: AccountId
    peer_id: PeerId
    conversation_id: ConversationId
    message_id: MessageId
    text: SensitiveValue[str]


class SyntheticFactory:
    def __init__(self) -> None:
        self._ids = DeterministicIdFactory()
        self._message_number = 0

    def message(self) -> SyntheticMessage:
        self._message_number += 1
        return SyntheticMessage(
            account_id=self._ids.new(AccountId),
            peer_id=self._ids.new(PeerId),
            conversation_id=self._ids.new(ConversationId),
            message_id=self._ids.new(MessageId),
            text=SensitiveValue(f"SYNTHETIC_MESSAGE_{self._message_number:04d}"),
        )
