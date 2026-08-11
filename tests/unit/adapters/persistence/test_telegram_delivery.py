from collections import deque
from datetime import UTC, datetime
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.records import OutboundIntentRecord
from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.telegram_user import FakeSendOutcome, ReplayTelegramGateway
from telegram_userbot.domain.messaging import AttemptOutcome, payload_sha256

NOW = datetime(2030, 3, 4, tzinfo=UTC)


def intent() -> OutboundIntentRecord:
    return OutboundIntentRecord(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        UUID(int=4),
        UUID(int=5),
        0,
        77,
        "synthetic output",
        payload_sha256("synthetic output"),
        "sending",
        None,
        1,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fake_outcome", "attempt_outcome"),
    [
        (FakeSendOutcome.SUCCESS, AttemptOutcome.SUCCEEDED),
        (FakeSendOutcome.FLOOD_WAIT, AttemptOutcome.FLOOD_WAIT),
        (FakeSendOutcome.TRANSIENT, AttemptOutcome.TRANSIENT),
        (FakeSendOutcome.PERMANENT, AttemptOutcome.PERMANENT),
        (FakeSendOutcome.UNKNOWN_BEFORE_ACCEPT, AttemptOutcome.UNKNOWN),
    ],
)
async def test_delivery_classifies_each_gateway_outcome(
    fake_outcome: FakeSendOutcome, attempt_outcome: AttemptOutcome
) -> None:
    gateway = ReplayTelegramGateway(outcomes=deque((fake_outcome,)))
    service = TelegramDeliveryService(gateway)
    completion = await service.send_prepared(intent=intent(), now=NOW)
    assert completion.outcome == attempt_outcome
