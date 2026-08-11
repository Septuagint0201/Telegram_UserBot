"""Telethon side-effect adapter with an injected client and no Session ownership."""

from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from telethon import functions, types  # type: ignore[import-untyped]
from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]

from telegram_userbot.application.ports.telegram import (
    TelegramFloodWaitError,
    TelegramGateway,
    TelegramPermanentError,
    TelegramReadReceipt,
    TelegramReadRequest,
    TelegramSendReceipt,
    TelegramSendUnknownError,
    TelegramTextRequest,
    TelegramTransientError,
    TelegramTypingAction,
    TelegramTypingRequest,
)
from telegram_userbot.domain.shared.ids import AccountId, ConversationId


class TelethonClient(Protocol):
    def __call__(self, request: object) -> Awaitable[object]: ...


class MessageResponse(Protocol):
    id: int


PeerResolver = Callable[[AccountId, ConversationId], Awaitable[object]]


class TelethonTelegramGateway(TelegramGateway):
    """The app process injects its sole connected Telethon client here."""

    def __init__(self, client: TelethonClient, resolve_peer: PeerResolver) -> None:
        self._client = client
        self._resolve_peer = resolve_peer

    async def send_text(self, request: TelegramTextRequest) -> TelegramSendReceipt:
        peer = await self._resolve_peer(request.account_id, request.conversation_id)
        rpc = functions.messages.SendMessageRequest(
            peer=peer,
            message=request.text.reveal_for_use(),
            random_id=request.random_id,
            no_webpage=True,
        )
        try:
            response = await self._client(rpc)
        except FloodWaitError as error:
            raise TelegramFloodWaitError(error.seconds) from error
        except (ConnectionError, TimeoutError, OSError) as error:
            raise TelegramSendUnknownError("send_unknown") from error
        except RPCError as error:
            raise TelegramPermanentError("telegram_rpc_rejected") from error
        message_id = getattr(response, "id", None)
        if not isinstance(message_id, int):
            raise TelegramTransientError("telegram_response_missing_message_id")
        return TelegramSendReceipt(message_id)

    async def acknowledge_read(self, request: TelegramReadRequest) -> TelegramReadReceipt:
        peer = await self._resolve_peer(request.account_id, request.conversation_id)
        try:
            await self._client(
                functions.messages.ReadHistoryRequest(
                    peer=peer,
                    max_id=request.max_telegram_message_id,
                )
            )
        except FloodWaitError as error:
            raise TelegramFloodWaitError(error.seconds) from error
        except (ConnectionError, TimeoutError, OSError) as error:
            raise TelegramTransientError("read_transport_error") from error
        except RPCError as error:
            raise TelegramPermanentError("read_rpc_rejected") from error
        return TelegramReadReceipt(request.max_telegram_message_id)

    async def set_typing(self, request: TelegramTypingRequest) -> None:
        peer = await self._resolve_peer(request.account_id, request.conversation_id)
        action: types.TypeSendMessageAction
        if request.action is TelegramTypingAction.STOP:
            action = types.SendMessageCancelAction()
        else:
            action = types.SendMessageTypingAction()
        try:
            await self._client(functions.messages.SetTypingRequest(peer=peer, action=action))
        except FloodWaitError as error:
            raise TelegramFloodWaitError(error.seconds) from error
        except (ConnectionError, TimeoutError, OSError) as error:
            raise TelegramTransientError("typing_transport_error") from error
        except RPCError as error:
            raise TelegramPermanentError("typing_rpc_rejected") from error


def assert_injected_client(value: object) -> TelethonClient:
    """Narrow the composition-root object without constructing a TelegramClient."""

    if not callable(value):
        raise TypeError("injected Telethon client must be callable")
    return cast(TelethonClient, value)
