from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.media_repository import MediaRepository


class _Result:
    def __init__(self, value: object | None = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _Session:
    def __init__(self, *, account_id: UUID | None, attached_id: UUID | None = None) -> None:
        self.account_id = account_id
        self.attached_id = attached_id
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.account_id

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.attached_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_media_attach_locks_ready_object_and_binds_account_scope() -> None:
    account_id, attached_id = uuid7(), uuid7()
    session = _Session(account_id=account_id, attached_id=attached_id)
    repository = MediaRepository(cast(AsyncSession, session))

    assert await repository.attach_to_revision(
        message_revision_id=uuid7(), position=0, media_object_id=uuid7()
    )
    assert len(session.statements) == 2
    lock_sql, attach_sql = map(str, session.statements)
    assert "FOR UPDATE" in lock_sql
    assert "media_objects.status" in lock_sql
    assert "message_media.account_id" in attach_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_media_attach_rejects_non_ready_object_before_revision_update() -> None:
    session = _Session(account_id=None)
    repository = MediaRepository(cast(AsyncSession, session))

    assert not await repository.attach_to_revision(
        message_revision_id=uuid7(), position=0, media_object_id=uuid7()
    )
    assert len(session.statements) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_media_cleanup_claim_rechecks_references_after_lock_wait() -> None:
    first, second = uuid7(), uuid7()
    session = AsyncMock()
    session.scalars.side_effect = [(first, second), (second,)]
    repository = MediaRepository(cast(AsyncSession, session))

    assert await repository.claim_expired(now=datetime(2030, 1, 1, tzinfo=UTC)) == (second,)
    assert session.scalars.await_count == 2
    claim_sql = str(session.scalars.await_args_list[1].args[0])
    assert claim_sql.startswith("UPDATE media_objects")
    assert "message_media.media_object_id" in claim_sql
    assert "RETURNING media_objects.id" in claim_sql
