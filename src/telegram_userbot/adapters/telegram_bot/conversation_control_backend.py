"""Queued M4 Control Bot boundary and app-owned command executor."""

import base64
import secrets
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid7

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.orchestrator_records import ControlCommandRecord
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
    OrchestratorConflictError,
)
from telegram_userbot.adapters.telegram_bot.conversation_control import (
    ConversationCommandResult,
    ConversationStatusSummary,
)
from telegram_userbot.domain.conversation import BaseMode, MaintenanceState
from telegram_userbot.domain.shared.redaction import SensitiveValue

ACCOUNT_COMMANDS = frozenset({"auto", "human", "copilot", "pause", "resume", "status"})
CONVERSATION_COMMANDS = frozenset(
    {
        "auto",
        "human",
        "copilot",
        "mode_inherit",
        "pause",
        "resume",
        "draft",
        "reply_pending",
        "cancel",
        "takeover_end",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class ConversationTarget:
    account_id: UUID
    conversation_id: UUID
    target_label: str = "Selected contact"


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    code: str
    changed: bool
    control_version: int | None = None
    mode_version: int | None = None
    payload: dict[str, Any] | None = None


class ConversationTargetTokenCodec:
    """Issue encrypted, authenticated, short-lived contact-selection tokens."""

    def __init__(
        self,
        signing_key: SensitiveValue[bytes],
        *,
        deployment_id: str,
        nonce_source: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if not deployment_id:
            raise ValueError("deployment id is required")
        key = signing_key.reveal_for_use()
        if len(key) < 32:
            raise ValueError("target token key must contain at least 256 bits")
        self._key = sha256(b"conversation-target-key-v1\0" + key).digest()
        self._deployment_id = deployment_id
        self._nonce_source = nonce_source

    def issue(
        self,
        *,
        account_id: UUID,
        conversation_id: UUID,
        admin_id: int,
        bot_chat_id: int,
        expires_at: datetime,
    ) -> str:
        if bot_chat_id != admin_id:
            raise ValueError("target token requires the administrator private chat")
        plaintext = (
            account_id.bytes
            + conversation_id.bytes
            + struct.pack(">qqq", admin_id, bot_chat_id, int(expires_at.timestamp()))
        )
        nonce = self._nonce_source(12)
        if len(nonce) != 12:
            raise ValueError("target token nonce source must return 12 bytes")
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, self._aad())
        return "ct_" + base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode("ascii")

    def resolve(
        self, token: str, *, admin_id: int, bot_chat_id: int, now: datetime
    ) -> ConversationTarget:
        if not token.startswith("ct_"):
            raise ValueError("invalid target token")
        encoded = token.removeprefix("ct_")
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise ValueError("invalid target token") from error
        if len(raw) != 84:
            raise ValueError("invalid target token")
        try:
            plaintext = AESGCM(self._key).decrypt(raw[:12], raw[12:], self._aad())
        except InvalidTag as error:
            raise ValueError("invalid target token") from error
        if len(plaintext) != 56:
            raise ValueError("invalid target token")
        account_id = UUID(bytes=plaintext[:16])
        conversation_id = UUID(bytes=plaintext[16:32])
        token_admin, token_chat, expiry = struct.unpack(">qqq", plaintext[32:])
        if token_admin != admin_id or token_chat != bot_chat_id or expiry <= int(now.timestamp()):
            raise ValueError("expired or wrong-principal target token")
        return ConversationTarget(account_id, conversation_id)

    def _aad(self) -> bytes:
        return b"conversation-target-v1\0" + self._deployment_id.encode()


class DurableConversationControlBackend:
    """Persist authenticated commands without mutating orchestrator-owned state."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        account_id: UUID,
        target_tokens: ConversationTargetTokenCodec,
        bot_identity: str = "control-bot",
        new_uuid: Callable[[], UUID] = uuid7,
    ) -> None:
        if not bot_identity:
            raise ValueError("bot identity is required")
        self._repository = ConversationOrchestratorRepository(session, new_uuid=new_uuid)
        self._account_id = account_id
        self._target_tokens = target_tokens
        self._bot_identity = bot_identity
        self._new_uuid = new_uuid

    async def set_mode(  # noqa: PLR0913 - actor/chat/update bindings are explicit
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        mode: BaseMode | None,
        now: datetime,
    ) -> ConversationCommandResult:
        if mode is None and target_token is None:
            raise ValueError("account mode cannot inherit")
        return await self._enqueue(
            admin_id=admin_id,
            bot_chat_id=bot_chat_id,
            telegram_update_id=telegram_update_id,
            command_kind="mode_inherit" if mode is None else mode.value.lower(),
            target_token=target_token,
            now=now,
        )

    async def set_pause(  # noqa: PLR0913 - actor/chat/update bindings are explicit
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        paused: bool,
        now: datetime,
    ) -> ConversationCommandResult:
        return await self._enqueue(
            admin_id=admin_id,
            bot_chat_id=bot_chat_id,
            telegram_update_id=telegram_update_id,
            command_kind="pause" if paused else "resume",
            target_token=target_token,
            now=now,
        )

    async def execute(  # noqa: PLR0913 - actor/chat/update bindings are explicit
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        command: str,
        target_token: str,
        now: datetime,
    ) -> ConversationCommandResult:
        if command not in CONVERSATION_COMMANDS - {"status"}:
            raise OrchestratorConflictError("UNSUPPORTED_CONTROL_COMMAND")
        return await self._enqueue(
            admin_id=admin_id,
            bot_chat_id=bot_chat_id,
            telegram_update_id=telegram_update_id,
            command_kind=command,
            target_token=target_token,
            now=now,
        )

    async def status(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        target_token: str | None,
        now: datetime,
    ) -> ConversationCommandResult:
        return await self._enqueue(
            admin_id=admin_id,
            bot_chat_id=bot_chat_id,
            telegram_update_id=telegram_update_id,
            command_kind="status",
            target_token=target_token,
            now=now,
        )

    async def _enqueue(  # noqa: PLR0913 - durable identity is explicit
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        command_kind: str,
        target_token: str | None,
        now: datetime,
    ) -> ConversationCommandResult:
        replay = await self._repository.get_control_command(
            bot_identity=self._bot_identity, telegram_update_id=telegram_update_id
        )
        if replay is not None:
            self._validate_replay(replay, admin_id, bot_chat_id, command_kind)
            return _command_result(replay)
        if bot_chat_id != admin_id:
            raise ValueError("control command requires the administrator private chat")
        target = self._target(target_token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now)
        allowed = ACCOUNT_COMMANDS if target is None else CONVERSATION_COMMANDS
        if command_kind not in allowed:
            raise OrchestratorConflictError("UNSUPPORTED_CONTROL_COMMAND")
        conversation_id = target.conversation_id if target is not None else None
        idempotency_key = sha256(
            b"conversation-control-v2\0"
            + self._bot_identity.encode()
            + b"\0"
            + str(telegram_update_id).encode()
            + b"\0"
            + str(admin_id).encode()
            + b"\0"
            + str(bot_chat_id).encode()
            + b"\0"
            + command_kind.encode()
            + b"\0"
            + (conversation_id.bytes if conversation_id is not None else b"account")
        ).digest()
        command = await self._repository.record_control_command(
            command_id=self._new_uuid(),
            account_id=self._account_id,
            conversation_id=conversation_id,
            bot_identity=self._bot_identity,
            telegram_update_id=telegram_update_id,
            admin_telegram_user_id=admin_id,
            bot_chat_id=bot_chat_id,
            command_kind=command_kind,
            idempotency_key=idempotency_key,
            now=now,
        )
        self._validate_replay(command, admin_id, bot_chat_id, command_kind)
        await self._repository.add_control_command_outbox(
            command_id=command.id,
            account_id=command.account_id,
            topic="control.command.requested",
            now=now,
        )
        return _command_result(command)

    def _target(
        self, token: str | None, *, admin_id: int, bot_chat_id: int, now: datetime
    ) -> ConversationTarget | None:
        if token is None:
            return None
        target = self._target_tokens.resolve(
            token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now
        )
        if target.account_id != self._account_id:
            raise ValueError("target account mismatch")
        return target

    def _validate_replay(
        self, command: ControlCommandRecord, admin_id: int, bot_chat_id: int, kind: str
    ) -> None:
        if (
            command.account_id != self._account_id
            or command.admin_telegram_user_id != admin_id
            or command.bot_chat_id != bot_chat_id
            or command.command_kind != kind
        ):
            raise ValueError("control command replay identity mismatch")


class ConversationControlCommandProcessor:
    """Execute one queued command under the app runtime's transaction and locks."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        account_id: UUID,
        allowed_admin_ids: frozenset[int],
        bot_identity: str = "control-bot",
        new_uuid: Callable[[], UUID] = uuid7,
    ) -> None:
        if not allowed_admin_ids or not bot_identity:
            raise ValueError("command processor requires an admin allowlist and bot identity")
        self._session = session
        self._repository = ConversationOrchestratorRepository(session, new_uuid=new_uuid)
        self._account_id = account_id
        self._allowed_admin_ids = allowed_admin_ids
        self._bot_identity = bot_identity

    async def process_next(
        self, *, now: datetime, command_id: UUID | None = None
    ) -> ConversationCommandResult | None:
        command = await self._repository.claim_pending_control_command(
            account_id=self._account_id,
            bot_identity=self._bot_identity,
            command_id=command_id,
        )
        if command is None:
            return None
        command_result: ConversationCommandResult
        try:
            self._validate_principal(command)
            prepared = await self._prepare(command, now)
            async with self._session.begin_nested():
                execution = await self._execute(command, now, prepared)
        except OrchestratorConflictError as error:
            result_code = error.code or "CONTROL_COMMAND_REJECTED"
            await self._repository.finish_control_command(
                command_id=command.id,
                result_code=result_code,
                accepted=False,
                result_changed=False,
                now=now,
            )
            command_result = ConversationCommandResult(result_code, False, accepted=False)
        else:
            await self._repository.finish_control_command(
                command_id=command.id,
                result_code=execution.code,
                accepted=True,
                result_changed=execution.changed,
                result_control_version=execution.control_version,
                result_mode_version=execution.mode_version,
                result_payload=execution.payload,
                now=now,
            )
            command_result = ConversationCommandResult(
                execution.code,
                execution.changed,
                status=_status_from_payload(execution.payload),
            )
        await self._repository.add_control_command_outbox(
            command_id=command.id,
            account_id=command.account_id,
            topic="control.command.completed",
            now=now,
        )
        return command_result

    async def _prepare(self, command: ControlCommandRecord, now: datetime) -> Any:
        """Lock target state and retain the execution snapshot outside the savepoint."""

        if command.conversation_id is None:
            account = await self._repository.account_control(command.account_id, now)
            await self._repository.bind_control_command_versions(
                command_id=command.id,
                expected_control_version=account.control_version,
                expected_mode_version=None,
            )
            return account
        activity = await self._repository.conversation_activity(command.conversation_id, now)
        await self._repository.bind_control_command_versions(
            command_id=command.id,
            expected_control_version=activity.resolution.account_control_version,
            expected_mode_version=activity.resolution.mode_version,
        )
        return activity

    def _validate_principal(self, command: ControlCommandRecord) -> None:
        if (
            command.account_id != self._account_id
            or command.bot_identity != self._bot_identity
            or command.admin_telegram_user_id not in self._allowed_admin_ids
            or command.bot_chat_id != command.admin_telegram_user_id
        ):
            raise OrchestratorConflictError("CONTROL_COMMAND_PRINCIPAL_REJECTED")
        allowed = ACCOUNT_COMMANDS if command.conversation_id is None else CONVERSATION_COMMANDS
        if command.command_kind not in allowed:
            raise OrchestratorConflictError("UNSUPPORTED_CONTROL_COMMAND")

    async def _execute(  # noqa: PLR0911 - command dispatch is intentionally explicit
        self, command: ControlCommandRecord, now: datetime, prepared: Any
    ) -> _ExecutionResult:
        actor = f"telegram_admin:{command.admin_telegram_user_id}"
        if command.conversation_id is None:
            account = prepared
            if command.command_kind in {"auto", "human", "copilot"}:
                changed, version = await self._repository.set_account_control(
                    account_id=command.account_id,
                    actor_ref=actor,
                    now=now,
                    expected_version=account.control_version,
                    default_base_mode=BaseMode(command.command_kind.upper()),
                )
                return _ExecutionResult(
                    "MODE_CHANGED" if changed else "NO_CHANGE", changed, version
                )
            if command.command_kind in {"pause", "resume"}:
                changed, version = await self._repository.set_account_control(
                    account_id=command.account_id,
                    actor_ref=actor,
                    now=now,
                    expected_version=account.control_version,
                    global_paused=command.command_kind == "pause",
                )
                return _ExecutionResult(
                    "PAUSED" if command.command_kind == "pause" else "RESUMED",
                    changed,
                    version,
                )
            if command.command_kind == "status":
                return _ExecutionResult(
                    "STATUS",
                    False,
                    account.control_version,
                    payload=_account_status_payload(account),
                )
            raise OrchestratorConflictError("UNSUPPORTED_CONTROL_COMMAND")

        activity = prepared
        resolution = activity.resolution
        if command.command_kind in {"auto", "human", "copilot", "mode_inherit"}:
            mode = (
                None
                if command.command_kind == "mode_inherit"
                else BaseMode(command.command_kind.upper())
            )
            control = await self._repository.set_conversation_control(
                conversation_id=command.conversation_id,
                actor_ref=actor,
                now=now,
                expected_version=resolution.mode_version,
                base_mode_override=mode,
            )
            return _ExecutionResult(
                control.result_code,
                control.changed,
                control.resolution.account_control_version,
                control.resolution.mode_version,
            )
        if command.command_kind in {"pause", "resume"}:
            control = await self._repository.set_conversation_control(
                conversation_id=command.conversation_id,
                actor_ref=actor,
                now=now,
                expected_version=resolution.mode_version,
                contact_paused=command.command_kind == "pause",
            )
            return _ExecutionResult(
                "PAUSED" if command.command_kind == "pause" else "RESUMED",
                control.changed,
                control.resolution.account_control_version,
                control.resolution.mode_version,
            )
        if command.command_kind == "draft":
            draft = await self._repository.request_copilot_draft(
                conversation_id=command.conversation_id, requested_by=actor, now=now
            )
            return _ExecutionResult(
                "DRAFT_REQUESTED",
                True,
                draft.snapshot.account_control_version,
                draft.snapshot.mode_version,
            )
        if command.command_kind == "reply_pending":
            turn = await self._repository.create_pending_turn(
                conversation_id=command.conversation_id,
                trigger_kind="manual_pending_reply",
                now=now,
                ignore_resume_floor=True,
            )
            return _ExecutionResult(
                "PENDING_REPLY_CREATED",
                True,
                turn.snapshot.account_control_version,
                turn.snapshot.mode_version,
            )
        if command.command_kind == "cancel":
            control = await self._repository.set_conversation_control(
                conversation_id=command.conversation_id,
                actor_ref=actor,
                now=now,
                expected_version=resolution.mode_version,
                cancel_only=True,
            )
            return _ExecutionResult(
                "CANCELLED",
                control.changed,
                control.resolution.account_control_version,
                control.resolution.mode_version,
            )
        if command.command_kind == "takeover_end":
            control = await self._repository.end_temporary_human(
                conversation_id=command.conversation_id,
                actor_ref=actor,
                expected_version=resolution.mode_version,
                now=now,
            )
            return _ExecutionResult(
                control.result_code,
                control.changed,
                control.resolution.account_control_version,
                control.resolution.mode_version,
            )
        if command.command_kind == "status":
            return _ExecutionResult(
                "STATUS",
                False,
                resolution.account_control_version,
                resolution.mode_version,
                _conversation_status_payload(activity),
            )
        raise OrchestratorConflictError("UNSUPPORTED_CONTROL_COMMAND")


def _command_result(command: ControlCommandRecord) -> ConversationCommandResult:
    if command.state == "pending":
        return ConversationCommandResult("CONTROL_COMMAND_QUEUED", False, pending=True)
    return ConversationCommandResult(
        command.result_code or "UNKNOWN",
        command.state == "applied" and command.result_changed is True,
        status=_status_from_payload(command.result_payload),
        accepted=command.state == "applied",
    )


def _account_status_payload(account: Any) -> dict[str, Any]:
    maintenance_paused = account.maintenance_state is not MaintenanceState.INACTIVE
    paused = account.global_paused or maintenance_paused
    effective = "PAUSED" if paused else account.default_base_mode.value
    pause_reason = (
        f"maintenance_{account.maintenance_state.value}"
        if maintenance_paused
        else "global_pause"
        if account.global_paused
        else None
    )
    return {
        "target_label": "Account default",
        "base_mode": account.default_base_mode.value,
        "base_source": "account_default",
        "effective_mode": effective,
        "operational_state": "PAUSED" if paused else "READY",
        "pause_reason": pause_reason,
        "block_reason": None,
        "account_control_version": account.control_version,
        "mode_version": None,
        "content_revision": None,
        "unanswered_count": 0,
        "active_turn_state": None,
        "active_draft_state": None,
    }


def _conversation_status_payload(activity: Any) -> dict[str, Any]:
    resolution = activity.resolution
    return {
        "target_label": "Selected contact",
        "base_mode": resolution.base_mode.value,
        "base_source": resolution.base_source,
        "effective_mode": resolution.effective_mode.value,
        "operational_state": resolution.operational_state.value,
        "pause_reason": resolution.pause_reason,
        "block_reason": resolution.block_reason,
        "account_control_version": resolution.account_control_version,
        "mode_version": resolution.mode_version,
        "content_revision": resolution.content_revision,
        "unanswered_count": activity.unanswered_count,
        "active_turn_state": activity.active_turn_state,
        "active_draft_state": activity.active_draft_state,
    }


def _status_from_payload(payload: dict[str, Any] | None) -> ConversationStatusSummary | None:
    if payload is None:
        return None
    try:
        return ConversationStatusSummary(
            str(payload["target_label"]),
            BaseMode(str(payload["base_mode"])),
            str(payload["base_source"]),
            str(payload["effective_mode"]),
            str(payload["operational_state"]),
            payload.get("pause_reason"),
            payload.get("block_reason"),
            int(payload["account_control_version"]),
            int(payload["mode_version"]) if payload.get("mode_version") is not None else None,
            int(payload["content_revision"])
            if payload.get("content_revision") is not None
            else None,
            int(payload["unanswered_count"]),
            payload.get("active_turn_state"),
            payload.get("active_draft_state"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid control command result payload") from error


__all__ = [
    "ConversationControlCommandProcessor",
    "ConversationTarget",
    "ConversationTargetTokenCodec",
    "DurableConversationControlBackend",
]
