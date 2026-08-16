"""PostgreSQL M7 schema and role contracts on the disposable service."""

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.schema import (
    M7_TABLES,
    conversations,
    proactive_budget_buckets,
    proactive_candidates,
    proactive_decisions,
    proactive_jobs,
    proactive_policies,
)
from telegram_userbot.domain.proactive.models import BudgetLimits, BudgetReservation
from telegram_userbot.domain.proactive.pipeline import ProactiveTarget
from tests.integration.test_m1_persistence import NOW, seed_conversation

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_budget_binding(
    session: AsyncSession,
    account_id: UUID,
    contact_id: UUID,
    conversation_id: UUID,
    *,
    policy_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    policy_id = uuid7() if policy_id is None else policy_id
    candidate_id = uuid7()
    decision_id = uuid7()
    await session.execute(
        insert(proactive_policies).values(
            id=policy_id,
            account_id=account_id,
            version_no=1,
            enabled=True,
            timezone_name="UTC",
            account_daily_limit=10,
            contact_bypass_daily_limit=1,
        )
    )
    await session.execute(
        insert(proactive_candidates).values(
            id=candidate_id,
            account_id=account_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            candidate_key=candidate_id.bytes + candidate_id.bytes,
            generation=1,
            membership_hash=b"m" * 32,
            state="send_selected",
            window_start_at=NOW,
            window_end_at=NOW + timedelta(hours=1),
            due_at=NOW,
            policy_version_id=policy_id,
            timezone_name="UTC",
            mode_version=1,
            content_revision=0,
            activity_revision=0,
        )
    )
    await session.execute(
        insert(proactive_decisions).values(
            id=decision_id,
            account_id=account_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            candidate_id=candidate_id,
            generation=1,
            policy_version_id=policy_id,
            timezone_name="UTC",
            action="send_now",
            decision_code="timely_support",
            topic="synthetic",
            priority=0,
            output_hash=b"d" * 32,
        )
    )
    return candidate_id, decision_id, policy_id


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
        "0016_m5_m7_delivery_integrity"
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
async def test_m7_terminal_candidate_job_is_not_reclaimed(db_session: AsyncSession) -> None:
    account_id, conversation_id, _account_peer_id = await seed_conversation(db_session)
    contact_id = await db_session.scalar(
        select(conversations.c.contact_id).where(conversations.c.id == conversation_id)
    )
    assert contact_id is not None
    policy_id, candidate_id, job_id = uuid7(), uuid7(), uuid7()
    await db_session.execute(
        insert(proactive_policies).values(
            id=policy_id,
            account_id=account_id,
            version_no=1,
            enabled=True,
            timezone_name="UTC",
            account_daily_limit=10,
            contact_bypass_daily_limit=1,
        )
    )
    await db_session.execute(
        insert(proactive_candidates).values(
            id=candidate_id,
            account_id=account_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            candidate_key=candidate_id.bytes + candidate_id.bytes,
            generation=1,
            membership_hash=b"m" * 32,
            state="evaluated_none",
            window_start_at=NOW,
            window_end_at=NOW + timedelta(hours=1),
            due_at=NOW,
            policy_version_id=policy_id,
            timezone_name="UTC",
            mode_version=1,
            content_revision=0,
            activity_revision=0,
        )
    )
    await db_session.execute(
        insert(proactive_jobs).values(
            id=job_id,
            account_id=account_id,
            candidate_id=candidate_id,
            job_kind="candidate_due",
            idempotency_key=b"t" * 32,
            available_at=NOW,
            state="pending",
        )
    )
    repository = ProactiveRepository(db_session)
    assert await repository.claim_next(now=NOW, owner=uuid7()) is None
    terminal_job = (
        (await db_session.execute(select(proactive_jobs).where(proactive_jobs.c.id == job_id)))
        .mappings()
        .one()
    )
    assert terminal_job["state"] == "succeeded"
    assert terminal_job["completed_at"] == NOW
    with pytest.raises(ValueError, match="already terminal"):
        await repository.enqueue_job(
            account_id=account_id,
            idempotency_key=b"u" * 32,
            available_at=NOW,
            candidate_id=candidate_id,
            now=NOW,
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
        candidate_id, decision_id, policy_id = await seed_budget_binding(
            setup, account_id, contact_id, conversation_id
        )
    key = b"b" * 32

    async def reserve() -> BudgetReservation | None:
        async with factory() as worker, worker.begin():
            return await ProactiveRepository(worker).reserve_budget(
                account_id=account_id,
                contact_id=contact_id,
                account_local_date=NOW.date(),
                contact_local_date=NOW.date(),
                account_timezone_name="UTC",
                contact_timezone_name="UTC",
                limits=BudgetLimits(10, 10),
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
                reservation_key=key,
                candidate_id=candidate_id,
                decision_id=decision_id,
                policy_version_id=policy_id,
                authorization_generation=1,
                target=ProactiveTarget.AUTO_SEND,
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


@pytest.mark.integration
async def test_m7_budget_limits_only_shrink_without_breaking_count_constraint(
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as setup, setup.begin():
        account_id, conversation_id, _account_peer_id = await seed_conversation(setup)
        contact_id = await setup.scalar(
            select(conversations.c.contact_id).where(conversations.c.id == conversation_id)
        )
        assert contact_id is not None
        candidate_id, decision_id, policy_id = await seed_budget_binding(
            setup, account_id, contact_id, conversation_id
        )

    first_key = b"s" * 32
    async with factory() as worker, worker.begin():
        first = await ProactiveRepository(worker).reserve_budget(
            account_id=account_id,
            contact_id=contact_id,
            account_local_date=NOW.date(),
            contact_local_date=NOW.date(),
            account_timezone_name="UTC",
            contact_timezone_name="UTC",
            limits=BudgetLimits(5, 5),
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
            reservation_key=first_key,
            candidate_id=candidate_id,
            decision_id=decision_id,
            policy_version_id=policy_id,
            authorization_generation=1,
            target=ProactiveTarget.AUTO_SEND,
        )
    assert first is not None

    async with factory() as worker, worker.begin():
        assert (
            await ProactiveRepository(worker).reserve_budget(
                account_id=account_id,
                contact_id=contact_id,
                account_local_date=NOW.date(),
                contact_local_date=NOW.date(),
                account_timezone_name="UTC",
                contact_timezone_name="UTC",
                limits=BudgetLimits(1, 1),
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
                reservation_key=b"t" * 32,
                candidate_id=candidate_id,
                decision_id=decision_id,
                policy_version_id=policy_id,
                authorization_generation=1,
                target=ProactiveTarget.AUTO_SEND,
            )
            is None
        )

    async with factory() as verification:
        rows = list(
            (
                await verification.execute(
                    select(
                        proactive_budget_buckets.c.scope,
                        proactive_budget_buckets.c.limit_value,
                        proactive_budget_buckets.c.held_count,
                    ).where(proactive_budget_buckets.c.account_id == account_id)
                )
            ).mappings()
        )
    assert {(row["scope"], row["limit_value"], row["held_count"]) for row in rows} == {
        ("account_daily", 1, 1),
        ("contact_daily", 1, 1),
    }

    async with factory() as worker, worker.begin():
        assert (
            await ProactiveRepository(worker).reserve_budget(
                account_id=account_id,
                contact_id=contact_id,
                account_local_date=NOW.date(),
                contact_local_date=NOW.date(),
                account_timezone_name="UTC",
                contact_timezone_name="UTC",
                limits=BudgetLimits(0, 0),
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
                reservation_key=b"u" * 32,
                candidate_id=candidate_id,
                decision_id=decision_id,
                policy_version_id=policy_id,
                authorization_generation=1,
                target=ProactiveTarget.AUTO_SEND,
            )
            is None
        )

    async with factory() as verification:
        limits = tuple(
            await verification.scalars(
                select(proactive_budget_buckets.c.limit_value).where(
                    proactive_budget_buckets.c.account_id == account_id
                )
            )
        )
    assert limits == (1, 1)
