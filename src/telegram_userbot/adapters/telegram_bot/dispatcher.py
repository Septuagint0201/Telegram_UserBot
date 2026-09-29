"""Private-admin Control Bot update dispatcher with no persistence dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from telegram_userbot.adapters.telegram_bot.http import (
    BotAPIError,
    BotMutationState,
    TelegramBotAPI,
    TelegramBotIdentity,
)
from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.domain.shared.redaction import SensitiveValue

_MODEL_COMMANDS = frozenset(
    {
        "/models",
        "/model_show",
        "/model_config",
        "/model_cancel",
        "/model_validate",
        "/model_activate",
        "/model_key",
    }
)
_CONVERSATION_COMMANDS = frozenset(
    {
        "/ai",
        "/human",
        "/copilot",
        "/mode_inherit",
        "/pause",
        "/resume",
        "/draft",
        "/reply_pending",
        "/cancel",
        "/takeover_end",
        "/status",
    }
)
_MEMORY_COMMANDS = frozenset(
    {
        "/memory",
        "/memory_candidates",
        "/memory_status",
        "/memory_accept",
        "/memory_reject",
        "/forget",
    }
)
_CONTEXT_COMMANDS = frozenset({"/context", "/context_preview"})
_FORWARD_MARKERS = frozenset(
    {
        "forward_origin",
        "forward_from",
        "forward_from_chat",
        "forward_sender_name",
        "forward_signature",
        "forward_date",
        "is_automatic_forward",
    }
)


class PublicServiceState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ServerStatusSnapshot:
    """Only the four public state bands are allowed onto Telegram."""

    app: PublicServiceState
    control: PublicServiceState
    worker: PublicServiceState

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, PublicServiceState)
            for value in (self.app, self.control, self.worker)
        ):
            raise TypeError("server status snapshot is invalid")

    @classmethod
    def unknown(cls) -> ServerStatusSnapshot:
        return cls(
            PublicServiceState.UNKNOWN,
            PublicServiceState.UNKNOWN,
            PublicServiceState.UNKNOWN,
        )


class ServerStatusProvider(Protocol):
    async def snapshot(self) -> ServerStatusSnapshot: ...


class ModelController(Protocol):
    async def handle(self, *, admin_id: int, message_text: str, now: datetime) -> BotReply: ...


class ConversationController(Protocol):
    async def handle(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        telegram_update_id: int,
        message_text: str,
        now: datetime,
    ) -> BotReply: ...


class MemoryController(Protocol):
    async def handle(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        message_text: str,
        now: datetime,
    ) -> BotReply: ...

    async def confirm_callback(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        callback_token: SensitiveValue[str],
        now: datetime,
    ) -> BotReply: ...


class ContextController(Protocol):
    async def handle(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        message_text: str,
        now: datetime,
    ) -> BotReply: ...

    async def confirm_callback(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        confirmation_token: SensitiveValue[str],
        now: datetime,
    ) -> BotReply: ...


class UpdateDisposition(StrEnum):
    HANDLED = "handled"
    REJECTED = "rejected"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    disposition: UpdateDisposition
    send_state: BotMutationState | None = None


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    """Ephemeral reply plan produced before the durable receipt is committed.

    The plan is deliberately never serialised: it may contain a short-lived
    callback or Web App token.  Production composition commits the command and
    receipt first, then hands this object to :meth:`deliver` exactly once.
    """

    disposition: UpdateDisposition
    chat_id: int | None = None
    reply: BotReply | None = None
    callback_query_id: str | None = None
    callback_namespace: str | None = None

    @property
    def response_required(self) -> bool:
        return self.reply is not None or self.callback_query_id is not None


@dataclass(frozen=True, slots=True)
class _AdminMessage:
    user_id: int
    chat_id: int
    message_id: int
    text: str


@dataclass(frozen=True, slots=True)
class _AdminCallback:
    callback_id: str
    user_id: int
    chat_id: int
    data: SensitiveValue[str]


@dataclass(frozen=True, slots=True)
class _RoutedReply:
    reply: BotReply
    callback_namespace: str | None = None


class ControlBotDispatcher:
    """Validate the Telegram principal before invoking injected application controllers."""

    def __init__(  # noqa: PLR0913 - explicit injected security/application boundaries
        self,
        *,
        identity: TelegramBotIdentity,
        allowed_admin_ids: frozenset[int],
        api: TelegramBotAPI,
        model: ModelController,
        conversation: ConversationController,
        memory: MemoryController,
        context: ContextController,
        status_provider: ServerStatusProvider,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not allowed_admin_ids or any(
            type(value) is not int or value <= 0 for value in allowed_admin_ids
        ):
            raise ValueError("Control Bot admin allowlist is invalid")
        if api.identity != identity:
            raise ValueError("Control Bot identity binding is invalid")
        self._identity = identity
        self._allowed_admin_ids = allowed_admin_ids
        self._api = api
        self._model = model
        self._conversation = conversation
        self._memory = memory
        self._context = context
        self._status_provider = status_provider
        self._now = now or (lambda: datetime.now(UTC))

    async def handle_update(self, *, update_id: int, update: Mapping[str, Any]) -> DispatchOutcome:
        prepared = await self.route_update(update_id=update_id, update=update)
        return await self.deliver(prepared)

    async def route_update(self, *, update_id: int, update: Mapping[str, Any]) -> PreparedDispatch:
        """Validate and route without calling the Telegram Bot API."""

        if type(update_id) is not int or update_id < 0 or not isinstance(update, Mapping):
            raise BotAPIError("BOT_UPDATE_INVALID")
        try:
            now = _validated_now(self._now())
            if "message" in update and "callback_query" not in update:
                message = self._message(update["message"])
                if message is None:
                    return PreparedDispatch(UpdateDisposition.REJECTED)
                command = _command_name(message.text)
                if command is not None and not _is_for_this_bot(
                    message.text, self._identity.username
                ):
                    return PreparedDispatch(UpdateDisposition.REJECTED)
                routed = await self._route_message(message, update_id=update_id, now=now)
                return PreparedDispatch(
                    UpdateDisposition.HANDLED,
                    chat_id=message.chat_id,
                    reply=routed.reply,
                    callback_namespace=routed.callback_namespace,
                )
            if "callback_query" in update and "message" not in update:
                callback = self._callback(update["callback_query"])
                if callback is None:
                    return PreparedDispatch(UpdateDisposition.REJECTED)
                return await self._route_callback(callback, now=now)
            return PreparedDispatch(UpdateDisposition.IGNORED)
        except asyncio.CancelledError:
            raise
        except BotAPIError:
            raise
        except Exception:
            raise BotAPIError("BOT_DISPATCH_FAILED") from None

    async def _route_message(  # noqa: PLR0911 - explicit command ownership matrix
        self, message: _AdminMessage, *, update_id: int, now: datetime
    ) -> _RoutedReply:
        if "\x00" in message.text:
            return _RoutedReply(BotReply("Request rejected."))
        command = _command_name(message.text)
        if command == "/server_status":
            if len(message.text.split()) != 1:
                return _RoutedReply(BotReply("Usage: /server_status"))
            try:
                raw_snapshot: object = await self._status_provider.snapshot()
                snapshot = (
                    raw_snapshot
                    if isinstance(raw_snapshot, ServerStatusSnapshot)
                    else ServerStatusSnapshot.unknown()
                )
            except Exception:
                snapshot = ServerStatusSnapshot.unknown()
            return _RoutedReply(BotReply(_server_status_text(snapshot)))
        if command in _CONVERSATION_COMMANDS:
            return _RoutedReply(
                await self._conversation.handle(
                    admin_id=message.user_id,
                    bot_chat_id=message.chat_id,
                    telegram_update_id=update_id,
                    message_text=message.text,
                    now=now,
                )
            )
        if command in _MEMORY_COMMANDS:
            return _RoutedReply(
                await self._memory.handle(
                    admin_id=message.user_id,
                    bot_chat_id=message.chat_id,
                    message_text=message.text,
                    now=now,
                ),
                "memory",
            )
        if command in _CONTEXT_COMMANDS:
            return _RoutedReply(
                await self._context.handle(
                    admin_id=message.user_id,
                    bot_chat_id=message.chat_id,
                    message_text=message.text,
                    now=now,
                ),
                "context",
            )
        if command is None or command in _MODEL_COMMANDS:
            return _RoutedReply(
                await self._model.handle(
                    admin_id=message.user_id,
                    message_text=message.text,
                    now=now,
                )
            )
        return _RoutedReply(BotReply("Unknown Control Bot command."))

    async def _route_callback(self, callback: _AdminCallback, *, now: datetime) -> PreparedDispatch:
        raw = callback.data.reveal_for_use()
        namespace, separator, token = raw.partition(":")
        if not separator or not token or namespace not in {"memory", "context"}:
            return PreparedDispatch(
                UpdateDisposition.REJECTED,
                callback_query_id=callback.callback_id,
            )
        if namespace == "memory":
            reply = await self._memory.confirm_callback(
                admin_id=callback.user_id,
                bot_chat_id=callback.chat_id,
                callback_token=SensitiveValue(token),
                now=now,
            )
        else:
            reply = await self._context.confirm_callback(
                admin_id=callback.user_id,
                bot_chat_id=callback.chat_id,
                confirmation_token=SensitiveValue(token),
                now=now,
            )
        return PreparedDispatch(
            UpdateDisposition.HANDLED,
            chat_id=callback.chat_id,
            reply=reply,
            callback_query_id=callback.callback_id,
        )

    async def deliver(self, prepared: PreparedDispatch) -> DispatchOutcome:
        """Perform the one-shot Bot mutations for a committed reply plan."""

        if not isinstance(prepared, PreparedDispatch):
            raise BotAPIError("BOT_DISPATCH_INVALID")
        mutation_states: list[BotMutationState] = []
        if prepared.callback_query_id is not None:
            result = await self._api.answer_callback_query(
                callback_query_id=prepared.callback_query_id
            )
            mutation_states.append(result.state)
        if prepared.reply is None:
            return DispatchOutcome(
                prepared.disposition,
                _combined_mutation_state(mutation_states),
            )
        if prepared.chat_id is None:
            raise BotAPIError("BOT_DISPATCH_INVALID")
        callback_data: SensitiveValue[str] | None = None
        if prepared.reply.callback_token is not None:
            if prepared.callback_namespace not in {"memory", "context"}:
                return DispatchOutcome(UpdateDisposition.REJECTED)
            callback_data = SensitiveValue(
                f"{prepared.callback_namespace}:{prepared.reply.callback_token.reveal_for_use()}"
            )
        try:
            result = await self._api.send_message(
                chat_id=prepared.chat_id,
                text=prepared.reply.text,
                web_app_url=prepared.reply.web_app_url,
                callback_data=callback_data,
            )
        except BotAPIError:
            mutation_states.append(BotMutationState.REJECTED)
        else:
            mutation_states.append(result.state)
        return DispatchOutcome(
            prepared.disposition,
            _combined_mutation_state(mutation_states),
        )

    def _message(self, value: object) -> _AdminMessage | None:
        if not isinstance(value, Mapping) or _is_forwarded(value) or "web_app_data" in value:
            return None
        sender = value.get("from")
        chat = value.get("chat")
        if not _is_private_admin(sender, chat, self._allowed_admin_ids):
            return None
        assert isinstance(sender, Mapping)
        assert isinstance(chat, Mapping)
        message_id = value.get("message_id")
        text = value.get("text")
        if (
            type(message_id) is not int
            or message_id <= 0
            or not isinstance(text, str)
            or not 1 <= len(text) <= 4096
        ):
            return None
        return _AdminMessage(
            user_id=sender["id"],
            chat_id=chat["id"],
            message_id=message_id,
            text=text,
        )

    def _callback(  # noqa: PLR0911 - fail-closed Telegram shape validator
        self, value: object
    ) -> _AdminCallback | None:
        if not isinstance(value, Mapping) or "inline_message_id" in value:
            return None
        sender = value.get("from")
        message = value.get("message")
        if not isinstance(message, Mapping) or _is_forwarded(message):
            return None
        chat = message.get("chat")
        if not _is_private_admin(sender, chat, self._allowed_admin_ids):
            return None
        message_sender = message.get("from")
        if not isinstance(message_sender, Mapping):
            return None
        username = message_sender.get("username")
        message_id = message.get("message_id")
        if (
            message_sender.get("id") != self._identity.user_id
            or message_sender.get("is_bot") is not True
            or type(message_id) is not int
            or message_id <= 0
            or (
                username is not None
                and (
                    not isinstance(username, str)
                    or username.casefold() != self._identity.username.casefold()
                )
            )
        ):
            return None
        callback_id = value.get("id")
        data = value.get("data")
        if (
            not isinstance(callback_id, str)
            or not 1 <= len(callback_id) <= 256
            or not isinstance(data, str)
            or not data
            or len(data.encode("utf-8")) > 64
        ):
            return None
        assert isinstance(sender, Mapping)
        assert isinstance(chat, Mapping)
        return _AdminCallback(
            callback_id=callback_id,
            user_id=sender["id"],
            chat_id=chat["id"],
            data=SensitiveValue(data),
        )


def _is_private_admin(sender: object, chat: object, allowed_admin_ids: frozenset[int]) -> bool:
    if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
        return False
    sender_id = sender.get("id")
    chat_id = chat.get("id")
    return (
        type(sender_id) is int
        and sender_id > 0
        and sender_id in allowed_admin_ids
        and sender.get("is_bot") is False
        and type(chat_id) is int
        and chat_id == sender_id
        and chat.get("type") == "private"
    )


def _is_forwarded(message: Mapping[str, object]) -> bool:
    return bool(set(message) & _FORWARD_MARKERS) or "sender_chat" in message


def _command_name(text: str) -> str | None:
    arguments = text.lstrip().split(maxsplit=1)
    if not arguments:
        return None
    first = arguments[0]
    if not first.startswith("/"):
        return None
    return first.split("@", maxsplit=1)[0].casefold()


def _is_for_this_bot(text: str, username: str) -> bool:
    arguments = text.lstrip().split(maxsplit=1)
    if not arguments:
        return False
    first = arguments[0]
    if "@" not in first:
        return True
    _command, addressed = first.split("@", maxsplit=1)
    return bool(addressed) and addressed.casefold() == username.casefold()


def _server_status_text(snapshot: ServerStatusSnapshot) -> str:
    return "\n".join(
        (
            "Server status:",
            f"app={snapshot.app.value}",
            f"control={snapshot.control.value}",
            f"worker={snapshot.worker.value}",
        )
    )


def _validated_now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BotAPIError("BOT_CLOCK_INVALID")
    return value


def _combined_mutation_state(states: list[BotMutationState]) -> BotMutationState | None:
    if not states:
        return None
    if BotMutationState.UNKNOWN in states:
        return BotMutationState.UNKNOWN
    if BotMutationState.REJECTED in states:
        return BotMutationState.REJECTED
    return BotMutationState.KNOWN


__all__ = [
    "ContextController",
    "ControlBotDispatcher",
    "ConversationController",
    "DispatchOutcome",
    "MemoryController",
    "ModelController",
    "PreparedDispatch",
    "PublicServiceState",
    "ServerStatusProvider",
    "ServerStatusSnapshot",
    "UpdateDisposition",
]
