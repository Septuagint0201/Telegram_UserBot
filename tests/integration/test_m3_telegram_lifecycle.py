from collections import deque
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    AttemptCompletionRecord,
    NewDeliveryGroupRecord,
    ReadHighWatermarkRecord,
    TypingLeaseRecord,
)
from telegram_userbot.adapters.persistence.schema import (
    account_peers,
    accounts,
    background_jobs,
    contacts,
    conversations,
    message_events,
    message_media,
    message_revisions,
    messages,
    outbound_attempts,
    outbound_delivery_groups,
    telegram_operations,
    telegram_peers,
    telegram_read_states,
    telegram_typing_states,
)
from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)
from telegram_userbot.adapters.telegram_user import (
    FakeSendOutcome,
    PeerAdmission,
    RawMedia,
    RawTelegramUpdate,
    ReplayTelegramGateway,
    normalize_update,
)
from telegram_userbot.application.ports.telegram import TelegramTextRequest
from telegram_userbot.domain.messaging import (
    AttemptOutcome,
    Direction,
    EventKind,
    MediaKind,
    NormalizedTelegramEvent,
    OutboundChunk,
    PeerKind,
    payload_sha256,
    stable_telegram_random_id,
)
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 2, 3, 4, 5, 6, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_conversation(session: AsyncSession) -> tuple[UUID, UUID, int]:
    account_id = uuid7()
    peer_id = uuid7()
    account_peer_id = uuid7()
    contact_id = uuid7()
    conversation_id = uuid7()
    telegram_chat_id = peer_id.int % 2**63
    await session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="m3-synthetic-owner",
            status="active",
        )
    )
    await session.execute(
        insert(telegram_peers).values(
            id=peer_id,
            peer_type="user",
            telegram_peer_id=telegram_chat_id,
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
            telegram_chat_id=telegram_chat_id,
        )
    )
    return account_id, conversation_id, telegram_chat_id


def normalized(  # noqa: PLR0913 - explicit dimensions document the event fixture
    *,
    account_id: UUID,
    conversation_id: UUID | None,
    telegram_chat_id: int,
    update_identity: str,
    kind: EventKind = EventKind.MESSAGE_CREATED,
    message_id: int = 10,
    direction: Direction = Direction.INCOMING,
    text_content: str | None = "synthetic private message",
    event_at: datetime = NOW,
    peer_kind: PeerKind = PeerKind.PRIVATE_USER,
    grouped_id: int | None = None,
    random_id: int | None = None,
    media: tuple[RawMedia, ...] = (),
) -> NormalizedTelegramEvent:
    return normalize_update(
        event_uuid=uuid7(),
        admission=PeerAdmission(account_id, conversation_id, peer_kind, telegram_chat_id),
        raw=RawTelegramUpdate(
            update_identity,
            kind,
            event_at,
            telegram_event_at=event_at,
            telegram_message_id=message_id,
            grouped_id=grouped_id,
            direction=direction,
            outbound_random_id=random_id,
            text=text_content,
            media=media,
        ),
    )


@pytest.mark.integration
async def test_ingest_deduplicates_revisions_and_tombstone_wins(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    created = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="create:10",
    )
    first = await repository.ingest(created)
    replay = await repository.ingest(created)
    assert first.revision_no == 1
    assert replay.duplicate
    assert (
        await db_session.scalar(
            select(text("COUNT(*)")).select_from(messages).where(messages.c.id == first.message_id)
        )
        == 1
    )

    edited = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="edit:10:2",
        kind=EventKind.MESSAGE_EDITED,
        text_content="edited synthetic message",
        event_at=NOW + timedelta(seconds=2),
    )
    assert (await repository.ingest(edited)).revision_no == 2
    stale = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="edit:10:1",
        kind=EventKind.MESSAGE_EDITED,
        text_content="stale private body",
        event_at=NOW + timedelta(seconds=1),
    )
    assert (await repository.ingest(stale)).revision_no == 2

    deleted = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="delete:10",
        kind=EventKind.MESSAGE_DELETED,
        text_content=None,
        event_at=NOW + timedelta(seconds=3),
    )
    await repository.ingest(deleted)
    late_create = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="late-create:10",
        text_content="must never reappear",
        event_at=NOW - timedelta(seconds=3),
    )
    await repository.ingest(late_create)
    tombstone = (
        (
            await db_session.execute(
                select(messages.c.is_tombstone, messages.c.current_revision_no).where(
                    messages.c.id == first.message_id
                )
            )
        )
        .mappings()
        .one()
    )
    revision_contents = tuple(
        tuple(row)
        for row in (
            await db_session.execute(
                select(message_revisions.c.text_content, message_revisions.c.caption).where(
                    message_revisions.c.message_id == first.message_id
                )
            )
        ).all()
    )
    assert tombstone == {"is_tombstone": True, "current_revision_no": 2}
    assert revision_contents == ((None, None), (None, None))
    assert await db_session.scalar(
        select(background_jobs.c.id).where(
            background_jobs.c.job_type == "memory.reconcile_message_delete",
            background_jobs.c.payload["message_id"].as_string() == str(first.message_id),
        )
    )


@pytest.mark.integration
async def test_delete_before_create_and_unsupported_peer_are_content_free(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    delete_first = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="delete-first:77",
        kind=EventKind.MESSAGE_DELETED,
        message_id=77,
        text_content=None,
    )
    result = await repository.ingest(delete_first)
    await repository.ingest(
        normalized(
            account_id=account_id,
            conversation_id=conversation_id,
            telegram_chat_id=chat_id,
            update_identity="create-late:77",
            message_id=77,
            text_content="deleted-before-observed",
            event_at=NOW - timedelta(seconds=2),
        )
    )
    assert (
        await db_session.scalar(
            select(message_revisions.c.text_content).where(
                message_revisions.c.message_id == result.message_id
            )
        )
        is None
    )

    unsupported = normalized(
        account_id=account_id,
        conversation_id=None,
        telegram_chat_id=-100999,
        update_identity="group:private-content",
        message_id=88,
        peer_kind=PeerKind.GROUP,
        text_content="must be stripped",
        media=(RawMedia(MediaKind.VIDEO, file_reference="must-be-stripped"),),
    )
    unsupported_result = await repository.ingest(unsupported)
    event_metadata = await db_session.scalar(
        select(message_events.c.metadata).where(message_events.c.id == unsupported_result.event_id)
    )
    assert "must be stripped" not in str(event_metadata)
    assert "must-be-stripped" not in str(event_metadata)
    assert (
        await db_session.scalar(
            select(text("COUNT(*)"))
            .select_from(messages)
            .where(messages.c.account_id == account_id)
        )
        == 1
    )


@pytest.mark.integration
async def test_album_and_media_metadata_preserve_stable_order_without_binary(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    for message_id in (102, 101):
        await repository.ingest(
            normalized(
                account_id=account_id,
                conversation_id=conversation_id,
                telegram_chat_id=chat_id,
                update_identity=f"album:{message_id}",
                message_id=message_id,
                grouped_id=500,
                media=(
                    RawMedia(
                        MediaKind.PHOTO,
                        file_reference=f"photo-{message_id}",
                        mime_type="image/jpeg",
                        size=123,
                    ),
                    RawMedia(MediaKind.VOICE, file_reference=f"voice-{message_id}"),
                ),
            )
        )
    album_ids = tuple(
        (
            await db_session.execute(
                select(messages.c.telegram_message_id)
                .where(
                    messages.c.conversation_id == conversation_id,
                    messages.c.grouped_id == 500,
                )
                .order_by(
                    messages.c.telegram_created_at,
                    messages.c.telegram_message_id,
                    messages.c.first_observed_at,
                    messages.c.id,
                )
            )
        ).scalars()
    )
    media_rows = (
        (
            await db_session.execute(
                select(
                    message_media.c.media_kind,
                    message_media.c.media_object_id,
                    message_media.c.metadata,
                )
                .where(message_media.c.account_id == account_id)
                .order_by(message_media.c.media_kind)
            )
        )
        .mappings()
        .all()
    )
    assert album_ids == (101, 102)
    assert all(row["media_object_id"] is None for row in media_rows)
    assert {row["media_kind"] for row in media_rows} == {"photo", "voice"}
    assert {row["media_kind"]: row["metadata"]["download_eligible"] for row in media_rows} == {
        "photo": True,
        "voice": False,
    }


def delivery_plan(
    account_id: UUID,
    conversation_id: UUID,
    *,
    chunks: int = 1,
) -> tuple[NewDeliveryGroupRecord, tuple[OutboundChunk, ...]]:
    group_id = uuid7()
    entropy = bytes(range(16))
    outbound = tuple(
        OutboundChunk(
            intent_id := uuid7(),
            sequence_no,
            stable_telegram_random_id(intent_id, entropy),
            f"synthetic output {sequence_no}",
            payload_sha256(f"synthetic output {sequence_no}"),
        )
        for sequence_no in range(chunks)
    )
    return (
        NewDeliveryGroupRecord(
            group_id,
            account_id,
            conversation_id,
            uuid7(),
            "ai",
            sha256(f"group:{group_id}".encode()).digest(),
            NOW,
        ),
        outbound,
    )


async def dispatch_fake(
    repository: TelegramLifecycleRepository,
    service: TelegramDeliveryService,
    *,
    intent_id: UUID,
    now: datetime,
) -> bool:
    """Exercise the production sequence; runtime commits between these three steps."""

    claimed = await repository.claim_intent(intent_id=intent_id, now=now)
    if claimed is None:
        return False
    completion = await service.send_prepared(intent=claimed, now=now)
    await repository.finish_attempt(intent=claimed, completion=completion)
    return True


@pytest.mark.integration
async def test_outbound_retries_reuse_random_id_and_partial_group(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _ = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    group, chunks = delivery_plan(account_id, conversation_id, chunks=2)
    assert await repository.create_delivery_group(group=group, chunks=chunks) == group.id
    assert await repository.create_delivery_group(group=group, chunks=chunks) == group.id

    fake = ReplayTelegramGateway(
        outcomes=deque(
            [FakeSendOutcome.FLOOD_WAIT, FakeSendOutcome.SUCCESS, FakeSendOutcome.PERMANENT]
        )
    )
    service = TelegramDeliveryService(fake)
    assert await dispatch_fake(repository, service, intent_id=chunks[0].intent_id, now=NOW)
    first_wait = await repository.get_intent(chunks[0].intent_id)
    assert first_wait is not None
    assert first_wait.state == "retry_wait"
    assert not await dispatch_fake(
        repository, service, intent_id=chunks[0].intent_id, now=NOW + timedelta(seconds=1)
    )
    assert await dispatch_fake(
        repository, service, intent_id=chunks[0].intent_id, now=NOW + timedelta(seconds=7)
    )
    assert await dispatch_fake(
        repository, service, intent_id=chunks[1].intent_id, now=NOW + timedelta(seconds=8)
    )
    assert [request.random_id for request in fake.send_requests[:2]] == [
        chunks[0].telegram_random_id,
        chunks[0].telegram_random_id,
    ]
    group_state = await db_session.scalar(
        select(outbound_delivery_groups.c.state).where(outbound_delivery_groups.c.id == group.id)
    )
    assert group_state == "partial"


@pytest.mark.integration
async def test_send_unknown_and_crash_after_send_reconcile_without_blind_resend(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    group, chunks = delivery_plan(account_id, conversation_id)
    await repository.create_delivery_group(group=group, chunks=chunks)
    fake = ReplayTelegramGateway(outcomes=deque([FakeSendOutcome.UNKNOWN_AFTER_ACCEPT]))
    service = TelegramDeliveryService(fake)
    await dispatch_fake(repository, service, intent_id=chunks[0].intent_id, now=NOW)
    unknown = await repository.get_intent(chunks[0].intent_id)
    assert unknown is not None
    assert unknown.state == "unknown"
    assert not await dispatch_fake(
        repository, service, intent_id=chunks[0].intent_id, now=NOW + timedelta(minutes=1)
    )

    observed = normalized(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_chat_id=chat_id,
        update_identity="outgoing-map:1000",
        message_id=1000,
        direction=Direction.OUTGOING,
        text_content="synthetic output 0",
        random_id=chunks[0].telegram_random_id,
        event_at=NOW + timedelta(seconds=2),
    )
    projected = await repository.ingest(observed)
    reconciled = await repository.get_intent(chunks[0].intent_id)
    assert projected.source == "ai"
    assert reconciled is not None
    assert reconciled.state == "sent"
    assert len(fake.send_requests) == 1

    crash_group, crash_chunks = delivery_plan(account_id, conversation_id)
    await repository.create_delivery_group(group=crash_group, chunks=crash_chunks)
    claimed = await repository.claim_intent(intent_id=crash_chunks[0].intent_id, now=NOW)
    assert claimed is not None
    assert (
        await db_session.scalar(
            select(outbound_attempts.c.state).where(
                outbound_attempts.c.intent_id == crash_chunks[0].intent_id
            )
        )
        == "started"
    )
    accepted = await fake.send_text(
        TelegramTextRequest(
            AccountId(account_id),
            ConversationId(conversation_id),
            RunId(crash_group.model_run_id or crash_group.id),
            crash_chunks[0].telegram_random_id,
            SensitiveValue(crash_chunks[0].text),
        )
    )
    assert accepted.telegram_message_id == 1001
    assert (
        await repository.recover_stale_sending(
            older_than=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=2),
        )
        == 1
    )
    recovered = await repository.get_intent(crash_chunks[0].intent_id)
    assert recovered is not None
    assert recovered.state == "unknown"


@pytest.mark.integration
async def test_human_source_read_high_watermark_typing_and_m3_roles(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    human = await repository.ingest(
        normalized(
            account_id=account_id,
            conversation_id=conversation_id,
            telegram_chat_id=chat_id,
            update_identity="human:501",
            message_id=501,
            direction=Direction.OUTGOING,
            text_content="human authored",
        )
    )
    version = (
        await db_session.execute(
            select(conversations.c.mode_version, conversations.c.content_revision).where(
                conversations.c.id == conversation_id
            )
        )
    ).one()
    assert human.source == "human"
    assert version == (2, 1)

    read_key = sha256(b"read:501").digest()
    read_record = ReadHighWatermarkRecord(uuid7(), account_id, conversation_id, 501, read_key, NOW)
    assert await repository.record_read_high_watermark(record=read_record)
    assert not await repository.record_read_high_watermark(record=read_record)
    await repository.record_read_high_watermark(
        record=ReadHighWatermarkRecord(
            uuid7(), account_id, conversation_id, 499, sha256(b"read:499").digest(), NOW
        )
    )
    assert (
        await db_session.scalar(
            select(telegram_read_states.c.max_telegram_message_id).where(
                telegram_read_states.c.conversation_id == conversation_id
            )
        )
        == 501
    )
    assert (
        await db_session.scalar(
            select(text("COUNT(*)"))
            .select_from(telegram_operations)
            .where(telegram_operations.c.conversation_id == conversation_id)
        )
        == 2
    )

    lease_token = uuid7()
    await repository.set_typing_lease(
        record=TypingLeaseRecord(
            account_id,
            conversation_id,
            lease_token,
            NOW + timedelta(seconds=8),
            NOW,
        )
    )
    await repository.set_typing_lease(
        record=TypingLeaseRecord(
            account_id, conversation_id, None, None, NOW + timedelta(seconds=1)
        )
    )
    typing = (
        (
            await db_session.execute(
                select(telegram_typing_states).where(
                    telegram_typing_states.c.conversation_id == conversation_id
                )
            )
        )
        .mappings()
        .one()
    )
    assert typing["active"] is False
    assert typing["version"] == 2

    privileges = await db_session.execute(
        text(
            """
            SELECT
              has_table_privilege('telegram_userbot_app_runtime', 'outbound_intents', 'UPDATE'),
              has_table_privilege('telegram_userbot_worker_runtime', 'outbound_intents', 'SELECT'),
              has_table_privilege('telegram_userbot_worker_runtime', 'outbound_intents', 'UPDATE')
            """
        )
    )
    assert privileges.one() == (True, True, False)


@pytest.mark.integration
async def test_unknown_outgoing_with_active_intent_stays_system_pending(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    group, chunks = delivery_plan(account_id, conversation_id)
    await repository.create_delivery_group(group=group, chunks=chunks)
    await repository.claim_intent(intent_id=chunks[0].intent_id, now=NOW)
    result = await repository.ingest(
        normalized(
            account_id=account_id,
            conversation_id=conversation_id,
            telegram_chat_id=chat_id,
            update_identity="outgoing-unmapped:900",
            message_id=900,
            direction=Direction.OUTGOING,
            text_content="unmapped outgoing",
        )
    )
    assert result.source == "system_pending"
    source_status = await db_session.scalar(
        select(messages.c.source_status).where(messages.c.id == result.message_id)
    )
    assert source_status == "pending"

    corrected = await repository.ingest(
        normalized(
            account_id=account_id,
            conversation_id=conversation_id,
            telegram_chat_id=chat_id,
            update_identity="outgoing-mapped:900",
            message_id=900,
            direction=Direction.OUTGOING,
            text_content="unmapped outgoing",
            random_id=chunks[0].telegram_random_id,
            event_at=NOW + timedelta(seconds=1),
        )
    )
    corrected_row = (
        await db_session.execute(
            select(messages.c.source, messages.c.source_status).where(
                messages.c.id == result.message_id
            )
        )
    ).one()
    assert corrected.source == "ai"
    assert corrected_row == ("ai", "corrected")


@pytest.mark.integration
async def test_m3_model_run_columns_exist_without_premature_foreign_key(
    db_session: AsyncSession,
) -> None:
    foreign_keys = tuple(
        (
            await db_session.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid IN (
                      'outbound_delivery_groups'::regclass,
                      'outbound_intents'::regclass
                    ) AND contype = 'f'
                    ORDER BY conname
                    """
                )
            )
        ).scalars()
    )
    assert all("model_run" not in name for name in foreign_keys)
    assert (
        await db_session.scalar(
            text(
                """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN ('outbound_delivery_groups', 'outbound_intents')
              AND column_name = 'model_run_id'
            """
            )
        )
        == 2
    )


@pytest.mark.integration
async def test_outbound_attempt_can_only_complete_once(db_session: AsyncSession) -> None:
    account_id, conversation_id, _ = await seed_conversation(db_session)
    repository = TelegramLifecycleRepository(db_session)
    group, chunks = delivery_plan(account_id, conversation_id)
    await repository.create_delivery_group(group=group, chunks=chunks)
    claimed = await repository.claim_intent(intent_id=chunks[0].intent_id, now=NOW)
    assert claimed is not None
    await repository.finish_attempt(
        intent=claimed,
        completion=AttemptCompletionRecord(AttemptOutcome.SUCCEEDED, NOW, telegram_message_id=800),
    )
    with pytest.raises(DBAPIError, match="one-way completion"):
        await db_session.execute(
            update(outbound_attempts)
            .where(outbound_attempts.c.intent_id == chunks[0].intent_id)
            .values(error_code="must_not_mutate")
        )
