"""Admin-only Control Bot boundary for memory review and forget requests."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationTargetTokenCodec,
)
from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.domain.shared.redaction import SensitiveValue


@dataclass(frozen=True, slots=True)
class MemoryCandidateSummary:
    token: str
    operation: str
    memory_type: str
    confidence_band: str
    evidence_count: int
    visual_only: bool


@dataclass(frozen=True, slots=True)
class MemoryItemSummary:
    token: str
    memory_type: str
    status: str
    version_no: int


@dataclass(frozen=True, slots=True)
class MemoryStatusSummary:
    freshness: str
    pending_jobs: int
    candidate_count: int
    active_count: int
    embedding_state: str


@dataclass(frozen=True, slots=True)
class MemoryReviewChallenge:
    action: str
    callback_token: SensitiveValue[str]
    expires_at: datetime


class MemoryControlBackend(Protocol):
    async def status(
        self, *, account_id: object, conversation_id: object, now: datetime
    ) -> MemoryStatusSummary: ...

    async def candidates(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryCandidateSummary, ...]: ...

    async def active(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryItemSummary, ...]: ...

    async def issue_action(
        self,
        *,
        action: str,
        target_token: str,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> MemoryReviewChallenge: ...

    async def confirm_action(
        self,
        *,
        callback_token: SensitiveValue[str],
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> bool: ...


class MemoryControlController:
    def __init__(
        self,
        *,
        allowed_admin_ids: frozenset[int],
        target_tokens: ConversationTargetTokenCodec,
        backend: MemoryControlBackend,
    ) -> None:
        if not allowed_admin_ids:
            raise ValueError("memory control requires an admin allowlist")
        self._allowed_admin_ids = allowed_admin_ids
        self._target_tokens = target_tokens
        self._backend = backend

    async def handle(  # noqa: PLR0911 - command boundary is fail-closed
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        message_text: str,
        now: datetime,
    ) -> BotReply:
        if admin_id not in self._allowed_admin_ids or bot_chat_id != admin_id:
            return BotReply("Request rejected.")
        try:
            args = shlex.split(message_text.strip())
        except ValueError:
            return BotReply("Invalid command syntax.")
        if len(args) != 2:
            return BotReply(
                "Usage: /memory, /memory_candidates, /memory_status, /memory_accept, "
                "/memory_reject, or /forget <selected-contact>."
            )
        command = args[0].split("@", maxsplit=1)[0].lower()
        target_token = args[1]
        try:
            action = {
                "/memory_accept": "accept",
                "/memory_reject": "reject",
                "/forget": "forget",
            }.get(command)
            if action is not None:
                challenge = await self._backend.issue_action(
                    action=action,
                    target_token=target_token,
                    admin_id=admin_id,
                    bot_chat_id=bot_chat_id,
                    now=now,
                )
                return BotReply(
                    f"{action} request created. Confirm within 5 minutes; "
                    "no memory content is shown.",
                    callback_token=challenge.callback_token,
                )
            target = self._target_tokens.resolve(
                target_token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now
            )
            if command == "/memory_status":
                return BotReply(
                    _status_text(
                        await self._backend.status(
                            account_id=target.account_id,
                            conversation_id=target.conversation_id,
                            now=now,
                        )
                    )
                )
            if command == "/memory":
                return BotReply(
                    _memory_text(
                        await self._backend.active(
                            account_id=target.account_id,
                            conversation_id=target.conversation_id,
                            admin_id=admin_id,
                            bot_chat_id=bot_chat_id,
                            now=now,
                        )
                    )
                )
            if command == "/memory_candidates":
                return BotReply(
                    _candidate_text(
                        await self._backend.candidates(
                            account_id=target.account_id,
                            conversation_id=target.conversation_id,
                            admin_id=admin_id,
                            bot_chat_id=bot_chat_id,
                            now=now,
                        )
                    )
                )
            return BotReply("Unknown memory command.")
        except RuntimeError, ValueError:
            return BotReply("Request rejected; state unchanged.")

    async def confirm_callback(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        callback_token: SensitiveValue[str],
        now: datetime,
    ) -> BotReply:
        if admin_id not in self._allowed_admin_ids or bot_chat_id != admin_id:
            return BotReply("Request rejected.")
        try:
            applied = await self._backend.confirm_action(
                callback_token=callback_token,
                admin_id=admin_id,
                bot_chat_id=bot_chat_id,
                now=now,
            )
        except RuntimeError, ValueError:
            applied = False
        return BotReply(
            "Memory action confirmed and queued."
            if applied
            else "Memory action invalid, expired, or already used."
        )


def _status_text(status: MemoryStatusSummary) -> str:
    return (
        f"Memory freshness={status.freshness}; pending_jobs={status.pending_jobs}; "
        f"candidates={status.candidate_count}; active={status.active_count}; "
        f"embedding={status.embedding_state}"
    )


def _candidate_text(candidates: tuple[MemoryCandidateSummary, ...]) -> str:
    if not candidates:
        return "No memory candidates."
    lines = ["Memory candidates:"]
    for candidate in candidates:
        visual = "; visual_only" if candidate.visual_only else ""
        lines.append(
            f"{candidate.token}: {candidate.operation}/{candidate.memory_type}; "
            f"confidence={candidate.confidence_band}; evidence={candidate.evidence_count}{visual}"
        )
    return "\n".join(lines)


def _memory_text(memories: tuple[MemoryItemSummary, ...]) -> str:
    if not memories:
        return "No active memories."
    lines = ["Active memories:"]
    lines.extend(
        f"{memory.token}: {memory.memory_type}; status={memory.status}; version={memory.version_no}"
        for memory in memories
    )
    return "\n".join(lines)
