# ruff: noqa: PLR0913 - protocol fakes mirror explicit actor/chat/update bindings.

from datetime import UTC, datetime

import pytest

from telegram_userbot.adapters.telegram_bot import (
    ControlBotConversationController,
    ConversationCommandResult,
    ConversationStatusSummary,
)
from telegram_userbot.domain.conversation import BaseMode

NOW = datetime(2030, 4, 5, 6, 7, 8, tzinfo=UTC)
ADMIN_ID = 42
TARGET = "ct_abcdefghijklmnopqrstuvwxyz012345"


class BackendFake:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def set_mode(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        mode: BaseMode | None,
        now: datetime,
    ) -> ConversationCommandResult:
        self.calls.append(
            ("mode", admin_id, bot_chat_id, telegram_update_id, target_token, mode, now)
        )
        return ConversationCommandResult("MODE_CHANGED", True)

    async def set_pause(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        paused: bool,
        now: datetime,
    ) -> ConversationCommandResult:
        self.calls.append(
            ("pause", admin_id, bot_chat_id, telegram_update_id, target_token, paused, now)
        )
        return ConversationCommandResult("PAUSED" if paused else "RESUMED", True)

    async def execute(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        command: str,
        target_token: str,
        now: datetime,
    ) -> ConversationCommandResult:
        self.calls.append(
            ("execute", admin_id, bot_chat_id, telegram_update_id, command, target_token, now)
        )
        return ConversationCommandResult(command.upper(), True)

    async def status(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        now: datetime,
    ) -> ConversationCommandResult:
        self.calls.append(("status", admin_id, bot_chat_id, telegram_update_id, target_token, now))
        return ConversationCommandResult(
            "STATUS",
            False,
            ConversationStatusSummary(
                "Synthetic contact",
                BaseMode.AUTO,
                "conversation_override",
                "AUTO",
                "READY",
                None,
                None,
                2,
                3,
                4,
                1,
                "collecting",
                None,
            ),
        )


class StatusBackendFake(BackendFake):
    async def set_mode(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        mode: BaseMode | None,
        now: datetime,
    ) -> ConversationCommandResult:
        status = ConversationStatusSummary(
            "Synthetic contact",
            BaseMode.COPILOT,
            "conversation_override",
            "COPILOT",
            "BLOCKED",
            "GLOBAL_PAUSE",
            "MAIN_AI_UNAVAILABLE",
            7,
            8,
            9,
            2,
            None,
            "ready",
        )
        return ConversationCommandResult("STATUS", True, status)


class NoChangeBackendFake(BackendFake):
    async def set_pause(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        paused: bool,
        now: datetime,
    ) -> ConversationCommandResult:
        return ConversationCommandResult("ALREADY_PAUSED", False)


class ErrorBackendFake(BackendFake):
    async def set_mode(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        mode: BaseMode | None,
        now: datetime,
    ) -> ConversationCommandResult:
        raise RuntimeError("synthetic conflict")


class RejectedBackendFake(BackendFake):
    async def set_pause(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        paused: bool,
        now: datetime,
    ) -> ConversationCommandResult:
        return ConversationCommandResult("MODE_VERSION_CONFLICT", False, accepted=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversation_commands_route_account_and_opaque_contact_scope() -> None:
    backend = BackendFake()
    controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend
    )
    commands = (
        "/ai",
        f"/human {TARGET}",
        f"/copilot {TARGET}",
        f"/mode_inherit {TARGET}",
        "/pause",
        f"/resume {TARGET}",
        f"/draft {TARGET}",
        f"/reply_pending {TARGET}",
        f"/cancel {TARGET}",
        f"/takeover_end {TARGET}",
    )
    for update_id, command in enumerate(commands, start=1):
        reply = await controller.handle(
            admin_id=ADMIN_ID,
            bot_chat_id=ADMIN_ID,
            telegram_update_id=update_id,
            message_text=command,
            now=NOW,
        )
        assert reply.text.startswith("Completed:")
    assert backend.calls[0][4] is None
    assert backend.calls[1][4] == TARGET
    assert backend.calls[3][5] is None
    assert {call[4] for call in backend.calls[6:]} == {
        "draft",
        "reply_pending",
        "cancel",
        "takeover_end",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_is_content_free_and_does_not_mutate_backend() -> None:
    backend = BackendFake()
    controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend
    )
    reply = await controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=11,
        message_text=f"/status {TARGET}",
        now=NOW,
    )
    assert "Synthetic contact" in reply.text
    assert "Versions: account=2; mode=3; content=4" in reply.text
    assert "Unanswered: 1" in reply.text
    assert backend.calls == [("status", ADMIN_ID, ADMIN_ID, 11, TARGET, NOW)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_controller_rejects_display_name_invalid_shape_and_non_admin() -> None:
    backend = BackendFake()
    controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend
    )
    rejected = (
        (ADMIN_ID, "/ai Alice Smith"),
        (ADMIN_ID, "/draft alice"),
        (ADMIN_ID, "/mode_inherit"),
        (ADMIN_ID, "/reply_pending"),
        (99, f"/ai {TARGET}"),
        (ADMIN_ID, "not a command"),
    )
    for update_id, (admin_id, command) in enumerate(rejected, start=20):
        reply = await controller.handle(
            admin_id=admin_id,
            bot_chat_id=admin_id,
            telegram_update_id=update_id,
            message_text=command,
            now=NOW,
        )
        assert "Completed:" not in reply.text
    group_reply = await controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=-100123,
        telegram_update_id=99,
        message_text="/pause",
        now=NOW,
    )
    assert "private chat" in group_reply.text
    assert backend.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_controller_handles_constructor_syntax_results_and_backend_conflict() -> None:
    with pytest.raises(ValueError, match="admin allowlist"):
        ControlBotConversationController(allowed_admin_ids=frozenset(), backend=BackendFake())

    controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=BackendFake()
    )
    invalid = await controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=30,
        message_text="/ai '",
        now=NOW,
    )
    assert invalid.text == "Invalid command syntax."
    unknown = await controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=31,
        message_text="/unknown",
        now=NOW,
    )
    assert unknown.text.startswith("Unknown conversation command")

    status_controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=StatusBackendFake()
    )
    status = await status_controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=32,
        message_text=f"/ai {TARGET}",
        now=NOW,
    )
    assert "Pause: GLOBAL_PAUSE" in status.text
    assert "Block: MAIN_AI_UNAVAILABLE" in status.text
    assert "Active turn:" not in status.text
    assert "Active draft: ready" in status.text

    no_change_controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=NoChangeBackendFake()
    )
    no_change = await no_change_controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=33,
        message_text="/pause",
        now=NOW,
    )
    assert no_change.text == "No change: ALREADY_PAUSED."
    rejected_controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=RejectedBackendFake()
    )
    durable_rejected = await rejected_controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=34,
        message_text="/pause",
        now=NOW,
    )
    assert durable_rejected.text == "Request rejected: MODE_VERSION_CONFLICT."

    error_controller = ControlBotConversationController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=ErrorBackendFake()
    )
    rejected = await error_controller.handle(
        admin_id=ADMIN_ID,
        bot_chat_id=ADMIN_ID,
        telegram_update_id=35,
        message_text=f"/human {TARGET}",
        now=NOW,
    )
    assert rejected.text == "Request rejected; state unchanged."
