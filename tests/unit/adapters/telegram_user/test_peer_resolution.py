from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.telegram_peer import PostgresTelegramPeerRepository
from telegram_userbot.adapters.telegram_user.peer import (
    TelethonPeerAdmissionResolver,
    TelethonPeerResolverError,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import TelethonUpdateScope
from telegram_userbot.application.ports.telegram_peer import (
    TelegramPeerBinding,
    TelegramPrivatePeerObservation,
)
from telegram_userbot.domain.messaging import PeerKind

ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000001")
CONVERSATION_ID = UUID("018f0000-0000-7000-8000-000000000002")
NOW = datetime(2026, 8, 26, tzinfo=UTC)


class _MappingsResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _MappingsResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _Session:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.statement: object | None = None

    async def execute(self, statement: object) -> _MappingsResult:
        self.statement = statement
        return _MappingsResult(self.row)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbound_peer_is_conversation_scoped_without_requiring_message_row() -> None:
    session = _Session(
        {
            "account_id": ACCOUNT_ID,
            "conversation_id": CONVERSATION_ID,
            "telegram_peer_id": 42,
            "access_hash": 99,
        }
    )
    repository = PostgresTelegramPeerRepository(
        cast(AsyncSession, session),
        new_uuid=lambda: UUID(int=3),
    )

    binding = await repository.resolve_outbound(
        account_id=ACCOUNT_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert binding == TelegramPeerBinding(ACCOUNT_ID, CONVERSATION_ID, 42, 99)
    sql = str(session.statement)
    assert "conversations.account_id" in sql
    assert "conversations.id" in sql
    assert "JOIN messages" not in sql
    assert "messages.account_id" not in sql
    assert "contacts.deleted_at IS NULL" in sql
    assert "contacts.automation_status" in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_private_binding_avoids_repeated_telegram_entity_rpc() -> None:
    client = AsyncMock()
    existing = AsyncMock(return_value=TelegramPeerBinding(ACCOUNT_ID, CONVERSATION_ID, 42, 99))
    admit = AsyncMock()
    deleted = AsyncMock()
    resolver = TelethonPeerAdmissionResolver(
        client=client,
        account_id=ACCOUNT_ID,
        managed_telegram_user_id=1000,
        admit_private=cast(
            Callable[
                [TelegramPrivatePeerObservation],
                Awaitable[TelegramPeerBinding],
            ],
            admit,
        ),
        lookup_existing=cast(
            Callable[[int], Awaitable[TelegramPeerBinding | None]],
            existing,
        ),
        lookup_deleted_message=cast(
            Callable[[int], Awaitable[TelegramPeerBinding | None]],
            deleted,
        ),
        now=lambda: NOW,
    )
    scope = TelethonUpdateScope(42, 42, 10, PeerKind.PRIVATE_USER)

    first = await resolver(scope)
    second = await resolver(scope)

    assert first == second
    assert first.conversation_id == CONVERSATION_ID
    assert existing.await_count == 2
    client.get_entity.assert_not_awaited()
    admit.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_binding_must_match_the_requested_private_identity() -> None:
    client = AsyncMock()
    existing = AsyncMock(return_value=TelegramPeerBinding(ACCOUNT_ID, CONVERSATION_ID, 43, 99))
    resolver = TelethonPeerAdmissionResolver(
        client=client,
        account_id=ACCOUNT_ID,
        managed_telegram_user_id=1000,
        admit_private=AsyncMock(),
        lookup_existing=existing,
        lookup_deleted_message=AsyncMock(),
        now=lambda: NOW,
    )

    with pytest.raises(TelethonPeerResolverError, match="BINDING_IDENTITY_MISMATCH"):
        await resolver(TelethonUpdateScope(42, 42, 10, PeerKind.PRIVATE_USER))
    client.get_entity.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_private_scope_with_third_party_sender_fails_closed_before_lookup() -> None:
    client = AsyncMock()
    existing = AsyncMock()
    admit = AsyncMock()
    resolver = TelethonPeerAdmissionResolver(
        client=client,
        account_id=ACCOUNT_ID,
        managed_telegram_user_id=1000,
        admit_private=admit,
        lookup_existing=existing,
        lookup_deleted_message=AsyncMock(),
        now=lambda: NOW,
    )

    admission = await resolver(TelethonUpdateScope(42, 777, 10, PeerKind.PRIVATE_USER))

    assert admission.peer_kind is PeerKind.UNKNOWN
    assert admission.conversation_id is None
    existing.assert_not_awaited()
    client.get_entity.assert_not_awaited()
    admit.assert_not_awaited()
