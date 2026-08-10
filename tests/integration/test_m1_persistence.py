import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.locks import try_advisory_scope_lock
from telegram_userbot.adapters.persistence.records import AccountRecord, NewJobRecord
from telegram_userbot.adapters.persistence.repositories import (
    AccountRepository,
    ConversationRepository,
    DurableJobRepository,
    MessageRedactionRepository,
)
from telegram_userbot.adapters.persistence.schema import (
    account_peers,
    accounts,
    background_jobs,
    contacts,
    conversations,
    message_events,
    message_revisions,
    messages,
    migration_progress,
    telegram_peers,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


async def seed_conversation(session: AsyncSession) -> tuple[UUID, UUID, UUID]:
    account_id = uuid7()
    peer_id = uuid7()
    account_peer_id = uuid7()
    contact_id = uuid7()
    conversation_id = uuid7()
    await session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="synthetic-owner",
            status="active",
            default_timezone="Asia/Shanghai",
        )
    )
    await session.execute(
        insert(telegram_peers).values(
            id=peer_id,
            peer_type="user",
            telegram_peer_id=peer_id.int % 2**63,
            is_bot=False,
        )
    )
    await session.execute(
        insert(account_peers).values(
            id=account_peer_id,
            account_id=account_id,
            peer_id=peer_id,
            observed_is_contact=True,
            metadata_schema_version=1,
        )
    )
    await session.execute(
        insert(contacts).values(
            id=contact_id,
            account_id=account_id,
            account_peer_id=account_peer_id,
            automation_status="allowed",
        )
    )
    await session.execute(
        insert(conversations).values(
            id=conversation_id,
            account_id=account_id,
            contact_id=contact_id,
            account_peer_id=account_peer_id,
            telegram_chat_id=peer_id.int % 2**63,
        )
    )
    return account_id, conversation_id, account_peer_id


async def seed_message(
    session: AsyncSession, account_id: UUID, conversation_id: UUID
) -> tuple[UUID, UUID]:
    event_uuid = uuid7()
    event_id = await session.scalar(
        insert(message_events)
        .values(
            event_uuid=event_uuid,
            account_id=account_id,
            conversation_id=conversation_id,
            event_kind="incoming.create",
            telegram_message_id=10,
            fingerprint_version=1,
            update_fingerprint=b"f" * 32,
            ordering_key="v1:00000010",
            metadata_schema_version=1,
        )
        .returning(message_events.c.id)
    )
    assert event_id is not None
    message_id = uuid7()
    revision_id = uuid7()
    await session.execute(
        insert(messages).values(
            id=message_id,
            account_id=account_id,
            conversation_id=conversation_id,
            telegram_message_id=10,
            direction="incoming",
            role="user",
            source="telegram_user",
            source_status="resolved",
            current_revision_no=1,
            telegram_created_at=NOW,
            first_observed_at=NOW,
            last_observed_at=NOW,
            metadata_schema_version=1,
        )
    )
    await session.execute(
        insert(message_revisions).values(
            id=revision_id,
            account_id=account_id,
            message_id=message_id,
            revision_no=1,
            body_kind="text",
            text_content="private synthetic text",
            entities_schema_version=1,
            entities=[],
            content_sha256=b"h" * 32,
            source_event_id=event_id,
        )
    )
    return message_id, revision_id


@pytest.mark.integration
async def test_repository_cas_and_composite_scope(db_session: AsyncSession) -> None:
    account_id, conversation_id, _ = await seed_conversation(db_session)
    assert (await AccountRepository(db_session).get(account_id)) == AccountRecord(
        account_id, account_id.int % 2**63, "synthetic-owner", "active", "Asia/Shanghai"
    )
    conversations_repository = ConversationRepository(db_session)
    changed = await conversations_repository.compare_and_set_mode(
        conversation_id=conversation_id,
        expected_version=1,
        base_mode_override="AUTO",
        contact_paused=False,
        now=NOW,
    )
    assert changed is not None
    assert changed.mode_version == 2
    assert (
        await conversations_repository.compare_and_set_mode(
            conversation_id=conversation_id,
            expected_version=1,
            base_mode_override="HUMAN",
            contact_paused=False,
            now=NOW,
        )
        is None
    )

    other_account = uuid7()
    await db_session.execute(
        insert(accounts).values(
            id=other_account,
            telegram_user_id=other_account.int % 2**63,
            display_label="other",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id)
                .values(account_id=other_account)
            )


@pytest.mark.integration
async def test_job_lease_owner_fencing_and_expiry_recovery(db_session: AsyncSession) -> None:
    jobs = DurableJobRepository(db_session)
    job_id = uuid7()
    new_job = NewJobRecord(
        job_id,
        None,
        "memory",
        "refresh",
        b"j" * 32,
        {"conversation_id": str(uuid7())},
        NOW,
    )
    assert await jobs.create(new_job) == job_id
    assert (
        await jobs.create(
            NewJobRecord(
                uuid7(),
                new_job.account_id,
                new_job.queue_name,
                new_job.job_type,
                new_job.idempotency_key,
                new_job.payload,
                new_job.available_at,
                new_job.priority,
                new_job.max_attempts,
            )
        )
        == job_id
    )

    owner = uuid7()
    claimed = await jobs.claim_next(queue_name="memory", owner=owner, now=NOW)
    assert claimed is not None
    assert claimed.fencing_token == 1
    assert not await jobs.complete(
        job_id=job_id, owner=uuid7(), fencing_token=claimed.fencing_token, now=NOW
    )
    assert await jobs.renew(
        job_id=job_id, owner=owner, fencing_token=claimed.fencing_token, now=NOW
    )
    assert await jobs.recover_expired(now=NOW + timedelta(seconds=61)) == 1
    assert not await jobs.complete(
        job_id=job_id,
        owner=owner,
        fencing_token=claimed.fencing_token,
        now=NOW + timedelta(seconds=61),
    )

    terminal_id = uuid7()
    await jobs.create(
        NewJobRecord(
            terminal_id,
            None,
            "terminal",
            "refresh",
            b"d" * 32,
            {"conversation_id": str(uuid7())},
            NOW,
            max_attempts=1,
        )
    )
    terminal_claim = await jobs.claim_next(queue_name="terminal", owner=owner, now=NOW)
    assert terminal_claim is not None
    assert await jobs.recover_expired(now=NOW + timedelta(seconds=61)) == 1
    assert (
        await db_session.scalar(
            select(background_jobs.c.state).where(background_jobs.c.id == terminal_id)
        )
        == "dead_letter"
    )


@pytest.mark.integration
async def test_one_way_redaction_and_event_idempotency(db_session: AsyncSession) -> None:
    account_id, conversation_id, _ = await seed_conversation(db_session)
    message_id, revision_id = await seed_message(db_session, account_id, conversation_id)
    redaction = MessageRedactionRepository(db_session)
    cleanup_job = NewJobRecord(
        uuid7(),
        account_id,
        "erasure",
        "reconcile_deleted_message",
        hashlib.sha256(str(message_id).encode()).digest(),
        {"message_id": str(message_id)},
        NOW,
    )
    assert await redaction.visible_current_text(account_id=account_id, message_id=message_id) == (
        "private synthetic text",
        None,
    )
    assert (
        await redaction.redact_message(
            account_id=account_id,
            message_id=message_id,
            reason="telegram_delete",
            now=NOW + timedelta(seconds=1),
            cleanup_job=cleanup_job,
        )
        == 1
    )
    assert (
        await redaction.visible_current_text(account_id=account_id, message_id=message_id) is None
    )
    assert (
        await db_session.scalar(
            select(background_jobs.c.id).where(background_jobs.c.id == cleanup_job.id)
        )
        == cleanup_job.id
    )

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(message_revisions)
                .where(message_revisions.c.id == revision_id)
                .values(text_content="restored", redacted_at=None, redaction_reason=None)
            )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(message_events).values(
                    event_uuid=uuid7(),
                    account_id=account_id,
                    conversation_id=conversation_id,
                    event_kind="incoming.create",
                    telegram_message_id=10,
                    fingerprint_version=1,
                    update_fingerprint=b"f" * 32,
                    ordering_key="duplicate",
                    metadata_schema_version=1,
                )
            )


@pytest.mark.integration
async def test_representative_message_query_uses_declared_index(db_session: AsyncSession) -> None:
    account_id, conversation_id, _ = await seed_conversation(db_session)
    count = 1000
    message_ids = [uuid7() for _ in range(count)]
    event_values = [
        {
            "event_uuid": uuid7(),
            "account_id": account_id,
            "conversation_id": conversation_id,
            "event_kind": "incoming.create",
            "telegram_message_id": index + 100,
            "fingerprint_version": 1,
            "update_fingerprint": hashlib.sha256(f"event-{index}".encode()).digest(),
            "ordering_key": f"v1:{index + 100:08d}",
            "metadata_schema_version": 1,
        }
        for index in range(count)
    ]
    event_ids = list(
        (
            await db_session.execute(
                insert(message_events).returning(message_events.c.id), event_values
            )
        ).scalars()
    )
    await db_session.execute(
        insert(messages),
        [
            {
                "id": message_id,
                "account_id": account_id,
                "conversation_id": conversation_id,
                "telegram_message_id": index + 100,
                "direction": "incoming",
                "role": "user",
                "source": "telegram_user",
                "source_status": "resolved",
                "current_revision_no": 1,
                "telegram_created_at": NOW + timedelta(seconds=index),
                "first_observed_at": NOW + timedelta(seconds=index),
                "last_observed_at": NOW + timedelta(seconds=index),
                "metadata_schema_version": 1,
            }
            for index, message_id in enumerate(message_ids)
        ],
    )
    await db_session.execute(
        insert(message_revisions),
        [
            {
                "id": uuid7(),
                "account_id": account_id,
                "message_id": message_id,
                "revision_no": 1,
                "body_kind": "none",
                "entities_schema_version": 1,
                "source_event_id": event_id,
            }
            for message_id, event_id in zip(message_ids, event_ids, strict=True)
        ],
    )
    await db_session.execute(text("ANALYZE messages"))
    plan = (
        await db_session.execute(
            text(
                """
                EXPLAIN (COSTS TRUE, FORMAT TEXT)
                SELECT id, telegram_message_id
                FROM messages
                WHERE conversation_id = :conversation_id
                ORDER BY telegram_created_at DESC, telegram_message_id DESC
                LIMIT 50
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalars()
    rendered = "\n".join(plan)
    assert "ix_messages_conversation_created" in rendered
    assert account_id is not None


@pytest.mark.integration
async def test_two_workers_and_advisory_lock_have_single_owner(
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    job_id = uuid7()
    queue_name = f"concurrent-{job_id}"
    async with factory() as creator, creator.begin():
        await DurableJobRepository(creator).create(
            NewJobRecord(
                job_id,
                None,
                queue_name,
                "synthetic",
                hashlib.sha256(str(job_id).encode()).digest(),
                {"scope_id": str(uuid7())},
                NOW,
            )
        )

    ready = asyncio.Event()
    release = asyncio.Event()

    async def claim(owner: UUID, wait_for_owner: bool) -> bool:
        async with factory() as worker, worker.begin():
            if wait_for_owner:
                await ready.wait()
            claimed = await DurableJobRepository(worker).claim_next(
                queue_name=queue_name, owner=owner, now=NOW
            )
            if not wait_for_owner:
                ready.set()
                await release.wait()
            else:
                release.set()
            return claimed is not None

    results = await asyncio.gather(claim(uuid7(), False), claim(uuid7(), True))
    assert sorted(results) == [False, True]

    account_id = uuid7()
    entity_id = uuid7()
    first = factory()
    second = factory()
    try:
        await first.begin()
        await second.begin()
        assert await try_advisory_scope_lock(
            first, scope="conversation", account_id=account_id, entity_id=entity_id
        )
        assert not await try_advisory_scope_lock(
            second, scope="conversation", account_id=account_id, entity_id=entity_id
        )
        await first.commit()
        assert await try_advisory_scope_lock(
            second, scope="conversation", account_id=account_id, entity_id=entity_id
        )
        await second.rollback()
    finally:
        await first.close()
        await second.close()


@pytest.mark.integration
@pytest.mark.recovery
async def test_interrupted_progress_resumes_with_version_cas(db_session: AsyncSession) -> None:
    await db_session.execute(
        insert(migration_progress).values(
            migration_key="synthetic_backfill",
            step_name="messages",
            watermark="100",
        )
    )
    async with db_session.begin_nested() as interrupted:
        await db_session.execute(
            update(migration_progress)
            .where(
                migration_progress.c.migration_key == "synthetic_backfill",
                migration_progress.c.version == 1,
            )
            .values(watermark="200", version=2)
        )
        await interrupted.rollback()
    assert (
        await db_session.scalar(
            select(migration_progress.c.watermark).where(
                migration_progress.c.migration_key == "synthetic_backfill"
            )
        )
        == "100"
    )
    resumed = await db_session.execute(
        update(migration_progress)
        .where(
            migration_progress.c.migration_key == "synthetic_backfill",
            migration_progress.c.version == 1,
        )
        .values(watermark="200", version=2, completed=True)
        .returning(migration_progress.c.version)
    )
    assert resumed.scalar_one() == 2
