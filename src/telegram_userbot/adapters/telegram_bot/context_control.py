"""Metadata-only /context and two-step /context_preview command boundary."""

import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_userbot.adapters.persistence.context_repository import (
    ContextSummaryRecord,
    PreviewChallenge,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.domain.shared.redaction import SensitiveValue


@dataclass(frozen=True, slots=True)
class PreviewDeliveryResult:
    state: str
    delivered_chunks: int
    total_chunks: int


class ContextControlBackend(Protocol):
    async def summary(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> ContextSummaryRecord | None: ...

    async def issue_preview(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> PreviewChallenge: ...

    async def confirm_preview(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        confirmation_token: SensitiveValue[str],
        now: datetime,
    ) -> tuple[PreviewRequestRecord, tuple[SensitiveValue[str], ...]] | None: ...

    async def deliver_preview(
        self,
        *,
        request: PreviewRequestRecord,
        chunks: tuple[SensitiveValue[str], ...],
        now: datetime,
    ) -> PreviewDeliveryResult: ...


class ControlBotContextController:
    def __init__(
        self, *, allowed_admin_ids: frozenset[int], backend: ContextControlBackend
    ) -> None:
        if not allowed_admin_ids:
            raise ValueError("context control requires an admin allowlist")
        self._allowed_admin_ids = allowed_admin_ids
        self._backend = backend

    async def handle(  # noqa: PLR0911 - explicit fail-closed command branches
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
            arguments = shlex.split(message_text.strip())
        except ValueError:
            return BotReply("Invalid command syntax.")
        if len(arguments) != 2:
            return BotReply(
                "Usage: /context <selected-contact> or /context_preview <selected-contact>."
            )
        command = arguments[0].split("@", maxsplit=1)[0].lower()
        target_token = arguments[1]
        try:
            if command == "/context":
                summary = await self._backend.summary(
                    admin_id=admin_id,
                    bot_chat_id=bot_chat_id,
                    target_token=target_token,
                    now=now,
                )
                return BotReply(
                    "No context manifest is available."
                    if summary is None
                    else _summary_text(summary)
                )
            if command == "/context_preview":
                challenge = await self._backend.issue_preview(
                    admin_id=admin_id,
                    bot_chat_id=bot_chat_id,
                    target_token=target_token,
                    now=now,
                )
                return BotReply(
                    "Sensitive preview request created. Telegram copies, forwards, screenshots, "
                    "and unknown sends may remain after best-effort deletion. Use the Confirm "
                    "button within 5 minutes.",
                    callback_token=challenge.confirmation_token,
                )
        except RuntimeError, ValueError:
            return BotReply("Request rejected; state unchanged.")
        return BotReply("Unknown context command.")

    async def confirm_callback(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        confirmation_token: SensitiveValue[str],
        now: datetime,
    ) -> BotReply:
        """Consume a callback token without exposing it in a Bot command or message body."""

        if admin_id not in self._allowed_admin_ids or bot_chat_id != admin_id:
            return BotReply("Request rejected.")
        try:
            confirmed = await self._backend.confirm_preview(
                admin_id=admin_id,
                bot_chat_id=bot_chat_id,
                confirmation_token=confirmation_token,
                now=now,
            )
            if confirmed is None:
                return BotReply("Preview confirmation is invalid, expired, or already used.")
            request, chunks = confirmed
            result = await self._backend.deliver_preview(
                request=request,
                chunks=chunks,
                now=now,
            )
            if result.state == "send_unknown":
                return BotReply(
                    "Preview delivery became send_unknown; it will not be retried automatically."
                )
            return BotReply(
                f"Preview delivered in {result.delivered_chunks}/{result.total_chunks} chunks; "
                "known Bot messages are scheduled for best-effort deletion."
            )
        except RuntimeError, ValueError:
            return BotReply("Request rejected; state unchanged.")


def _summary_text(summary: ContextSummaryRecord) -> str:
    layers = (
        ", ".join(
            f"{layer}={summary.layer_counts[layer]}/{summary.layer_tokens.get(layer, 0)}t"
            for layer in sorted(summary.layer_counts)
        )
        or "none"
    )
    return "\n".join(
        (
            f"Manifest: {summary.manifest_short_id}; purpose={summary.purpose}; "
            f"role={summary.logical_role}",
            f"Budget: {summary.input_token_estimate}/{summary.effective_input_budget}; "
            f"images={summary.image_count}",
            f"Layers: {layers}",
            f"Freshness: {summary.memory_freshness}; omissions={summary.omission_count}",
            "Versions: "
            f"builder={summary.builder_version}; context={summary.context_policy_version}; "
            f"retrieval={summary.retrieval_policy_version}; "
            f"estimator={summary.token_estimator_version}",
        )
    )
