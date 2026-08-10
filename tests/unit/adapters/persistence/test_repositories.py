from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    AccountRecord,
    JobState,
    NewJobRecord,
)
from telegram_userbot.adapters.persistence.repositories import (
    AccountRepository,
    ConversationRepository,
    DurableJobRepository,
    MessageRedactionRepository,
    OutboxRepository,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeResult:
    def __init__(
        self,
        *,
        rows: Sequence[Any] = (),
        scalar: object = None,
        rowcount: int = 0,
    ) -> None:
        self.rows = list(rows)
        self.scalar = scalar
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        return self.rows[0]

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def all(self) -> list[Any]:
        return self.rows

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeSession:
    def __init__(
        self,
        *,
        execute_results: Sequence[FakeResult] = (),
        scalar_results: Sequence[object] = (),
    ) -> None:
        self.execute_results = list(execute_results)
        self.scalar_results = list(scalar_results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.execute_results.pop(0) if self.execute_results else FakeResult()

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self.scalar_results.pop(0)


def session(
    *,
    execute_results: Sequence[FakeResult] = (),
    scalar_results: Sequence[object] = (),
) -> tuple[AsyncSession, FakeSession]:
    fake = FakeSession(execute_results=execute_results, scalar_results=scalar_results)
    return cast(AsyncSession, fake), fake


def account_row(account_id: UUID) -> dict[str, object]:
    return {
        "id": account_id,
        "telegram_user_id": 42,
        "display_label": "owner",
        "status": "active",
        "default_timezone": "Asia/Shanghai",
    }


def conversation_row(conversation_id: UUID, account_id: UUID) -> dict[str, object]:
    return {
        "id": conversation_id,
        "account_id": account_id,
        "mode_version": 2,
        "content_revision": 3,
        "base_mode_override": "AUTO",
        "contact_paused": False,
    }


def job_row(job_id: UUID, owner: UUID | None = None) -> dict[str, object]:
    return {
        "id": job_id,
        "account_id": None,
        "queue_name": "memory",
        "job_type": "refresh",
        "state": "leased" if owner else "pending",
        "priority": 5,
        "payload": {"conversation_id": str(uuid7())},
        "attempt_count": 1 if owner else 0,
        "max_attempts": 5,
        "available_at": NOW,
        "lease_owner": owner,
        "lease_expires_at": NOW + timedelta(seconds=60) if owner else None,
        "version": 2 if owner else 1,
        "fencing_token": 1 if owner else 0,
        "dispatch_generation": 1,
    }


@pytest.mark.unit
async def test_account_and_conversation_repositories_detach_records() -> None:
    account_id = uuid7()
    conversation_id = uuid7()
    sql_session, _ = session(
        execute_results=[
            FakeResult(),
            FakeResult(rows=[account_row(account_id)]),
            FakeResult(rows=[]),
            FakeResult(rows=[conversation_row(conversation_id, account_id)]),
            FakeResult(rows=[]),
            FakeResult(rows=[conversation_row(conversation_id, account_id)]),
        ]
    )
    accounts = AccountRepository(sql_session)
    await accounts.add(AccountRecord(account_id, 42, "owner", "active", "Asia/Shanghai"))
    assert (await accounts.get(account_id)) == AccountRecord(
        account_id, 42, "owner", "active", "Asia/Shanghai"
    )
    assert await accounts.get(uuid7()) is None

    conversations = ConversationRepository(sql_session)
    current = await conversations.get(conversation_id)
    assert current is not None
    assert current.mode_version == 2
    assert (
        await conversations.compare_and_set_mode(
            conversation_id=conversation_id,
            expected_version=1,
            base_mode_override="HUMAN",
            contact_paused=True,
            now=NOW,
        )
        is None
    )
    changed = await conversations.compare_and_set_mode(
        conversation_id=conversation_id,
        expected_version=2,
        base_mode_override="AUTO",
        contact_paused=False,
        now=NOW,
    )
    assert changed is not None
    assert changed.content_revision == 3


@pytest.mark.unit
async def test_job_create_is_idempotent_and_writes_wakeup() -> None:
    job_id = uuid7()
    new_job = NewJobRecord(
        job_id,
        None,
        "memory",
        "refresh",
        b"a" * 32,
        {"conversation_id": str(uuid7())},
        NOW,
    )
    sql_session, fake = session(execute_results=[FakeResult()], scalar_results=[job_id])
    assert await DurableJobRepository(sql_session).create(new_job) == job_id
    assert len(fake.statements) == 2

    sql_session, _ = session(scalar_results=[None, job_id])
    assert await DurableJobRepository(sql_session).create(new_job) == job_id

    sql_session, _ = session(scalar_results=[None, None])
    with pytest.raises(RuntimeError, match="disappeared"):
        await DurableJobRepository(sql_session).create(new_job)


@pytest.mark.unit
async def test_job_claim_lease_fencing_and_recovery_paths() -> None:
    owner = uuid7()
    job_id = uuid7()
    sql_session, _ = session(execute_results=[FakeResult(scalar=None)])
    jobs = DurableJobRepository(sql_session)
    assert await jobs.claim_next(queue_name="memory", owner=owner, now=NOW) is None

    sql_session, _ = session(
        execute_results=[
            FakeResult(scalar=job_id),
            FakeResult(rows=[job_row(job_id, owner)]),
            FakeResult(rowcount=1),
            FakeResult(rowcount=0),
            FakeResult(rowcount=1),
            FakeResult(rowcount=2),
        ]
    )
    jobs = DurableJobRepository(sql_session)
    claimed = await jobs.claim_next(queue_name="memory", owner=owner, now=NOW)
    assert claimed is not None
    assert claimed.state is JobState.LEASED
    assert claimed.fencing_token == 1
    assert await jobs.renew(job_id=job_id, owner=owner, fencing_token=1, now=NOW)
    assert not await jobs.renew(job_id=job_id, owner=uuid7(), fencing_token=1, now=NOW)
    assert await jobs.complete(job_id=job_id, owner=owner, fencing_token=1, now=NOW)
    assert await jobs.recover_expired(now=NOW) == 2


@pytest.mark.unit
async def test_job_notification_rebuild_and_outbox_repository() -> None:
    job_a = uuid7()
    job_b = uuid7()
    sql_session, fake = session(
        execute_results=[
            FakeResult(rows=[(job_a, None, 2), (job_b, uuid7(), 3)]),
            FakeResult(),
            FakeResult(),
        ]
    )
    assert await DurableJobRepository(sql_session).rebuild_notifications(queue_name="memory") == 2
    assert len(fake.statements) == 3

    outbox_row = {
        "id": 10,
        "topic": "durable_job.available",
        "aggregate_type": "background_job",
        "aggregate_id": str(job_a),
        "aggregate_version": 2,
        "payload": {"job_id": str(job_a), "dispatch_generation": 2},
    }
    sql_session, _ = session(
        execute_results=[
            FakeResult(rows=[outbox_row]),
            FakeResult(rowcount=1),
            FakeResult(rowcount=0),
        ]
    )
    outbox = OutboxRepository(sql_session)
    with pytest.raises(ValueError, match="batch limit"):
        await outbox.claim_batch(limit=0)
    records = await outbox.claim_batch(limit=1)
    assert records[0].id == 10
    assert await outbox.mark_published(outbox_id=10, now=NOW)
    assert not await outbox.record_failure(outbox_id=999, error_code="redis_offline")


@pytest.mark.unit
async def test_message_redaction_hides_content_and_rejects_unknown_reason() -> None:
    account_id = uuid7()
    message_id = uuid7()
    cleanup = NewJobRecord(
        uuid7(),
        account_id,
        "erasure",
        "reconcile_deleted_message",
        b"e" * 32,
        {"message_id": str(message_id)},
        NOW,
    )
    sql_session, fake = session(
        execute_results=[
            FakeResult(rowcount=2),
            FakeResult(rowcount=1),
            FakeResult(),
            FakeResult(rows=[SimpleNamespace(text_content="hello", caption=None)]),
            FakeResult(rows=[]),
        ],
        scalar_results=[cleanup.id],
    )
    repository = MessageRedactionRepository(sql_session)
    with pytest.raises(ValueError, match="unknown redaction"):
        await repository.redact_message(
            account_id=account_id,
            message_id=message_id,
            reason="restore",
            now=NOW,
            cleanup_job=cleanup,
        )
    with pytest.raises(ValueError, match="exact message scope"):
        await repository.redact_message(
            account_id=account_id,
            message_id=message_id,
            reason="policy",
            now=NOW,
            cleanup_job=NewJobRecord(
                cleanup.id,
                None,
                cleanup.queue_name,
                cleanup.job_type,
                cleanup.idempotency_key,
                cleanup.payload,
                cleanup.available_at,
            ),
        )
    assert (
        await repository.redact_message(
            account_id=account_id,
            message_id=message_id,
            reason="telegram_delete",
            now=NOW,
            cleanup_job=cleanup,
        )
        == 2
    )
    redaction_sql = str(fake.statements[0])
    assert "entities=NULL" in redaction_sql.replace(" ", "")
    assert await repository.visible_current_text(account_id=account_id, message_id=message_id) == (
        "hello",
        None,
    )
    assert await repository.visible_current_text(account_id=account_id, message_id=uuid7()) is None
