"""Calendar jobs use real PostgreSQL, provider I/O fences and synthetic messages."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from telegram_userbot.adapters.llm.protocols import ProviderWireRequest, ProviderWireResponse
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_periods import MemoryPeriodRepository
from telegram_userbot.adapters.persistence.scope_erasure_repository import ScopeErasureRepository
from telegram_userbot.domain.messaging.events import BodyKind, MessageBody
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.processes.worker_executors import JobExecutionError
from tests.integration.test_m5_context_media import NOW
from tests.integration.test_m8_memory_runtime import Transport, context, executor, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")
RUN_AT = NOW + timedelta(minutes=20)
OLD_DAY = NOW - timedelta(days=7)


class PeriodTransport(Transport):
    def __init__(self, callback: Any = None, *, empty: bool = False) -> None:
        self.callback = callback
        self.empty = empty
        self.inputs: list[Any] = []

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        content = request.body.reveal_for_use()["messages"][1]["content"]
        if isinstance(content, list):
            content = content[0]["text"]
        data = json.loads(content)
        self.inputs.append(data)
        if self.callback is not None:
            await self.callback()
        return ProviderWireResponse(
            200,
            SensitiveValue(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "schema_version": 2,
                                        "summary_text": None
                                        if self.empty
                                        else "A synthetic calendar summary.",
                                        "no_change_reason": "No change" if self.empty else None,
                                    }
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
            ),
        )


class WorkerSession(Session):
    pass


@event.listens_for(WorkerSession, "after_begin")
def worker_role(session: Any, transaction: Any, connection: Any) -> None:
    connection.exec_driver_sql("SET LOCAL ROLE telegram_userbot_worker_runtime")


async def setup(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, UUID]:
    async with sessions() as session, session.begin():
        account, conversation, revision, base_job = await seed(session)
        await session.execute(
            update(s.memory_jobs).where(s.memory_jobs.c.id == base_job).values(state="cancelled")
        )
        await session.execute(
            update(s.messages)
            .where(s.messages.c.conversation_id == conversation)
            .values(telegram_created_at=OLD_DAY)
        )
        await session.execute(
            update(s.message_events)
            .where(s.message_events.c.conversation_id == conversation)
            .values(observed_at=NOW)
        )
        return account, conversation, revision


async def scan(sessions: async_sessionmaker[AsyncSession]) -> UUID | None:
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        count, _ = await MemoryPeriodRepository(session).scan(now=RUN_AT, deployment_timezone="UTC")
        if not count:
            return None
        return await session.scalar(
            select(s.memory_jobs.c.id).where(s.memory_jobs.c.state == "pending")
        )


async def run(
    sessions: async_sessionmaker[AsyncSession], job: UUID, transport: PeriodTransport | None = None
) -> PeriodTransport:
    transport = transport or PeriodTransport()
    worker_sessions = async_sessionmaker(sessions.kw["bind"], sync_session_class=WorkerSession)
    work = await context(worker_sessions, job, now=RUN_AT)
    await executor(transport, now=RUN_AT)(work)
    # DurableJobConsumer normally completes the generic parent after the atomic result.
    async with sessions() as session, session.begin():
        await session.execute(
            update(s.background_jobs)
            .where(s.background_jobs.c.id == work.job.id)
            .values(
                state="succeeded",
                completed_at=RUN_AT,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
    return transport


async def edit(session: AsyncSession, conversation: UUID) -> UUID:
    old = (
        (
            await session.execute(
                select(s.message_revisions)
                .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
                .where(
                    s.messages.c.conversation_id == conversation,
                    s.messages.c.current_revision_no == s.message_revisions.c.revision_no,
                )
            )
        )
        .mappings()
        .one()
    )
    event = await session.scalar(
        insert(s.message_events)
        .values(
            event_uuid=uuid7(),
            account_id=old["account_id"],
            conversation_id=conversation,
            event_kind="incoming.edit",
            fingerprint_version=1,
            update_fingerprint=hashlib.sha256(uuid7().bytes).digest(),
            ordering_key="synthetic-edit",
            metadata_schema_version=1,
            observed_at=NOW,
            projected_at=NOW,
        )
        .returning(s.message_events.c.id)
    )
    new_id = uuid7()
    body = MessageBody(BodyKind.TEXT, "A corrected synthetic preference.")
    await session.execute(
        insert(s.message_revisions).values(
            id=new_id,
            account_id=old["account_id"],
            message_id=old["message_id"],
            revision_no=old["revision_no"] + 1,
            body_kind="text",
            text_content=body.text,
            entities_schema_version=1,
            entities=[],
            content_sha256=body.content_sha256,
            source_event_id=event,
        )
    )
    await session.execute(
        update(s.messages)
        .where(s.messages.c.id == old["message_id"])
        .values(current_revision_no=old["revision_no"] + 1)
    )
    return new_id


@pytest.mark.integration
async def test_daily_weekly_and_late_edit_rebuild(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    _, conversation, revision = await setup(sessions)
    daily = await scan(sessions)
    assert daily is not None
    assert await scan(sessions) is None
    daily_input = await run(sessions, daily)
    assert [item["source_id"] for item in daily_input.inputs[0]["sources"]] == [str(revision)]
    weekly = await scan(sessions)
    assert weekly is not None
    weekly_input = await run(sessions, weekly)
    assert {item["source_type"] for item in weekly_input.inputs[0]["sources"]} == {
        "summary_version"
    }
    assert await scan(sessions) is None
    async with sessions() as session, session.begin():
        revised = await edit(session, conversation)
    replacement = await scan(sessions)
    assert replacement is not None
    assert replacement != daily
    async with sessions() as session:
        assert (await session.execute(select(s.summaries.c.status))).scalars().all() == [
            "invalidated",
            "invalidated",
        ]
        assert await session.scalar(select(func.count()).select_from(s.summary_watermarks)) == 0
        assert set(
            (
                await session.execute(
                    select(s.embedding_records.c.state).where(
                        s.embedding_records.c.summary_version_id.is_not(None)
                    )
                )
            ).scalars()
        ) == {"invalidated"}
    replacement_input = await run(sessions, replacement)
    assert replacement_input.inputs[0]["sources"][0]["source_id"] == str(revised)
    new_weekly = await scan(sessions)
    assert new_weekly is not None
    assert new_weekly != weekly
    await run(sessions, new_weekly)
    async with sessions() as session:
        assert (
            await session.execute(select(s.summaries.c.current_version_no))
        ).scalars().all() == [2, 2]
        assert await session.scalar(select(func.count()).select_from(s.memory_proposals)) == 0


@pytest.mark.integration
@pytest.mark.parametrize("gate", ["quiet", "projection", "origin", "erasure", "busy"])
async def test_calendar_admission_gates(
    isolated_scope_erasure_engine: AsyncEngine, gate: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, _, _ = await setup(sessions)
    async with sessions() as session, session.begin():
        if gate == "quiet":
            await session.execute(update(s.message_events).values(observed_at=RUN_AT))
        elif gate == "projection":
            await session.execute(update(s.message_events).values(projected_at=None))
        elif gate == "origin":
            await session.execute(update(s.messages).values(source_status="pending"))
        elif gate == "busy":
            await session.execute(update(s.memory_jobs).values(state="pending"))
        else:
            await session.execute(
                insert(s.data_erasure_requests).values(
                    id=uuid7(),
                    account_id=account,
                    scope_type="account",
                    requested_by="synthetic",
                    policy_version=1,
                    state="requested",
                    request_idempotency_key=hashlib.sha256(b"period-erasure").digest(),
                )
            )
    assert await scan(sessions) is None


@pytest.mark.integration
async def test_period_snapshot_survives_timezone_edit(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    await setup(sessions)
    async with sessions() as session, session.begin():
        await session.execute(update(s.contacts).values(timezone="Asia/Tokyo"))
    job = await scan(sessions)
    assert job is not None
    async with sessions() as session, session.begin():
        await session.execute(update(s.contacts).values(timezone="America/New_York"))
    transport = await run(sessions, job)
    assert transport.inputs[0]["summary_period"]["timezone"] == "Asia/Tokyo"
    async with sessions() as session:
        row = (await session.execute(select(s.summary_versions))).mappings().one()
        assert row["timezone_snapshot"] == "Asia/Tokyo"
        assert row["period_start_at"].hour == 15


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["edit", "empty"])
async def test_period_output_fence_and_terminal_budget(
    isolated_scope_erasure_engine: AsyncEngine, mutation: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    _, conversation, _ = await setup(sessions)
    job = await scan(sessions)
    assert job is not None
    committed = False

    async def callback() -> None:
        nonlocal committed
        async with sessions() as session, session.begin():
            await edit(session, conversation)
        committed = True

    work = await context(sessions, job, now=RUN_AT)
    with pytest.raises(JobExecutionError) as error:
        await executor(
            PeriodTransport(callback if mutation == "edit" else None, empty=mutation == "empty"),
            now=RUN_AT,
        )(work)
    assert committed == (mutation == "edit")
    assert error.value.code == (
        "MEMORY_PERIOD_CHANGED" if mutation == "edit" else "MEMORY_PERIOD_SUMMARY_REQUIRED"
    )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(s.summary_versions)) == 0
    followup = await scan(sessions)
    assert (followup is not None and followup != job) if mutation == "edit" else followup is None


@pytest.mark.integration
async def test_period_migration_upgrade_rollback(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    def migrate(connection: Any, target: str, *, down: bool = False) -> None:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, target)

    async with isolated_scope_erasure_engine.begin() as connection:
        await connection.run_sync(lambda c: migrate(c, "0034_scope_erasure_completion", down=True))
    async with isolated_scope_erasure_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(lambda c: migrate(c, "head"))
        await transaction.rollback()
    async with isolated_scope_erasure_engine.begin() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0034_scope_erasure_completion"
        )
        await connection.run_sync(lambda c: migrate(c, "head"))
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    await setup(sessions)
    assert await scan(sessions) is not None
    with pytest.raises(RuntimeError, match="calendar summary jobs prevent downgrade"):
        async with isolated_scope_erasure_engine.begin() as connection:
            await connection.run_sync(
                lambda c: migrate(c, "0034_scope_erasure_completion", down=True)
            )


@pytest.mark.integration
async def test_period_snapshot_is_removed_by_scope_erasure(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, _, _ = await setup(sessions)
    job = await scan(sessions)
    assert job is not None
    await run(sessions, job)
    request = uuid7()
    async with sessions() as session, session.begin():
        await session.execute(
            insert(s.data_erasure_requests).values(
                id=request,
                account_id=account,
                scope_type="account",
                state="requested",
                requested_by="synthetic",
                policy_version=1,
                request_idempotency_key=hashlib.sha256(request.bytes).digest(),
            )
        )
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        await ScopeErasureRepository(session).prepare(
            account_id=account,
            request_id=request,
            contact_id=None,
            now=RUN_AT,
            scope_secret=b"p" * 32,
            policy_version=1,
        )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(s.background_jobs.c.payload).where(
                    s.background_jobs.c.job_type == "memory.generate"
                )
            )
            == {}
        )
        version = (await session.execute(select(s.summary_versions))).mappings().one()
        assert version["content_text"] is None
        assert version["period_start_at"] is None
        assert version["timezone_snapshot"] is None
    assert await scan(sessions) is None


@pytest.mark.integration
async def test_late_message_in_empty_day_rejects_inflight_week(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, conversation, _ = await setup(sessions)
    daily = await scan(sessions)
    assert daily is not None
    await run(sessions, daily)
    weekly = await scan(sessions)
    assert weekly is not None
    changed = False

    async def callback() -> None:
        nonlocal changed
        async with sessions() as session, session.begin():
            original = dict((await session.execute(select(s.messages))).mappings().one())
            event_id = await session.scalar(
                insert(s.message_events)
                .values(
                    account_id=account,
                    conversation_id=conversation,
                    event_uuid=uuid7(),
                    event_kind="incoming.create",
                    fingerprint_version=1,
                    update_fingerprint=hashlib.sha256(uuid7().bytes).digest(),
                    ordering_key="late-message",
                    metadata_schema_version=1,
                    observed_at=NOW,
                    projected_at=NOW,
                )
                .returning(s.message_events.c.id)
            )
            new_message, new_revision = uuid7(), uuid7()
            original.update(
                id=new_message,
                telegram_message_id=11,
                telegram_created_at=OLD_DAY - timedelta(days=1),
            )
            await session.execute(insert(s.messages).values(**original))
            body = MessageBody(BodyKind.TEXT, "A late synthetic message.")
            await session.execute(
                insert(s.message_revisions).values(
                    id=new_revision,
                    account_id=account,
                    message_id=new_message,
                    revision_no=1,
                    body_kind="text",
                    text_content=body.text,
                    entities_schema_version=1,
                    entities=[],
                    content_sha256=body.content_sha256,
                    source_event_id=event_id,
                )
            )
        changed = True

    work = await context(sessions, weekly, now=RUN_AT)
    with pytest.raises(JobExecutionError) as failure:
        await executor(PeriodTransport(callback), now=RUN_AT)(work)
    assert changed
    assert failure.value.code == "MEMORY_PERIOD_CHANGED"
    next_day = await scan(sessions)
    assert next_day is not None
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(s.summaries)
                .where(s.summaries.c.summary_kind == "weekly")
            )
            == 0
        )
