"""PostgreSQL M7 schema and role contracts on the disposable service."""

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.schema import (
    M7_TABLES,
    conversations,
    proactive_budget_buckets,
    proactive_jobs,
)
from telegram_userbot.domain.proactive.models import BudgetLimits, BudgetReservation
from tests.integration.test_m1_persistence import NOW, seed_conversation

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_m7_schema_inventory_constraints_and_head(db_session: AsyncSession) -> None:
    rows = await db_session.scalars(
        text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'proactive_%' ORDER BY tablename"
        )
    )
    assert set(rows) == set(M7_TABLES)
    assert await db_session.scalar(text("SELECT version_num FROM alembic_version")) == (
        "0013_m5_m7_consistency"
    )
    indexes = set(
        await db_session.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename IN ("
                "'proactive_candidates','proactive_jobs','proactive_budget_buckets')"
            )
        )
    )
    assert {
        "ix_proactive_candidates_due",
        "ix_proactive_jobs_due",
        "uq_proactive_budget_bucket_identity",
    } <= indexes
    constraints = {
        cast(str, row["conname"])
        for row in (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'proactive_occurrences'::regclass"
                )
            )
        ).mappings()
    }
    assert {
        "ck_proactive_occurrences_reason_values",
        "ck_proactive_occurrences_state_values",
        "ck_proactive_occurrences_window_values",
    } <= constraints


@pytest.mark.integration
async def test_m7_roles_keep_control_out_of_candidate_and_decision_truth(
    db_session: AsyncSession,
) -> None:
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_control_runtime', "
                "'proactive_candidates', 'UPDATE')"
            )
        )
        is False
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_control_runtime', "
                "'proactive_policies', 'INSERT')"
            )
        )
        is True
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_worker_runtime', "
                "'proactive_budget_reservations', 'UPDATE')"
            )
        )
        is True
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_backup', "
                "'proactive_decisions', 'UPDATE')"
            )
        )
        is False
    )


@pytest.mark.integration
async def test_m7_concurrent_job_replay_and_same_owner_reclaim_are_fenced(
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as setup, setup.begin():
        account_id, _conversation_id, _account_peer_id = await seed_conversation(setup)
    key = b"f" * 32

    async def enqueue() -> UUID:
        async with factory() as worker, worker.begin():
            return await ProactiveRepository(worker).enqueue_job(
                account_id=account_id,
                idempotency_key=key,
                available_at=NOW,
                now=NOW,
            )

    first_id, second_id = await asyncio.gather(enqueue(), enqueue())
    assert first_id == second_id

    owner = uuid7()
    async with factory() as worker, worker.begin():
        repository = ProactiveRepository(worker)
        first = await repository.claim_next(
            now=NOW,
            owner=owner,
            lease=timedelta(seconds=1),
        )
    assert first is not None
    async with factory() as worker, worker.begin():
        assert not await ProactiveRepository(worker).complete_job(
            idempotency_key=key,
            owner=owner,
            fencing_token=first.fencing_token,
            now=NOW + timedelta(seconds=1),
        )
    async with factory() as worker, worker.begin():
        repository = ProactiveRepository(worker)
        assert (
            await repository.claim_next(
                now=NOW + timedelta(seconds=1),
                owner=owner,
            )
            is None
        )
    async with factory() as worker, worker.begin():
        repository = ProactiveRepository(worker)
        replacement = await repository.claim_next(
            now=NOW + timedelta(seconds=6),
            owner=owner,
        )
    assert replacement is not None
    assert replacement.id == first.id
    assert replacement.fencing_token > first.fencing_token

    async with factory() as worker, worker.begin():
        repository = ProactiveRepository(worker)
        assert not await repository.complete_job(
            idempotency_key=key,
            owner=owner,
            fencing_token=first.fencing_token,
            now=NOW + timedelta(seconds=6),
        )
        assert await repository.complete_job(
            idempotency_key=key,
            owner=owner,
            fencing_token=replacement.fencing_token,
            now=NOW + timedelta(seconds=6),
        )

    terminal_key = b"d" * 32
    async with factory() as worker, worker.begin():
        await ProactiveRepository(worker).enqueue_job(
            account_id=account_id,
            idempotency_key=terminal_key,
            available_at=NOW,
            now=NOW,
        )
    async with factory() as worker, worker.begin():
        terminal = await ProactiveRepository(worker).claim_next(
            now=NOW + timedelta(seconds=10),
            owner=owner,
            lease=timedelta(seconds=1),
            max_attempts=1,
        )
    assert terminal is not None
    async with factory() as worker, worker.begin():
        assert (
            await ProactiveRepository(worker).claim_next(
                now=NOW + timedelta(seconds=11),
                owner=owner,
                max_attempts=1,
            )
            is None
        )
    async with factory() as verification:
        assert (
            await verification.scalar(
                select(proactive_jobs.c.state).where(
                    proactive_jobs.c.idempotency_key == terminal_key
                )
            )
            == "dead_letter"
        )


@pytest.mark.integration
async def test_m7_concurrent_budget_replay_counts_one_hold(postgres_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as setup, setup.begin():
        account_id, conversation_id, _account_peer_id = await seed_conversation(setup)
        contact_id = await setup.scalar(
            select(conversations.c.contact_id).where(conversations.c.id == conversation_id)
        )
    assert contact_id is not None
    key = b"b" * 32

    async def reserve() -> BudgetReservation | None:
        async with factory() as worker, worker.begin():
            return await ProactiveRepository(worker).reserve_budget(
                account_id=account_id,
                contact_id=contact_id,
                local_date=NOW.date(),
                limits=BudgetLimits(10, 10),
                expires_at=NOW + timedelta(minutes=5),
                reservation_key=key,
            )

    first, second = await asyncio.gather(reserve(), reserve())
    assert first is not None
    assert second is not None
    assert first.id == second.id
    async with factory() as verification:
        counts = tuple(
            await verification.scalars(
                select(proactive_budget_buckets.c.held_count).where(
                    proactive_budget_buckets.c.account_id == account_id
                )
            )
        )
        assert counts == (1, 1)
