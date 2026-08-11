"""Framework-independent M4 conversation-control command boundary."""

import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.domain.conversation import BaseMode

OPAQUE_TARGET = re.compile(r"^ct_[A-Za-z0-9_-]{20,100}$")


@dataclass(frozen=True, slots=True)
class ConversationStatusSummary:
    target_label: str
    base_mode: BaseMode
    base_source: str
    effective_mode: str
    operational_state: str
    pause_reason: str | None
    block_reason: str | None
    account_control_version: int
    mode_version: int
    content_revision: int
    unanswered_count: int
    active_turn_state: str | None = None
    active_draft_state: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationCommandResult:
    result_code: str
    changed: bool
    status: ConversationStatusSummary | None = None


class ConversationControlBackend(Protocol):
    async def set_mode(
        self,
        *,
        admin_id: int,
        telegram_update_id: int,
        target_token: str | None,
        mode: BaseMode | None,
        now: datetime,
    ) -> ConversationCommandResult: ...

    async def set_pause(
        self,
        *,
        admin_id: int,
        telegram_update_id: int,
        target_token: str | None,
        paused: bool,
        now: datetime,
    ) -> ConversationCommandResult: ...

    async def execute(
        self,
        *,
        admin_id: int,
        telegram_update_id: int,
        command: str,
        target_token: str,
        now: datetime,
    ) -> ConversationCommandResult: ...

    async def status(
        self,
        *,
        admin_id: int,
        target_token: str | None,
        now: datetime,
    ) -> ConversationStatusSummary: ...


class ControlBotConversationController:
    """Parse allowlisted commands without accepting display names as identity keys."""

    def __init__(
        self,
        *,
        allowed_admin_ids: frozenset[int],
        backend: ConversationControlBackend,
    ) -> None:
        if not allowed_admin_ids:
            raise ValueError("conversation control requires an admin allowlist")
        self._allowed_admin_ids = allowed_admin_ids
        self._backend = backend

    async def handle(  # noqa: PLR0911 - explicit command contract
        self,
        *,
        admin_id: int,
        telegram_update_id: int,
        message_text: str,
        now: datetime,
    ) -> BotReply:
        if admin_id not in self._allowed_admin_ids:
            return BotReply("Request rejected.")
        try:
            arguments = shlex.split(message_text.strip())
        except ValueError:
            return BotReply("Invalid command syntax.")
        if not arguments or not arguments[0].startswith("/"):
            return BotReply("Use a conversation control command.")
        command = arguments[0].split("@", maxsplit=1)[0].lower()
        target = arguments[1] if len(arguments) == 2 else None
        if len(arguments) > 2 or (target is not None and OPAQUE_TARGET.fullmatch(target) is None):
            return BotReply("Select a contact again; the target token is invalid or expired.")
        try:
            if command in {"/ai", "/human", "/copilot"}:
                mode = {
                    "/ai": BaseMode.AUTO,
                    "/human": BaseMode.HUMAN,
                    "/copilot": BaseMode.COPILOT,
                }[command]
                return _command_reply(
                    await self._backend.set_mode(
                        admin_id=admin_id,
                        telegram_update_id=telegram_update_id,
                        target_token=target,
                        mode=mode,
                        now=now,
                    )
                )
            if command == "/mode_inherit":
                if target is None:
                    return BotReply("Usage: /mode_inherit <selected-contact>")
                return _command_reply(
                    await self._backend.set_mode(
                        admin_id=admin_id,
                        telegram_update_id=telegram_update_id,
                        target_token=target,
                        mode=None,
                        now=now,
                    )
                )
            if command in {"/pause", "/resume"}:
                return _command_reply(
                    await self._backend.set_pause(
                        admin_id=admin_id,
                        telegram_update_id=telegram_update_id,
                        target_token=target,
                        paused=command == "/pause",
                        now=now,
                    )
                )
            if command in {"/draft", "/reply_pending", "/cancel", "/takeover_end"}:
                if target is None:
                    return BotReply(f"Usage: {command} <selected-contact>")
                return _command_reply(
                    await self._backend.execute(
                        admin_id=admin_id,
                        telegram_update_id=telegram_update_id,
                        command=command.removeprefix("/"),
                        target_token=target,
                        now=now,
                    )
                )
            if command == "/status":
                return BotReply(
                    _status_text(
                        await self._backend.status(
                            admin_id=admin_id,
                            target_token=target,
                            now=now,
                        )
                    )
                )
        except RuntimeError, ValueError:
            return BotReply("Request rejected; state unchanged.")
        return BotReply(
            "Unknown conversation command. Use /ai, /human, /copilot, /pause, "
            "/resume, /draft, /reply_pending, /cancel, or /status."
        )


def _command_reply(result: ConversationCommandResult) -> BotReply:
    if result.status is not None:
        return BotReply(_status_text(result.status))
    if result.changed:
        return BotReply(f"Completed: {result.result_code}.")
    return BotReply(f"No change: {result.result_code}.")


def _status_text(status: ConversationStatusSummary) -> str:
    lines = [
        f"Target: {status.target_label}",
        f"Base: {status.base_mode.value} ({status.base_source})",
        f"Effective: {status.effective_mode}; operational={status.operational_state}",
        (
            "Versions: "
            f"account={status.account_control_version}; mode={status.mode_version}; "
            f"content={status.content_revision}"
        ),
        f"Unanswered: {status.unanswered_count}",
    ]
    if status.pause_reason is not None:
        lines.append(f"Pause: {status.pause_reason}")
    if status.block_reason is not None:
        lines.append(f"Block: {status.block_reason}")
    if status.active_turn_state is not None:
        lines.append(f"Active turn: {status.active_turn_state}")
    if status.active_draft_state is not None:
        lines.append(f"Active draft: {status.active_draft_state}")
    return "\n".join(lines)
