"""Real PostgreSQL scope cancellation, export cleanup, and conservative billing."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid7

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.domain.proactive.models import BudgetLimits
from telegram_userbot.domain.proactive.pipeline import ProactiveTarget
from tests.integration.test_m5_context_media import NOW, seed_scope
from tests.integration.test_m7_proactive_pipeline import seed_budget_binding
from tests.integration.test_m8_scope_erasure import _advance, _request
from tests.operations.test_m8_data_export_leases import _load_export_script

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _export(session: AsyncSession, account: UUID, contact: UUID | None, state: str) -> UUID:
    request = uuid7()
    now = datetime.now(UTC)
    await session.execute(
        insert(s.data_export_requests).values(
            id=request,
            account_id=account,
            contact_id=contact,
            state=state,
            requested_by="actor:hmac-sha256:" + "a" * 64,
            created_at=now,
            expires_at=now + timedelta(days=1),
            owner_instance_id=uuid7() if state == "claimed" else None,
            lease_expires_at=now + timedelta(minutes=30) if state == "claimed" else None,
            completed_at=now if state == "completed" else None,
            artifact_sha256=hashlib.sha256(b"ciphertext").digest()
            if state == "completed"
            else None,
            attempt_count=0 if state == "requested" else 1,
        )
    )
    return request


@pytest.mark.integration
async def test_export_admission_waits_for_uncommitted_erasure_then_rejects(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    engine = isolated_scope_erasure_engine
    async with AsyncSession(engine) as seed, seed.begin():
        account, conversation, _, _ = await seed_scope(seed)
    started = asyncio.Event()

    async def stale_exporter() -> None:
        async with AsyncSession(engine) as exporter, exporter.begin():
            await exporter.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
            started.set()
            await _export(exporter, account, None, "requested")

    task: asyncio.Task[None] | None = None
    try:
        async with AsyncSession(engine) as eraser, eraser.begin():
            await _request(eraser, account, conversation)
            task = asyncio.create_task(stale_exporter())
            await started.wait()
            await asyncio.sleep(0.05)
            assert not task.done()
        with pytest.raises(DBAPIError, match="ERASURE_EXPORT_BLOCKED"):
            async with asyncio.timeout(10):
                await task
        async with AsyncSession(engine) as reader:
            assert (
                await reader.scalar(
                    select(s.data_export_requests.c.id).where(
                        s.data_export_requests.c.account_id == account
                    )
                )
                is None
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.integration
async def test_upgrade_revokes_exports_for_preexisting_erasure_intent(
    isolated_scope_erasure_session: AsyncSession,
) -> None:
    session = isolated_scope_erasure_session
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    connection = await session.connection()

    def migrate(sync_connection: object, *, down: bool) -> None:
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.attributes["connection"] = sync_connection
        if down:
            command.downgrade(config, "0030_scope_derived_erasure")
        else:
            command.upgrade(config, "head")

    await connection.run_sync(migrate, down=True)
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    account, conversation, _, _ = await seed_scope(session)
    request = await _export(session, account, None, "claimed")
    await _request(session, account, conversation)
    assert (
        await session.scalar(
            select(s.data_export_requests.c.state).where(s.data_export_requests.c.id == request)
        )
        == "claimed"
    )
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await connection.run_sync(migrate, down=False)
    result = (
        await session.execute(
            select(
                s.data_export_requests.c.state, s.data_export_requests.c.erasure_requested_at
            ).where(s.data_export_requests.c.id == request)
        )
    ).one()
    assert result.state == "failed"
    assert result.erasure_requested_at is not None


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_export_revocation_is_immediate_scoped_and_one_way(
    isolated_scope_erasure_session: AsyncSession, scope: str
) -> None:
    session = isolated_scope_erasure_session
    account, conversation, _, _ = await seed_scope(session)
    _, sibling, _, _ = await seed_scope(session, existing_account_id=account)
    other_account, _, _, _ = await seed_scope(session)
    contact = await session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
    )
    other_contact = await session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == sibling)
    )
    queued = await _export(session, account, contact, "requested")
    claimed = await _export(session, account, None, "claimed")
    completed = await _export(session, account, contact, "completed")
    unrelated = await _export(session, account, other_contact, "requested")
    other = await _export(session, other_account, None, "requested")
    request = await _request(session, account, conversation, scope=scope)
    rows = {row.id: row for row in (await session.execute(select(s.data_export_requests))).all()}
    assert rows[queued].state == rows[claimed].state == "failed"
    assert rows[queued].last_error_code == rows[claimed].last_error_code == "ERASURE_SCOPE"
    assert rows[claimed].owner_instance_id is None
    assert rows[completed].state == "completed"
    assert rows[completed].artifact_deleted_at is None
    assert rows[other].erasure_requested_at is None
    assert (rows[unrelated].erasure_requested_at is not None) == (scope == "account")
    for request_id in (queued, claimed, completed):
        assert rows[request_id].erasure_requested_at is not None
        assert rows[request_id].erasure_cleaned_at is None
    # Enforce this at the database boundary even before the erasure worker runs.
    with pytest.raises(DBAPIError, match="ERASURE_EXPORT_BLOCKED"):  # noqa: PT012
        async with session.begin_nested():
            await session.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
            await _export(session, account, None, "requested")
    with pytest.raises(DBAPIError, match="EXPORT_ERASURE_IMMUTABLE"):
        async with session.begin_nested():
            await session.execute(
                update(s.data_export_requests)
                .where(s.data_export_requests.c.id == claimed)
                .values(erasure_requested_at=None)
            )
    await _advance(session, account, request)
    assert (
        await session.scalar(
            select(s.data_erasure_requests.c.last_error_code).where(
                s.data_erasure_requests.c.id == request
            )
        )
        == "ERASURE_EXPORT_PENDING"
    )
    # The worker cannot assert physical cleanup, even on an already-revoked row.
    with pytest.raises(DBAPIError) as caught:  # noqa: PT012
        async with session.begin_nested():
            await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
            await session.execute(
                update(s.data_export_requests)
                .where(s.data_export_requests.c.id == claimed)
                .values(erasure_cleaned_at=datetime.now(UTC))
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"


@pytest.mark.integration
async def test_real_export_cleanup_waits_for_live_writer_and_acknowledges_disk_removal(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path
) -> None:
    engine = isolated_scope_erasure_engine
    async with AsyncSession(engine) as session, session.begin():
        account, conversation, _, _ = await seed_scope(session)
        completed = await _export(session, account, None, "completed")
        claimed = await _export(session, account, None, "claimed")
        request = await _request(session, account, conversation)
        await _advance(session, account, request)
    module = _load_export_script()
    final = tmp_path / f"{completed}.1.jsonl.age"
    orphan = tmp_path / f"{claimed}.1.jsonl.age"
    final.write_bytes(b"ciphertext")
    orphan.write_bytes(b"unregistered-ciphertext")
    lock = module._ArtifactLock(tmp_path, claimed)
    assert lock.acquire()
    dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)

    def cleanup() -> int:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as connection:
            connection.execute("SET ROLE telegram_userbot_export_runtime")
            return int(module._cleanup_erased(connection, tmp_path, datetime.now(UTC)))

    try:
        assert await asyncio.to_thread(cleanup) == 1
        assert not final.exists()
        assert orphan.exists()
        async with AsyncSession(engine) as session, session.begin():
            await _advance(session, account, request)
            assert (
                await session.scalar(
                    select(s.data_erasure_requests.c.last_error_code).where(
                        s.data_erasure_requests.c.id == request
                    )
                )
                == "ERASURE_EXPORT_PENDING"
            )
    finally:
        lock.close()
    assert await asyncio.to_thread(cleanup) == 1
    assert await asyncio.to_thread(cleanup) == 0
    assert not orphan.exists()
    async with AsyncSession(engine) as session, session.begin():
        await _advance(session, account, request)
        assert (
            await session.scalar(
                select(s.data_erasure_requests.c.last_error_code).where(
                    s.data_erasure_requests.c.id == request
                )
            )
            == "ERASURE_MEDIA_INVENTORY_PENDING"
        )
        assert (
            await session.scalar(
                select(s.data_erasure_requests.c.completed_at).where(
                    s.data_erasure_requests.c.id == request
                )
            )
            is None
        )
        # A stale writer cannot publish its old claim after physical cleanup.
        result = await session.execute(
            update(s.data_export_requests)
            .where(
                s.data_export_requests.c.id == claimed,
                s.data_export_requests.c.state == "claimed",
            )
            .values(
                state="completed",
                completed_at=datetime.now(UTC),
                owner_instance_id=None,
                lease_expires_at=None,
                artifact_sha256=b"x" * 32,
            )
            .returning(s.data_export_requests.c.id)
        )
        assert result.first() is None


@pytest.mark.integration
@pytest.mark.parametrize("target", [ProactiveTarget.AUTO_SEND, ProactiveTarget.COPILOT_DRAFT])
@pytest.mark.parametrize(
    ("state", "started", "expected"),
    [
        (None, False, "released"),
        ("planned", False, "released"),
        ("sending", True, "send_unknown"),
        ("partial", True, "send_unknown"),
        ("sent", True, "committed"),
        ("unknown", True, "send_unknown"),
        ("sending", False, "held"),
    ],
)
async def test_scope_budget_preserves_side_effect_cost_and_replays_without_double_counting(
    db_session: AsyncSession,
    state: str | None,
    started: bool,
    expected: str,
    target: ProactiveTarget,
) -> None:
    account, conversation, turn, _ = await seed_scope(db_session)
    contact = await db_session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
    )
    assert isinstance(contact, UUID)
    candidate, decision, policy = await seed_budget_binding(
        db_session, account, contact, conversation, now=NOW
    )
    repository = ProactiveRepository(db_session)
    key = hashlib.sha256(account.bytes).digest()
    reservation = await repository.reserve_budget(
        account_id=account,
        contact_id=contact,
        account_local_date=NOW.date(),
        contact_local_date=NOW.date(),
        account_timezone_name="UTC",
        contact_timezone_name="UTC",
        limits=BudgetLimits(10, 10),
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
        reservation_key=key,
        candidate_id=candidate,
        decision_id=decision,
        policy_version_id=policy,
        authorization_generation=1,
        target=target,
    )
    assert reservation is not None
    draft = None
    if target is ProactiveTarget.COPILOT_DRAFT and state is not None:
        draft = uuid7()
        await db_session.execute(
            insert(s.copilot_drafts).values(
                id=draft,
                account_id=account,
                contact_id=contact,
                conversation_id=conversation,
                turn_id=turn,
                proactive_decision_id=decision,
                draft_kind="proactive",
                state="requested",
                account_control_version_snapshot=1,
                mode_version_snapshot=1,
                content_revision_snapshot=0,
                requested_by="system:proactive",
                requested_at=NOW,
            )
        )
        await repository.bind_budget_target(
            account_id=account, reservation_key=key, target=target, target_id=draft, now=NOW
        )
    if state is not None:
        group = uuid7()
        await db_session.execute(
            insert(s.outbound_delivery_groups).values(
                id=group,
                account_id=account,
                conversation_id=conversation,
                proactive_decision_id=decision,
                source="proactive_ai" if draft is None else "copilot_approved",
                copilot_draft_id=draft,
                state="planned",
                intent_count=1,
                idempotency_key=b"g" * 32,
                mode_version=1,
                content_revision=0,
                account_control_version=1,
                max_delivery_chunks=1,
                send_authorized_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        if draft is None:
            await repository.bind_budget_target(
                account_id=account,
                reservation_key=key,
                target=target,
                target_id=group,
                now=NOW,
            )
        await db_session.execute(
            update(s.outbound_delivery_groups)
            .where(s.outbound_delivery_groups.c.id == group)
            .values(
                state=state,
                first_side_effect_at=NOW if started else None,
                sent_count=1 if state == "sent" else 0,
            )
        )
    request = await _request(db_session, account, conversation)
    for _ in range(2):
        await _advance(db_session, account, request)
        assert (
            await db_session.scalar(
                select(s.proactive_budget_reservations.c.state).where(
                    s.proactive_budget_reservations.c.id == reservation.id
                )
            )
            == expected
        )
        buckets = (
            await db_session.execute(
                select(
                    s.proactive_budget_buckets.c.held_count,
                    s.proactive_budget_buckets.c.committed_count,
                ).where(s.proactive_budget_buckets.c.account_id == account)
            )
        ).all()
        assert len(buckets) == 2
        assert set(buckets) == {(1 if expected == "held" else 0, 1 if started else 0)}
    if expected == "held":
        assert (
            await db_session.scalar(
                select(s.data_erasure_requests.c.last_error_code).where(
                    s.data_erasure_requests.c.id == request
                )
            )
            == "ERASURE_BUDGET_PENDING"
        )
    with pytest.raises(DBAPIError, match="ERASURE_BUDGET_BLOCKED"):
        async with db_session.begin_nested():
            # Even a stale caller with the pre-deletion IDs cannot reauthorize a hold.
            await db_session.execute(
                update(s.proactive_budget_reservations)
                .where(s.proactive_budget_reservations.c.id == reservation.id)
                .values(state="held", terminal_at=None, committed_at=None)
            )


@pytest.mark.integration
async def test_export_erasure_blocks_lossy_migration_rollback(
    isolated_scope_erasure_session: AsyncSession,
) -> None:
    session = isolated_scope_erasure_session
    account, conversation, _, _ = await seed_scope(session)
    await _export(session, account, None, "requested")
    await _request(session, account, conversation)
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    connection = await session.connection()

    def downgrade(sync_connection: object) -> None:
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.attributes["connection"] = sync_connection
        command.downgrade(config, "0030_scope_derived_erasure")

    with pytest.raises(RuntimeError, match="MIGRATION_0031_DOWNGRADE_REQUIRES_NO_EXPORT_ERASURE"):
        async with connection.begin_nested():
            await connection.run_sync(downgrade)
    assert (
        await session.scalar(text("SELECT version_num FROM alembic_version"))
        == "0036_worker_complete"
    )
