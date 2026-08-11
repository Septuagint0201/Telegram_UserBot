from dataclasses import dataclass, field
from uuid import UUID

import pytest
from telethon import functions, types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.telethon import TelethonTelegramGateway
from telegram_userbot.application.ports.telegram import (
    TelegramReadRequest,
    TelegramSendUnknownError,
    TelegramTextRequest,
    TelegramTypingAction,
    TelegramTypingRequest,
)
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue


@dataclass(slots=True)
class Response:
    id: int


@dataclass(slots=True)
class InjectedClient:
    requests: list[object] = field(default_factory=list)
    failure: BaseException | None = None

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if isinstance(request, functions.messages.SendMessageRequest):
            return Response(700)
        return object()


async def resolve_peer(account_id: AccountId, conversation_id: ConversationId) -> object:
    assert account_id.value == UUID(int=1)
    assert conversation_id.value == UUID(int=2)
    return types.InputPeerUser(42, 99)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_telethon_gateway_builds_requests_with_injected_client_only() -> None:
    client = InjectedClient()
    gateway = TelethonTelegramGateway(client, resolve_peer)
    account_id = AccountId(UUID(int=1))
    conversation_id = ConversationId(UUID(int=2))
    receipt = await gateway.send_text(
        TelegramTextRequest(
            account_id,
            conversation_id,
            RunId(UUID(int=3)),
            123,
            SensitiveValue("synthetic output"),
        )
    )
    await gateway.acknowledge_read(TelegramReadRequest(account_id, conversation_id, 88))
    await gateway.set_typing(
        TelegramTypingRequest(account_id, conversation_id, TelegramTypingAction.START)
    )
    await gateway.set_typing(
        TelegramTypingRequest(account_id, conversation_id, TelegramTypingAction.STOP)
    )

    assert receipt.telegram_message_id == 700
    send = client.requests[0]
    read = client.requests[1]
    typing_start = client.requests[2]
    typing_stop = client.requests[3]
    assert isinstance(send, functions.messages.SendMessageRequest)
    assert send.random_id == 123
    assert send.message == "synthetic output"
    assert isinstance(read, functions.messages.ReadHistoryRequest)
    assert read.max_id == 88
    assert isinstance(typing_start, functions.messages.SetTypingRequest)
    assert isinstance(typing_start.action, types.SendMessageTypingAction)
    assert isinstance(typing_stop, functions.messages.SetTypingRequest)
    assert isinstance(typing_stop.action, types.SendMessageCancelAction)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_telethon_send_transport_failure_is_unknown_not_retryable() -> None:
    gateway = TelethonTelegramGateway(InjectedClient(failure=ConnectionError()), resolve_peer)
    with pytest.raises(TelegramSendUnknownError):
        await gateway.send_text(
            TelegramTextRequest(
                AccountId(UUID(int=1)),
                ConversationId(UUID(int=2)),
                RunId(UUID(int=3)),
                123,
                SensitiveValue("synthetic output"),
            )
        )
