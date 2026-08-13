from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.context_repository import (
    ContextSummaryRecord,
    PreviewChallenge,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.telegram_bot import (
    ControlBotContextController,
    PreviewDeliveryResult,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 1, tzinfo=UTC)
ADMIN = 42
TARGET = "ct_abcdefghijklmnopqrstuvwxyz"


class BackendFake:
    def __init__(self) -> None:
        self.confirmed = False
        self.calls: list[str] = []

    async def summary(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> ContextSummaryRecord | None:
        self.calls.append("summary")
        assert (admin_id, bot_chat_id, target_token, now) == (ADMIN, ADMIN, TARGET, NOW)
        return ContextSummaryRecord(
            UUID(int=1),
            "00000000",
            "reactive_reply",
            "main_ai",
            24_000,
            1_234,
            1,
            2,
            "degraded",
            "builder-v1",
            "context-v1",
            "retrieval-v1",
            "utf8_bytes_v1",
            {"current": 1, "recent": 2},
            {"current": 200, "recent": 300},
            NOW,
        )

    async def issue_preview(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> PreviewChallenge:
        self.calls.append("issue")
        return PreviewChallenge(
            UUID(int=2),
            "00000000",
            NOW + timedelta(minutes=5),
            SensitiveValue("preview_confirmation_token"),
        )

    async def confirm_preview(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        confirmation_token: SensitiveValue[str],
        now: datetime,
    ) -> tuple[PreviewRequestRecord, tuple[SensitiveValue[str], ...]] | None:
        self.calls.append("confirm")
        if self.confirmed or confirmation_token.reveal_for_use() != "preview_confirmation_token":
            return None
        self.confirmed = True
        return (
            PreviewRequestRecord(
                UUID(int=2),
                UUID(int=1),
                b"a" * 32,
                b"b" * 32,
                "confirmed",
                ADMIN,
                ADMIN,
                "control-bot",
            ),
            (SensitiveValue("SYNTHETIC_REDACTED_PREVIEW"),),
        )

    async def deliver_preview(
        self,
        *,
        request: PreviewRequestRecord,
        chunks: tuple[SensitiveValue[str], ...],
        now: datetime,
    ) -> PreviewDeliveryResult:
        self.calls.append("deliver")
        assert "SYNTHETIC_REDACTED_PREVIEW" not in repr(chunks)
        return PreviewDeliveryResult("delivered", 1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_command_returns_metadata_only() -> None:
    backend = BackendFake()
    controller = ControlBotContextController(allowed_admin_ids=frozenset({ADMIN}), backend=backend)
    reply = await controller.handle(
        admin_id=ADMIN,
        bot_chat_id=ADMIN,
        message_text=f"/context {TARGET}",
        now=NOW,
    )
    assert "Budget: 1234/24000" in reply.text
    assert "current=1/200t" in reply.text
    assert "SYNTHETIC_REDACTED_PREVIEW" not in reply.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_preview_requires_private_admin_and_one_time_confirmation() -> None:
    backend = BackendFake()
    controller = ControlBotContextController(allowed_admin_ids=frozenset({ADMIN}), backend=backend)
    rejected = await controller.handle(
        admin_id=ADMIN,
        bot_chat_id=-100,
        message_text=f"/context_preview {TARGET}",
        now=NOW,
    )
    assert rejected.text == "Request rejected."
    challenge = await controller.handle(
        admin_id=ADMIN,
        bot_chat_id=ADMIN,
        message_text=f"/context_preview {TARGET}",
        now=NOW,
    )
    assert "Telegram copies" in challenge.text
    assert challenge.callback_token is not None
    assert "preview_confirmation_token" not in challenge.text
    assert "preview_confirmation_token" not in repr(challenge)
    confirmed = await controller.confirm_callback(
        admin_id=ADMIN,
        bot_chat_id=ADMIN,
        confirmation_token=challenge.callback_token,
        now=NOW,
    )
    assert "best-effort deletion" in confirmed.text
    replay = await controller.confirm_callback(
        admin_id=ADMIN,
        bot_chat_id=ADMIN,
        confirmation_token=SensitiveValue("preview_confirmation_token"),
        now=NOW,
    )
    assert "already used" in replay.text
    assert backend.calls == ["issue", "confirm", "deliver", "confirm"]


class SendUnknownBackend(BackendFake):
    async def deliver_preview(
        self,
        *,
        request: PreviewRequestRecord,
        chunks: tuple[SensitiveValue[str], ...],
        now: datetime,
    ) -> PreviewDeliveryResult:
        return PreviewDeliveryResult("send_unknown", 0, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_send_unknown_is_explicit_and_not_retried() -> None:
    controller = ControlBotContextController(
        allowed_admin_ids=frozenset({ADMIN}), backend=SendUnknownBackend()
    )
    reply = await controller.confirm_callback(
        admin_id=ADMIN,
        bot_chat_id=ADMIN,
        confirmation_token=SensitiveValue("preview_confirmation_token"),
        now=NOW,
    )
    assert "send_unknown" in reply.text
    assert "not be retried" in reply.text
