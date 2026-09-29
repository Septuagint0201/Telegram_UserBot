from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.telegram_peer import (
    PostgresTelegramPeerRepository,
    TelegramPeerAdmissionError,
)
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.application.ports.telegram_peer import (
    TelegramMediaBinding,
    TelegramPeerBinding,
    TelegramPrivatePeerObservation,
)
from telegram_userbot.domain.messaging import MediaKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId

ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000251")
CONTACT_ID = UUID("01900000-0000-7000-8000-000000000252")
CONVERSATION_ID = UUID("01900000-0000-7000-8000-000000000253")
PEER_ID = UUID("01900000-0000-7000-8000-000000000254")
ACCOUNT_PEER_ID = UUID("01900000-0000-7000-8000-000000000255")
MESSAGE_ID = UUID("01900000-0000-7000-8000-000000000256")
REVISION_ID = UUID("01900000-0000-7000-8000-000000000257")
NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.row = row
        self.rows = [] if rows is None else rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _Session:
    def __init__(
        self,
        *,
        results: list[_Result] | None = None,
        scalars: list[object] | None = None,
    ) -> None:
        self.results = [] if results is None else results
        self.scalars = [] if scalars is None else scalars
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self.scalars.pop(0) if self.scalars else None


def _observation() -> TelegramPrivatePeerObservation:
    return TelegramPrivatePeerObservation(
        account_id=ACCOUNT_ID,
        managed_telegram_user_id=1000,
        telegram_user_id=42,
        access_hash=99,
        username="alice",
        display_name="Alice",
        observed_is_contact=True,
        observed_at=NOW,
    )


def _binding_row() -> dict[str, object]:
    return {
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "telegram_peer_id": 42,
        "access_hash": 99,
    }


def _repository(session: _Session) -> PostgresTelegramPeerRepository:
    ids: Iterator[UUID] = iter((PEER_ID, ACCOUNT_PEER_ID, CONTACT_ID, CONVERSATION_ID))
    return PostgresTelegramPeerRepository(
        cast(AsyncSession, session),
        new_uuid=lambda: next(ids),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admit_private_creates_scoped_rows_and_returns_binding() -> None:
    session = _Session(
        results=[
            _Result({"status": "active", "telegram_user_id": 1000}),
            _Result(),
            _Result({"id": CONTACT_ID, "automation_status": "review", "deleted_at": None}),
            _Result(),
            _Result({"id": CONVERSATION_ID, "deleted_at": None}),
        ],
        scalars=[PEER_ID, ACCOUNT_PEER_ID],
    )
    binding = await _repository(session).admit_private(_observation())
    assert binding == TelegramPeerBinding(ACCOUNT_ID, CONVERSATION_ID, 42, 99)
    assert len(session.statements) == 7
    assert "ON CONFLICT" in str(
        cast(Any, session.statements[1]).compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admit_private_rejects_account_identity_upserts_and_deleted_contact() -> None:
    with pytest.raises(TelegramPeerAdmissionError, match="ACCOUNT_NOT_ACTIVE"):
        await _repository(_Session(results=[_Result(None)])).admit_private(_observation())

    with pytest.raises(TelegramPeerAdmissionError, match="ACCOUNT_IDENTITY_MISMATCH"):
        await _repository(
            _Session(results=[_Result({"status": "active", "telegram_user_id": 7})])
        ).admit_private(_observation())

    with pytest.raises(TelegramPeerAdmissionError, match="PEER_UPSERT_FAILED"):
        await _repository(
            _Session(
                results=[_Result({"status": "active", "telegram_user_id": 1000})],
                scalars=[None],
            )
        ).admit_private(_observation())

    with pytest.raises(TelegramPeerAdmissionError, match="ACCOUNT_PEER_UPSERT_FAILED"):
        await _repository(
            _Session(
                results=[_Result({"status": "active", "telegram_user_id": 1000})],
                scalars=[PEER_ID, None],
            )
        ).admit_private(_observation())

    with pytest.raises(TelegramPeerAdmissionError, match="CONTACT_NOT_ADMISSIBLE"):
        await _repository(
            _Session(
                results=[
                    _Result({"status": "active", "telegram_user_id": 1000}),
                    _Result(),
                    _Result(
                        {"id": CONTACT_ID, "automation_status": "deleting", "deleted_at": None}
                    ),
                ],
                scalars=[PEER_ID, ACCOUNT_PEER_ID],
            )
        ).admit_private(_observation())

    with pytest.raises(TelegramPeerAdmissionError, match="CONVERSATION_NOT_ADMISSIBLE"):
        await _repository(
            _Session(
                results=[
                    _Result({"status": "active", "telegram_user_id": 1000}),
                    _Result(),
                    _Result({"id": CONTACT_ID, "automation_status": "review", "deleted_at": None}),
                    _Result(None),
                ],
                scalars=[PEER_ID, ACCOUNT_PEER_ID],
            )
        ).admit_private(_observation())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_lookups_distinguish_missing_duplicate_and_current_rows() -> None:
    session = _Session(
        results=[
            _Result(rows=[]),
            _Result(rows=[_binding_row()]),
            _Result(_binding_row()),
            _Result(None),
        ]
    )
    repository = _repository(session)
    assert (
        await repository.find_private_for_deleted_message(
            account_id=ACCOUNT_ID, telegram_message_id=7
        )
        is None
    )
    found_deleted = await repository.find_private_for_deleted_message(
        account_id=ACCOUNT_ID, telegram_message_id=7
    )
    assert found_deleted == TelegramPeerBinding(ACCOUNT_ID, CONVERSATION_ID, 42, 99)
    assert (
        await repository.find_private_by_chat(account_id=ACCOUNT_ID, telegram_user_id=42)
        == found_deleted
    )
    assert (
        await repository.resolve_outbound(account_id=ACCOUNT_ID, conversation_id=CONVERSATION_ID)
        is None
    )
    deleted_sql = str(cast(Any, session.statements[0]))
    assert "messages" in deleted_sql


def _image_request() -> TelegramImageDownloadRequest:
    return TelegramImageDownloadRequest(
        AccountId(ACCOUNT_ID),
        ConversationId(CONVERSATION_ID),
        MessageId(MESSAGE_ID),
        1,
        0,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_image_resolution_and_pending_listing_are_account_scoped() -> None:
    image_row = {
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": MESSAGE_ID,
        "revision_no": 1,
        "position": 0,
        "media_kind": MediaKind.PHOTO.value,
        "telegram_file_ref": "opaque-ref",
        "declared_mime": "image/jpeg",
        "declared_size": 123,
    }
    pending_row = {
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": MESSAGE_ID,
        "revision_no": 1,
        "position": 0,
    }
    session = _Session(results=[_Result(image_row), _Result(None), _Result(rows=[pending_row])])
    repository = _repository(session)
    resolved = await repository.resolve_image(_image_request())
    assert resolved == TelegramMediaBinding(
        ACCOUNT_ID,
        CONVERSATION_ID,
        MESSAGE_ID,
        1,
        0,
        MediaKind.PHOTO,
        "opaque-ref",
        "image/jpeg",
        123,
    )
    assert await repository.resolve_image(_image_request()) is None
    pending = await repository.list_pending_images(account_id=ACCOUNT_ID, limit=10)
    assert pending == (_image_request(),)
    with pytest.raises(ValueError, match="limit"):
        await repository.list_pending_images(account_id=ACCOUNT_ID, limit=101)
