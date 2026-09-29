"""Bounded backlog coverage, calendar reduction and explicit timezone rebuild."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from itertools import pairwise
from uuid import UUID, uuid7

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_periods import MemoryPeriodRepository
from telegram_userbot.domain.memory.models import SummaryKind
from telegram_userbot.domain.memory.periods import SummaryPeriod
from tests.integration.test_m5_context_media import NOW
from tests.integration.test_m8_memory_periods import (
    OLD_DAY,
    RUN_AT,
    WorkerSession,
    run,
    scan,
    setup,
)
from tests.integration.test_m8_memory_runtime import Transport, context, executor, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def append_messages(session: AsyncSession, conversation: UUID, count: int) -> int:
    old = (
        (
            await session.execute(
                select(s.messages).where(s.messages.c.conversation_id == conversation)
            )
        )
        .mappings()
        .one()
    )
    revision = (
        (
            await session.execute(
                select(s.message_revisions).where(s.message_revisions.c.message_id == old["id"])
            )
        )
        .mappings()
        .one()
    )
    last = revision["source_event_id"]
    for index in range(count):
        message_id = uuid7()
        last = await session.scalar(
            insert(s.message_events)
            .values(
                event_uuid=uuid7(),
                account_id=old["account_id"],
                conversation_id=conversation,
                event_kind="incoming.create",
                telegram_message_id=100 + index,
                fingerprint_version=1,
                update_fingerprint=hashlib.sha256(message_id.bytes).digest(),
                ordering_key=f"synthetic:{index}",
                metadata_schema_version=1,
                observed_at=NOW,
                projected_at=NOW,
            )
            .returning(s.message_events.c.id)
        )
        await session.execute(
            insert(s.messages).values(
                **{**dict(old), "id": message_id, "telegram_message_id": 100 + index}
            )
        )
        await session.execute(
            insert(s.message_revisions).values(
                **{
                    **dict(revision),
                    "id": uuid7(),
                    "message_id": message_id,
                    "source_event_id": last,
                }
            )
        )
    assert isinstance(last, int)
    return last


@pytest.mark.integration
async def test_large_day_reduces_parts_then_publishes_complete_daily(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, conversation, _ = await setup(sessions)
    async with sessions() as session, session.begin():
        await append_messages(session, conversation, 80)
    count = 0
    for _ in range(20):
        job = await scan(sessions)
        if job is None:
            break
        await run(sessions, job)
        count += 1
    assert 3 < count < 20
    async with sessions() as session:
        repository = MemoryPeriodRepository(session)
        period = SummaryPeriod.at(SummaryKind.DAILY, OLD_DAY, "UTC")
        current = await repository.current(conversation, period)
        inputs, _, _ = await repository.daily_sources(account, conversation, period)
        assert await repository.matches(current, inputs)
        assert all(item.source_type == "summary_version" for item in inputs)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(s.memory_input_manifest_items)
                .where(s.memory_input_manifest_items.c.source_type == "message_revision")
            )
            == 81
        )
        assert all(item.partition is None for item in await repository.snapshots(conversation))


@pytest.mark.integration
async def test_episode_backlog_resumes_without_skipping_event_ranges(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    async with sessions() as session, session.begin():
        _, conversation, _, first = await seed(session)
        last = await append_messages(session, conversation, 70)
        await session.execute(
            update(s.memory_jobs).where(s.memory_jobs.c.id == first).values(range_end_event_id=last)
        )
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    transport = Transport(mode="empty")
    for attempt in range(10):
        async with sessions() as session:
            job = await session.scalar(
                select(s.memory_jobs.c.id)
                .where(s.memory_jobs.c.state == "pending")
                .order_by(s.memory_jobs.c.created_at, s.memory_jobs.c.id)
                .limit(1)
            )
        if job is None:
            break
        clock = RUN_AT + timedelta(minutes=attempt)
        work = await context(worker, job, now=clock)
        await executor(transport, now=clock)(work)
    async with sessions() as session:
        ranges = (
            await session.execute(
                select(
                    s.memory_jobs.c.range_start_event_id,
                    s.memory_jobs.c.range_end_event_id,
                    s.memory_jobs.c.state,
                )
                .where(s.memory_jobs.c.job_kind == "episode")
                .order_by(s.memory_jobs.c.generation)
            )
        ).all()
        assert len(ranges) >= 3
        assert all(row.state == "succeeded" for row in ranges)
        assert all(
            right.range_start_event_id == left.range_end_event_id + 1
            for left, right in pairwise(ranges)
        )
        assert ranges[-1].range_end_event_id == last
        assert (
            await session.scalar(
                select(s.memory_watermarks.c.last_contiguous_decided_event_id).where(
                    s.memory_watermarks.c.conversation_id == conversation,
                    s.memory_watermarks.c.watermark_kind == "episode",
                )
            )
            == last
        )


@pytest.mark.integration
async def test_timezone_rebuild_cancels_old_jobs_and_preserves_provenance(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, conversation, _ = await setup(sessions)
    job = await scan(sessions)
    assert job is not None
    await run(sessions, job)
    async with sessions() as session, session.begin():
        old_version = await session.scalar(select(s.summary_versions.c.id))
        await session.execute(
            update(s.contacts)
            .where(s.contacts.c.account_id == account)
            .values(timezone="Asia/Tokyo")
        )
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    async with worker() as session, session.begin():
        assert (
            await MemoryPeriodRepository(session).repartition_history(
                account_id=account,
                conversation_id=conversation,
                timezone="Asia/Tokyo",
                now=RUN_AT + timedelta(seconds=1),
            )
            == 1
        )
    job = await scan(sessions)
    assert job is not None
    await run(sessions, job)
    async with sessions() as session:
        assert (
            await session.scalar(
                select(s.summary_versions.c.invalidation_state).where(
                    s.summary_versions.c.id == old_version
                )
            )
            == "invalidated"
        )
        current = await MemoryPeriodRepository(session).current(
            conversation, SummaryPeriod.at(SummaryKind.DAILY, OLD_DAY, "Asia/Tokyo")
        )
        assert current["status"] == "active"
    # Returning to an archived timezone reuses the parent with a new version.
    async with sessions() as session, session.begin():
        await session.execute(
            update(s.contacts).where(s.contacts.c.account_id == account).values(timezone="UTC")
        )
    async with worker() as session, session.begin():
        assert (
            await MemoryPeriodRepository(session).repartition_history(
                account_id=account,
                conversation_id=conversation,
                timezone="UTC",
                now=RUN_AT + timedelta(seconds=2),
            )
            == 1
        )
    job = await scan(sessions)
    assert job is not None
    await run(sessions, job)
    async with sessions() as session:
        restored = await MemoryPeriodRepository(session).current(
            conversation, SummaryPeriod.at(SummaryKind.DAILY, OLD_DAY, "UTC")
        )
        assert restored["status"] == "active"
        assert restored["version_no"] == 2
        assert restored["id"] != old_version
