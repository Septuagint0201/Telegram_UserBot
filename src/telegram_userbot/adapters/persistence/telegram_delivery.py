"""Execute one already-durable Telegram attempt and classify its result."""

from datetime import datetime, timedelta

from telegram_userbot.adapters.persistence.records import (
    AttemptCompletionRecord,
    OutboundIntentRecord,
)
from telegram_userbot.application.ports.telegram import (
    TelegramFloodWaitError,
    TelegramGateway,
    TelegramPermanentError,
    TelegramSendUnknownError,
    TelegramTextRequest,
    TelegramTransientError,
)
from telegram_userbot.domain.messaging import AttemptOutcome
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue


class TelegramDeliveryService:
    """No persistence is hidden here: claim/start must be committed before calling."""

    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway

    async def send_prepared(
        self, *, intent: OutboundIntentRecord, now: datetime
    ) -> AttemptCompletionRecord:
        request = TelegramTextRequest(
            account_id=AccountId(intent.account_id),
            conversation_id=ConversationId(intent.conversation_id),
            run_id=RunId(intent.model_run_id or intent.delivery_group_id),
            random_id=intent.telegram_random_id,
            text=SensitiveValue(intent.text_content),
        )
        try:
            receipt = await self._gateway.send_text(request)
        except TelegramFloodWaitError as error:
            return AttemptCompletionRecord(
                AttemptOutcome.FLOOD_WAIT,
                now,
                error_code=error.error_code,
                retry_after_seconds=error.retry_after_seconds,
                next_attempt_at=now + timedelta(seconds=error.retry_after_seconds),
            )
        except TelegramTransientError as error:
            return AttemptCompletionRecord(
                AttemptOutcome.TRANSIENT,
                now,
                error_code=error.error_code,
                next_attempt_at=now + timedelta(seconds=5),
            )
        except TelegramPermanentError as error:
            return AttemptCompletionRecord(
                AttemptOutcome.PERMANENT, now, error_code=error.error_code
            )
        except TelegramSendUnknownError as error:
            return AttemptCompletionRecord(AttemptOutcome.UNKNOWN, now, error_code=error.error_code)
        return AttemptCompletionRecord(
            AttemptOutcome.SUCCEEDED,
            now,
            telegram_message_id=receipt.telegram_message_id,
        )
