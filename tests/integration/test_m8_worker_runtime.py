from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.schema import (
    accounts,
    background_jobs,
    data_erasure_requests,
    erasure_ledger,
    erasure_progress,
    memories,
    transactional_outbox,
)
from telegram_userbot.adapters.persistence.worker_runtime import (
    DURABLE_JOB_TOPIC,
    WorkerOutboxRepository,
)
from telegram_userbot.processes.worker import DurableCompensationPublisher

NOW = datetime(2030, 1, 1, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_worker_outbox_leaves_unrelated_topics_untouched(
    db_session: AsyncSession,
) -> None:
    job_id = uuid7()
    await db_session.execute(
        insert(background_jobs).values(
            id=job_id,
            account_id=None,
            queue_name="worker",
            job_type="maintenance.synthetic",
            idempotency_key=hashlib.sha256(job_id.bytes).digest(),
            state="pending",
            payload_schema_version=1,
            payload={},
            available_at=NOW,
            dispatch_generation=1,
        )
    )
    worker_outbox_id = await db_session.scalar(
        insert(transactional_outbox)
        .values(
            account_id=None,
            topic=DURABLE_JOB_TOPIC,
            aggregate_type="background_job",
            aggregate_id=str(job_id),
            aggregate_version=1,
            payload_schema_version=1,
            payload={"job_id": str(job_id), "dispatch_generation": 1},
        )
        .returning(transactional_outbox.c.id)
    )
    unrelated_id = await db_session.scalar(
        insert(transactional_outbox)
        .values(
            account_id=None,
            topic="model_configuration.activated",
            aggregate_type="model_configuration",
            aggregate_id=str(uuid7()),
            aggregate_version=1,
            payload_schema_version=1,
            payload={},
        )
        .returning(transactional_outbox.c.id)
    )
    assert worker_outbox_id is not None
    assert unrelated_id is not None

    repository = WorkerOutboxRepository(db_session)
    records = await repository.due_wakeups(now=NOW)

    assert [record.id for record in records] == [worker_outbox_id]
    assert await repository.mark_published(outbox_id=worker_outbox_id, now=NOW)
    state_rows = (
        await db_session.execute(
            select(
                transactional_outbox.c.id,
                transactional_outbox.c.published_at,
            ).where(transactional_outbox.c.id.in_((worker_outbox_id, unrelated_id)))
        )
    ).mappings()
    states: dict[int, datetime | None] = {
        int(row[transactional_outbox.c.id]): row[transactional_outbox.c.published_at]
        for row in state_rows
    }
    assert states[worker_outbox_id] == NOW
    assert states[unrelated_id] is None


@pytest.mark.integration
async def test_worker_reconciles_queued_memory_erasure_with_progress_and_ledger(
    db_session: AsyncSession,
) -> None:
    account_id = uuid7()
    memory_id = uuid7()
    request_id = uuid7()
    await db_session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="erasure-worker",
            status="active",
        )
    )
    await db_session.execute(
        insert(memories).values(
            id=memory_id,
            account_id=account_id,
            memory_type="fact",
            semantic_key_hash=b"m" * 32,
            status="active",
            current_version_no=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db_session.execute(
        insert(data_erasure_requests).values(
            id=request_id,
            account_id=account_id,
            scope_type="memory",
            memory_id=memory_id,
            state="requested",
            requested_by="integration",
            request_idempotency_key=b"r" * 32,
            policy_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    assert await MemoryRepository(db_session).reconcile_erasure_request(
        account_id=account_id,
        request_id=request_id,
        erasure_scope_secret=b"e" * 32,
        now=NOW,
    )
    assert (
        await MemoryRepository(db_session).reconcile_erasure_request(
            account_id=account_id,
            request_id=request_id,
            erasure_scope_secret=b"e" * 32,
            now=NOW,
        )
        is False
    )
    assert (
        await db_session.scalar(select(memories.c.status).where(memories.c.id == memory_id))
        == "forgotten"
    )
    assert (
        await db_session.scalar(
            select(data_erasure_requests.c.state).where(data_erasure_requests.c.id == request_id)
        )
        == "completed"
    )
    assert set(
        await db_session.scalars(
            select(erasure_progress.c.state).where(erasure_progress.c.request_id == request_id)
        )
    ) == {"completed"}
    assert (
        await db_session.scalar(
            select(erasure_ledger.c.request_id).where(erasure_ledger.c.request_id == request_id)
        )
        == request_id
    )


@pytest.mark.integration
async def test_worker_compensation_publishes_queued_erasure_without_content_payload(
    postgres_engine: AsyncEngine,
) -> None:
    connection = await postgres_engine.connect()
    transaction = await connection.begin()
    sessions = async_sessionmaker(
        connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        account_id = uuid7()
        request_id = uuid7()
        async with sessions() as setup, setup.begin():
            await setup.execute(
                insert(accounts).values(
                    id=account_id,
                    telegram_user_id=account_id.int % 2**63,
                    display_label="erasure-publisher",
                    status="active",
                )
            )
            await setup.execute(
                insert(data_erasure_requests).values(
                    id=request_id,
                    account_id=account_id,
                    scope_type="account",
                    state="requested",
                    requested_by="integration",
                    request_idempotency_key=b"p" * 32,
                    policy_version=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        result = await DurableCompensationPublisher(sessions).publish(now=NOW)
        assert result >= 2
        async with sessions() as reader:
            job = (
                await reader.execute(
                    select(background_jobs.c.job_type, background_jobs.c.payload).where(
                        background_jobs.c.id == request_id
                    )
                )
            ).one()
            assert job == ("memory.reconcile_erasure", {"request_id": str(request_id)})
            outbox_payload = (
                await reader.execute(
                    select(transactional_outbox.c.payload).where(
                        transactional_outbox.c.aggregate_id == str(request_id)
                    )
                )
            ).scalar_one()
            assert outbox_payload == {"job_id": str(request_id), "dispatch_generation": 2}
    finally:
        await transaction.rollback()
        await connection.close()
