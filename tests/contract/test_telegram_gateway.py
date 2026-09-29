from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from telethon import functions, types  # type: ignore[import-untyped]
from telethon.tl import custom  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.telethon import (
    TelethonTelegramGateway,
    assert_injected_client,
)
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
class InjectedClient:
    requests: list[object] = field(default_factory=list)
    failure: BaseException | None = None
    send_response: object = field(
        default_factory=lambda: types.UpdateShortSentMessage(
            id=700,
            pts=1,
            pts_count=1,
            date=datetime(2026, 8, 24, tzinfo=UTC),
        )
    )

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if isinstance(request, functions.messages.SendMessageRequest):
            return self.send_response
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


@pytest.mark.contract
@pytest.mark.asyncio
async def test_telethon_send_requires_unambiguous_message_id_in_response() -> None:
    gateway = TelethonTelegramGateway(InjectedClient(send_response=object()), resolve_peer)
    with pytest.raises(TelegramSendUnknownError, match="missing_message_id"):
        await gateway.send_text(
            TelegramTextRequest(
                AccountId(UUID(int=1)),
                ConversationId(UUID(int=2)),
                RunId(UUID(int=3)),
                123,
                SensitiveValue("synthetic output"),
            )
        )


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_telethon_send_matches_random_id_in_updates(combined: bool) -> None:
    updates = [
        types.UpdateMessageID(id=900, random_id=999),
        types.UpdateMessageID(id=701, random_id=123),
    ]
    response: object
    if combined:
        response = types.UpdatesCombined(updates, [], [], None, seq_start=1, seq=2)
    else:
        response = types.Updates(updates, [], [], None, seq=2)
    gateway = TelethonTelegramGateway(InjectedClient(send_response=response), resolve_peer)

    receipt = await gateway.send_text(
        TelegramTextRequest(
            AccountId(UUID(int=1)),
            ConversationId(UUID(int=2)),
            RunId(UUID(int=3)),
            123,
            SensitiveValue("synthetic output"),
        )
    )

    assert receipt.telegram_message_id == 701


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        message_type(
            id=702,
            peer_id=types.PeerUser(42),
            date=datetime(2026, 8, 24, tzinfo=UTC),
            message="accepted",
        )
        for message_type in (types.Message, custom.Message)
    ],
)
async def test_telethon_send_accepts_direct_message_response(response: object) -> None:
    gateway = TelethonTelegramGateway(InjectedClient(send_response=response), resolve_peer)

    receipt = await gateway.send_text(
        TelegramTextRequest(
            AccountId(UUID(int=1)),
            ConversationId(UUID(int=2)),
            RunId(UUID(int=3)),
            123,
            SensitiveValue("synthetic output"),
        )
    )

    assert receipt.telegram_message_id == 702


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ([types.UpdateMessageID(id=700, random_id=999)], "random_id_mismatch"),
        (
            [
                types.UpdateMessageID(id=700, random_id=123),
                types.UpdateMessageID(id=701, random_id=123),
            ],
            "message_id_ambiguous",
        ),
    ],
)
async def test_telethon_send_rejects_wrong_or_ambiguous_random_id(
    updates: list[object], code: str
) -> None:
    response = types.Updates(updates, [], [], None, seq=1)
    gateway = TelethonTelegramGateway(InjectedClient(send_response=response), resolve_peer)
    with pytest.raises(TelegramSendUnknownError, match=code):
        await gateway.send_text(
            TelegramTextRequest(
                AccountId(UUID(int=1)),
                ConversationId(UUID(int=2)),
                RunId(UUID(int=3)),
                123,
                SensitiveValue("synthetic output"),
            )
        )


@pytest.mark.contract
def test_telethon_client_must_be_injected_as_a_callable() -> None:
    client = InjectedClient()
    assert assert_injected_client(client) is client
    with pytest.raises(TypeError, match="must be callable"):
        assert_injected_client(object())
