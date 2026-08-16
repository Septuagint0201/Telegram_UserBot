from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.media_repository import (
    MediaDeletionLease,
    MediaRepository,
)


class _Result:
    def __init__(
        self,
        value: object | None = None,
        *,
        rows: list[dict[str, object]] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._value = value
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows

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
    account_id, first, second = uuid7(), uuid7(), uuid7()
    session = AsyncMock()
    session.execute.return_value = _Result(
        rows=[
            {
                "id": first,
                "account_id": account_id,
                "status": "ready",
                "storage_key": "first.png",
                "sha256": b"a" * 32,
                "delete_fencing_token": 0,
                "delete_attempt_count": 0,
                "delete_first_failed_at": None,
                "delete_critical_alerted_at": None,
            },
            {
                "id": second,
                "account_id": account_id,
                "status": "failed",
                "storage_key": "second.png",
                "sha256": b"b" * 32,
                "delete_fencing_token": 3,
                "delete_attempt_count": 2,
                "delete_first_failed_at": datetime(2029, 12, 31, tzinfo=UTC),
                "delete_critical_alerted_at": None,
            },
        ]
    )
    session.scalar.side_effect = [None, 4]
    repository = MediaRepository(cast(AsyncSession, session))

    assert await repository.claim_expired(now=datetime(2030, 1, 1, tzinfo=UTC)) == (
        MediaDeletionLease(
            second,
            account_id,
            "second.png",
            b"b" * 32,
            4,
            3,
            datetime(2029, 12, 31, tzinfo=UTC),
        ),
    )
    pg_dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    select_sql = str(session.execute.await_args.args[0].compile(dialect=pg_dialect))
    assert "FOR UPDATE" in select_sql
    assert "SKIP LOCKED" in select_sql
    assert session.scalar.await_count == 2
    claim_sql = str(session.scalar.await_args_list[1].args[0])
    assert claim_sql.startswith("UPDATE media_objects")
    assert "message_media.media_object_id" in claim_sql
    assert "RETURNING media_objects.delete_fencing_token" in claim_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_media_cleanup_finish_uses_fencing_and_exponential_backoff() -> None:
    session = AsyncMock()
    session.execute.return_value = _Result(rowcount=1)
    repository = MediaRepository(cast(AsyncSession, session))
    lease = MediaDeletionLease(uuid7(), uuid7(), "object.png", b"h" * 32, 7, 3)
    now = datetime(2030, 1, 1, tzinfo=UTC)

    outcome = await repository.finish_deletion(
        deletion=lease,
        deleted=False,
        now=now,
        error_code="synthetic_failure",
    )
    assert outcome.completed
    assert not outcome.critical_alert
    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert lease.fencing_token in params.values()
    assert now + timedelta(minutes=4) in params.values()
    assert "synthetic_failure" in params.values()
    assert now in params.values()

    session.execute.return_value = _Result(rowcount=0)
    assert not (await repository.finish_deletion(deletion=lease, deleted=True, now=now)).completed

    session.execute.return_value = _Result(rowcount=1)
    critical = await repository.finish_deletion(
        deletion=MediaDeletionLease(
            lease.object_id,
            lease.account_id,
            lease.storage_key,
            lease.sha256,
            lease.fencing_token,
            4,
            now - timedelta(hours=24),
        ),
        deleted=False,
        now=now,
    )
    assert critical == type(critical)(completed=True, critical_alert=True)
    assert "delete_critical_alerted_at" in str(session.execute.await_args.args[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_media_cleanup_claim_rejects_invalid_policy() -> None:
    repository = MediaRepository(cast(AsyncSession, AsyncMock()))
    now = datetime(2030, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="claim policy"):
        await repository.claim_expired(now=now, limit=0)
    with pytest.raises(ValueError, match="claim policy"):
        await repository.claim_expired(now=now, lease=timedelta(minutes=16))
