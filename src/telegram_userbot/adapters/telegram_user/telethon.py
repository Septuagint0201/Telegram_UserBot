"""Telethon side-effect adapter with an injected connected client."""

from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from telethon import functions, types  # type: ignore[import-untyped]
from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl import custom  # type: ignore[import-untyped]

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


PeerResolver = Callable[[AccountId, ConversationId], Awaitable[object]]


def _valid_message_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _sent_message_id(response: object, *, random_id: int) -> int:
    """Extract the result without guessing which outgoing request was accepted."""

    if isinstance(response, (types.Message, custom.Message, types.UpdateShortSentMessage)):
        message_id = _valid_message_id(response.id)
        if message_id is not None:
            return message_id
        raise TelegramSendUnknownError("telegram_response_message_id_invalid")

    if isinstance(response, (types.Updates, types.UpdatesCombined)):
        mappings = tuple(
            update for update in response.updates if isinstance(update, types.UpdateMessageID)
        )
        matching_ids = {
            message_id
            for update in mappings
            if update.random_id == random_id
            if (message_id := _valid_message_id(update.id)) is not None
        }
        if len(matching_ids) == 1:
            return matching_ids.pop()
        if len(matching_ids) > 1:
            raise TelegramSendUnknownError("telegram_response_message_id_ambiguous")
        if mappings:
            raise TelegramSendUnknownError("telegram_response_random_id_mismatch")

    # The RPC may already have been accepted. Treat every unprovable response as
    # unknown so the delivery state machine reconciles instead of blindly retrying.
    raise TelegramSendUnknownError("telegram_response_missing_message_id")


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
        return TelegramSendReceipt(_sent_message_id(response, random_id=request.random_id))

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
