from datetime import UTC, datetime
from uuid import uuid7

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import accounts, contacts
from telegram_userbot.adapters.persistence.telegram_peer import (
    PostgresTelegramPeerRepository,
)
from telegram_userbot.application.ports.telegram_peer import (
    TelegramPrivatePeerObservation,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_private_peer_binding_is_account_scoped_and_rejected_after_contact_delete(
    db_session: AsyncSession,
) -> None:
    account_id = uuid7()
    await db_session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=1000,
            display_label="m8-synthetic-telethon-owner",
            status="active",
        )
    )
    repository = PostgresTelegramPeerRepository(db_session, new_uuid=uuid7)
    binding = await repository.admit_private(
        TelegramPrivatePeerObservation(
            account_id=account_id,
            managed_telegram_user_id=1000,
            telegram_user_id=42,
            access_hash=99,
            username="synthetic_peer",
            display_name="Synthetic Peer",
            observed_is_contact=False,
            observed_at=NOW,
        )
    )

    assert binding.account_id == account_id
    assert binding.telegram_user_id == 42
    assert (
        await repository.find_private_by_chat(
            account_id=account_id,
            telegram_user_id=42,
        )
        == binding
    )
    assert (
        await repository.resolve_outbound(
            account_id=account_id,
            conversation_id=binding.conversation_id,
        )
        == binding
    )

    await db_session.execute(
        update(contacts)
        .where(contacts.c.account_id == account_id)
        .values(automation_status="deleting", deleted_at=NOW, updated_at=NOW)
    )

    assert (
        await repository.find_private_by_chat(
            account_id=account_id,
            telegram_user_id=42,
        )
        is None
    )
    assert (
        await repository.resolve_outbound(
            account_id=account_id,
            conversation_id=binding.conversation_id,
        )
        is None
    )
