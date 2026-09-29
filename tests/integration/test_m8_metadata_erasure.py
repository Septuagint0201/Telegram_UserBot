"""Real-role profile, auxiliary payload, queue and audit erasure boundaries."""

import asyncio
import hashlib
from datetime import timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.telegram_peer import (
    PostgresTelegramPeerRepository,
    TelegramPeerAdmissionError,
)
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.adapters.telegram_user import (
    PeerAdmission,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.application.ports.telegram_peer import TelegramPrivatePeerObservation
from telegram_userbot.domain.messaging import EventKind, PeerKind
from tests.integration.test_m5_context_media import NOW, seed_scope
from tests.integration.test_m6_account_scope_constraints import _seed_memory_run, _seed_profile
from tests.integration.test_m8_scope_erasure import _advance, _request

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _private_metadata(
    session: AsyncSession, account: UUID, conversation: UUID, turn: UUID, revision: UUID
) -> dict[str, UUID | int]:
    contact, peer = (
        await session.execute(
            select(s.conversations.c.contact_id, s.conversations.c.account_peer_id).where(
                s.conversations.c.id == conversation
            )
        )
    ).one()
    message = await session.scalar(
        select(s.message_revisions.c.message_id).where(s.message_revisions.c.id == revision)
    )
    await session.execute(
        update(s.account_peers)
        .where(s.account_peers.c.id == peer)
        .values(
            username="private_username",
            display_name="private_display",
            access_hash=123,
            metadata={"private": "profile"},
            observed_is_contact=True,
        )
    )
    await session.execute(
        update(s.contacts)
        .where(s.contacts.c.id == contact)
        .values(timezone="Asia/Tokyo", locale="ja")
    )
    await session.execute(
        update(s.messages).where(s.messages.c.id == message).values(metadata={"private": "body"})
    )
    media, reaction, job = uuid7(), uuid7(), uuid7()
    await session.execute(
        insert(s.message_media).values(
            id=media,
            account_id=account,
            message_revision_id=revision,
            media_kind="photo",
            position=1,
            telegram_file_ref="private-access-reference",
            original_name_sanitized="private-name.jpg",
            metadata_schema_version=1,
            metadata={"private": "attachment"},
        )
    )
    await session.execute(
        insert(s.message_reactions).values(
            id=reaction,
            account_id=account,
            message_id=message,
            reaction_key="private-reaction",
            active=True,
        )
    )
    await session.execute(
        insert(s.background_jobs).values(
            id=job,
            account_id=account,
            queue_name="worker",
            job_type="memory.refresh_completed_turn",
            idempotency_key=hashlib.sha256(job.bytes).digest(),
            payload_schema_version=1,
            payload={"turn_id": str(turn), "legacy_body": "private"},
            state="leased",
            lease_owner=uuid7(),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    )
    outbox = await session.scalar(
        insert(s.transactional_outbox)
        .values(
            account_id=account,
            topic="durable_job.available",
            aggregate_type="background_job",
            aggregate_id=str(job),
            aggregate_version=1,
            payload_schema_version=1,
            payload={"job_id": str(job), "dispatch_generation": 1, "legacy_body": "private"},
        )
        .returning(s.transactional_outbox.c.id)
    )
    audit = await session.scalar(
        insert(s.audit_log)
        .values(
            account_id=account,
            actor_type="admin",
            actor_ref="private-actor",
            action="synthetic_action",
            target_type="background_job",
            target_id=str(job),
            result="success",
            request_id=uuid7(),
            metadata_schema_version=1,
            metadata={"private": "audit"},
            before_sha256=b"b" * 32,
            after_sha256=b"a" * 32,
        )
        .returning(s.audit_log.c.id)
    )
    assert isinstance(message, UUID)
    assert isinstance(outbox, int)
    assert isinstance(audit, int)
    return {
        "contact": contact,
        "peer": peer,
        "message": message,
        "media": media,
        "reaction": reaction,
        "job": job,
        "outbox": outbox,
        "audit": audit,
    }


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_metadata_erasure_is_scoped_replay_safe_and_preserves_cleanup_work(
    db_session: AsyncSession,
    scope: str,
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    first = await _private_metadata(db_session, account, conversation, turn, revision)
    _, other_conversation, other_turn, other_revision = await seed_scope(
        db_session, existing_account_id=account
    )
    sibling = await _private_metadata(
        db_session, account, other_conversation, other_turn, other_revision
    )
    foreign_account, foreign_conversation, foreign_turn, foreign_revision = await seed_scope(
        db_session
    )
    foreign = await _private_metadata(
        db_session, foreign_account, foreign_conversation, foreign_turn, foreign_revision
    )
    # One global Telegram identity can carry independent per-account profiles.
    shared_peer = uuid7()
    await db_session.execute(
        insert(s.account_peers).values(
            id=shared_peer,
            account_id=foreign_account,
            peer_id=select(s.account_peers.c.peer_id)
            .where(s.account_peers.c.id == first["peer"])
            .scalar_subquery(),
            username="other_account_profile",
            last_observed_at=NOW,
            metadata_schema_version=1,
        )
    )
    request = await _request(db_session, account, conversation, scope=scope)
    # The cleanup queue must remain runnable after deletion intent, including account wipe.
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    await db_session.execute(
        insert(s.background_jobs).values(
            id=request,
            account_id=account,
            queue_name="worker",
            job_type="memory.reconcile_erasure",
            idempotency_key=hashlib.sha256(request.bytes).digest(),
            payload_schema_version=1,
            payload={"request_id": str(request)},
        )
    )
    cleanup_notice = await db_session.scalar(
        insert(s.transactional_outbox)
        .values(
            account_id=account,
            topic="durable_job.available",
            aggregate_type="background_job",
            aggregate_id=str(request),
            aggregate_version=1,
            payload_schema_version=1,
            payload={"job_id": str(request), "dispatch_generation": 1},
        )
        .returning(s.transactional_outbox.c.id)
    )
    await db_session.execute(text("RESET ROLE"))
    for _ in range(2):
        await _advance(db_session, account, request)
        for data, erased in ((first, True), (sibling, scope == "account"), (foreign, False)):
            peer = (
                await db_session.execute(
                    select(s.account_peers).where(s.account_peers.c.id == data["peer"])
                )
            ).one()
            assert (peer.metadata_erased_at is not None) == erased
            assert peer.username == (None if erased else "private_username")
            assert peer.access_hash == (None if erased else 123)
            assert peer.metadata == ({} if erased else {"private": "profile"})
            media = (
                await db_session.execute(
                    select(s.message_media).where(s.message_media.c.id == data["media"])
                )
            ).one()
            assert media.telegram_file_ref == (None if erased else "private-access-reference")
            assert (
                await db_session.scalar(
                    select(s.message_reactions.c.id).where(
                        s.message_reactions.c.id == data["reaction"]
                    )
                )
                is None
            ) == erased
            job = (
                await db_session.execute(
                    select(s.background_jobs).where(s.background_jobs.c.id == data["job"])
                )
            ).one()
            assert job.state == ("cancelled" if erased else "leased")
            assert (job.payload == {}) == erased
            assert job.version == (2 if erased else 1)
            assert job.fencing_token == (1 if erased else 0)
            outbox = (
                await db_session.execute(
                    select(s.transactional_outbox).where(
                        s.transactional_outbox.c.id == data["outbox"]
                    )
                )
            ).one()
            assert (outbox.published_at is not None) == erased
            assert (outbox.payload == {}) == erased
            audit = (
                await db_session.execute(
                    select(s.audit_log).where(s.audit_log.c.id == data["audit"])
                )
            ).one()
            assert (audit.metadata == {}) == erased
            assert audit.action == "synthetic_action"
            assert audit.result == "success"
            assert audit.actor_ref == (None if erased else "private-actor")
        assert await db_session.scalar(
            select(s.background_jobs.c.payload).where(s.background_jobs.c.id == request)
        ) == {"request_id": str(request)}
        assert (
            await db_session.scalar(
                select(s.transactional_outbox.c.published_at).where(
                    s.transactional_outbox.c.id == cleanup_notice
                )
            )
            is None
        )
    assert (
        await db_session.scalar(
            select(s.accounts.c.display_label).where(s.accounts.c.id == account)
        )
        is None
    ) == (scope == "account")
    assert (
        len(
            (
                await db_session.execute(
                    select(s.audit_log.c.id).where(
                        s.audit_log.c.request_id == request,
                        s.audit_log.c.action == "scope_metadata_redacted",
                    )
                )
            ).all()
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.completed_at).where(
                s.data_erasure_requests.c.id == request
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(s.account_peers.c.username).where(s.account_peers.c.id == shared_peer)
        )
        == "other_account_profile"
    )


@pytest.mark.integration
async def test_late_metadata_and_queue_revival_are_blocked_but_audit_facts_survive(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    data = await _private_metadata(db_session, account, conversation, turn, revision)
    request = await _request(db_session, account, conversation)
    # Direct writes are blocked even before the worker changes the contact status.
    with pytest.raises(DBAPIError, match="ERASURE_METADATA_WRITE_BLOCKED"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.account_peers)
                .where(s.account_peers.c.id == data["peer"])
                .values(username="late_profile")
            )
    await _advance(db_session, account, request)
    for statement in (
        update(s.account_peers)
        .where(s.account_peers.c.id == data["peer"])
        .values(username="restored"),
        update(s.account_peers)
        .where(s.account_peers.c.id == data["peer"])
        .values(metadata_erased_at=None),
        update(s.background_jobs)
        .where(s.background_jobs.c.id == data["job"])
        .values(state="pending"),
        update(s.transactional_outbox)
        .where(s.transactional_outbox.c.id == data["outbox"])
        .values(published_at=None),
        insert(s.message_reactions).values(
            id=uuid7(),
            account_id=account,
            message_id=data["message"],
            reaction_key="late",
            active=True,
        ),
    ):
        with pytest.raises(DBAPIError, match="ERASURE_METADATA_"):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    audit = await db_session.scalar(
        insert(s.audit_log)
        .values(
            account_id=account,
            actor_type="service",
            actor_ref="late-private-actor",
            action="late_outcome",
            target_type="conversation",
            target_id=str(conversation),
            result="failed",
            request_id=uuid7(),
            metadata_schema_version=1,
            metadata={"private": "late"},
        )
        .returning(s.audit_log.c.id)
    )
    with pytest.raises(DBAPIError) as caught:
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.audit_log).where(s.audit_log.c.id == audit).values(action="forged")
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    await db_session.execute(text("RESET ROLE"))
    row = (await db_session.execute(select(s.audit_log).where(s.audit_log.c.id == audit))).one()
    assert row.action == "late_outcome"
    assert row.result == "failed"
    assert row.actor_ref is None
    assert row.metadata == {}
    assert row.metadata_erased_at is not None


@pytest.mark.integration
async def test_peer_admission_rejects_erasure_intent_before_profile_refresh(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    data = await _private_metadata(db_session, account, conversation, turn, revision)
    account_native = await db_session.scalar(
        select(s.accounts.c.telegram_user_id).where(s.accounts.c.id == account)
    )
    peer_native = await db_session.scalar(
        select(s.conversations.c.telegram_chat_id).where(s.conversations.c.id == conversation)
    )
    assert isinstance(account_native, int)
    assert isinstance(peer_native, int)
    await _request(db_session, account, conversation)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    with pytest.raises(TelegramPeerAdmissionError, match="CONTACT_NOT_ADMISSIBLE"):
        await PostgresTelegramPeerRepository(db_session, new_uuid=uuid7).admit_private(
            TelegramPrivatePeerObservation(
                account_id=account,
                managed_telegram_user_id=account_native,
                telegram_user_id=peer_native,
                access_hash=456,
                username="late",
                display_name="late",
                observed_is_contact=True,
                observed_at=NOW,
            )
        )
    assert (
        await db_session.scalar(
            select(s.account_peers.c.username).where(s.account_peers.c.id == data["peer"])
        )
        == "private_username"
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", [EventKind.MESSAGE_CREATED, EventKind.REACTION_CHANGED])
async def test_late_telegram_events_keep_deduplication_without_projecting_private_data(
    db_session: AsyncSession,
    kind: EventKind,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    chat = await db_session.scalar(
        select(s.conversations.c.telegram_chat_id).where(s.conversations.c.id == conversation)
    )
    assert isinstance(chat, int)
    await _request(db_session, account, conversation)
    event = normalize_update(
        event_uuid=uuid7(),
        admission=PeerAdmission(account, conversation, PeerKind.PRIVATE_USER, chat),
        raw=RawTelegramUpdate(
            str(uuid7()),
            kind,
            NOW,
            telegram_message_id=11 if kind is EventKind.MESSAGE_CREATED else 10,
            text="late private body" if kind is EventKind.MESSAGE_CREATED else None,
            reaction_key="emoji:wave" if kind is EventKind.REACTION_CHANGED else None,
        ),
    )
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    repository = TelegramLifecycleRepository(db_session)
    result = await repository.ingest(event)
    assert result.message_id is None
    replay = await repository.ingest(event)
    assert replay.duplicate
    assert replay.event_id == result.event_id
    row = (
        await db_session.execute(
            select(s.message_events).where(s.message_events.c.id == result.event_id)
        )
    ).one()
    assert row.metadata == {}
    assert row.metadata_erased_at is not None
    assert row.projected_at is not None
    assert (
        await db_session.scalar(
            select(s.message_reactions.c.id).where(s.message_reactions.c.account_id == account)
        )
        is None
    )


@pytest.mark.integration
async def test_metadata_transaction_rollback_preserves_payload_and_erasure_replay(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    data = await _private_metadata(db_session, account, conversation, turn, revision)
    request = await _request(db_session, account, conversation)
    with pytest.raises(RuntimeError, match="interrupted"):  # noqa: PT012 - explicit interruption
        async with db_session.begin_nested():
            await _advance(db_session, account, request)
            raise RuntimeError("interrupted")
    assert (
        await db_session.scalar(
            select(s.account_peers.c.username).where(s.account_peers.c.id == data["peer"])
        )
        == "private_username"
    )
    assert (
        await db_session.scalar(
            select(s.background_jobs.c.state).where(s.background_jobs.c.id == data["job"])
        )
        == "leased"
    )
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.account_peers.c.username).where(s.account_peers.c.id == data["peer"])
        )
        is None
    )


@pytest.mark.integration
async def test_control_history_and_model_receipts_keep_facts_without_private_metadata(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, _ = await seed_scope(db_session)
    contact = await db_session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
    )
    profile, config, credential, _ = await _seed_profile(
        db_session,
        logical_role="memory_agent",
        profile_kind="generation",
        protocol="openai_responses",
    )
    run = await _seed_memory_run(
        db_session,
        account_id=account,
        conversation_id=conversation,
        profile_id=profile,
        config_id=config,
        credential_version_id=credential,
    )
    await db_session.execute(
        update(s.model_runs).where(s.model_runs.c.id == run).values(provider_request_id="private")
    )
    attempt = await db_session.scalar(
        insert(s.model_run_attempts)
        .values(
            model_run_id=run,
            attempt_no=1,
            state="started",
            provider_request_id="private",
            started_at=NOW,
        )
        .returning(s.model_run_attempts.c.id)
    )
    for table, values in (
        (s.conversation_mode_history, {"conversation_id": conversation, "mode_version": 1}),
        (s.account_control_history, {"control_version": 1}),
    ):
        await db_session.execute(
            insert(table).values(
                account_id=account,
                **values,
                change_kind="synthetic",
                previous_state="HUMAN",
                new_state="AUTO",
                reason="private reason",
                actor_type="admin",
                actor_ref="private",
            )
        )
    await db_session.execute(
        insert(s.account_orchestrator_states).values(account_id=account, updated_by="private")
    )
    draft, edit, command = uuid7(), uuid7(), uuid7()
    await db_session.execute(
        insert(s.copilot_drafts).values(
            id=draft,
            account_id=account,
            contact_id=contact,
            conversation_id=conversation,
            turn_id=turn,
            draft_kind="reactive",
            state="requested",
            account_control_version_snapshot=1,
            mode_version_snapshot=1,
            content_revision_snapshot=0,
            requested_by="private",
        )
    )
    await db_session.execute(
        insert(s.copilot_edit_sessions).values(
            id=edit,
            account_id=account,
            conversation_id=conversation,
            draft_id=draft,
            admin_telegram_user_id=1,
            bot_chat_id=1,
            force_reply_message_id=1,
            expires_at=NOW + timedelta(minutes=5),
        )
    )
    await db_session.execute(
        insert(s.control_commands).values(
            id=command,
            account_id=account,
            conversation_id=conversation,
            bot_identity=str(command),
            telegram_update_id=1,
            admin_telegram_user_id=1,
            bot_chat_id=1,
            command_kind="synthetic",
            idempotency_key=command.bytes * 2,
            state="pending",
        )
    )
    request = await _request(db_session, account, conversation, scope="account")
    await _advance(db_session, account, request)
    for table, column in (
        (s.account_orchestrator_states, "updated_by"),
        (s.conversation_mode_history, "reason"),
        (s.account_control_history, "reason"),
        (s.copilot_drafts, "requested_by"),
    ):
        row = (await db_session.execute(select(table).where(table.c.account_id == account))).one()
        assert row._mapping[column] is None
        assert row.metadata_erased_at is not None
    assert (
        await db_session.scalar(
            select(s.copilot_edit_sessions.c.id).where(s.copilot_edit_sessions.c.id == edit)
        )
        is None
    )
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    await db_session.execute(
        update(s.model_run_attempts)
        .where(s.model_run_attempts.c.id == attempt)
        .values(
            state="succeeded",
            provider_request_id="late-private",
            completed_at=NOW,
            input_tokens=10,
            output_tokens=20,
        )
    )
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    await db_session.execute(
        update(s.control_commands)
        .where(s.control_commands.c.id == command)
        .values(
            state="rejected",
            result_code="ERASURE_SCOPE",
            result_changed=False,
            completed_at=NOW,
            result_payload={"private": "late"},
        )
    )
    await db_session.execute(text("RESET ROLE"))
    row = (
        await db_session.execute(
            select(s.model_run_attempts).where(s.model_run_attempts.c.id == attempt)
        )
    ).one()
    assert row.provider_request_id is None
    assert row.state == "succeeded"
    assert row.input_tokens == 10
    assert row.output_tokens == 20
    row = (
        await db_session.execute(
            select(s.control_commands).where(s.control_commands.c.id == command)
        )
    ).one()
    assert row.state == "rejected"
    assert row.result_payload is None


@pytest.mark.integration
async def test_metadata_redactor_requires_worker_and_quiesced_scope(
    db_session: AsyncSession,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    request = await _request(db_session, account, conversation)
    for role in ("app", "control", "worker"):
        await db_session.execute(text(f"SET LOCAL ROLE telegram_userbot_{role}_runtime"))
        with pytest.raises(DBAPIError) as caught:
            async with db_session.begin_nested():
                await db_session.execute(
                    text("SELECT public.redact_scope_metadata(:request, :now)"),
                    {"request": request, "now": NOW},
                )
        if role == "worker":
            assert "ERASURE_METADATA_QUIESCENCE_REQUIRED" in str(caught.value)
        else:
            assert getattr(caught.value.orig, "sqlstate", None) == "42501"
        await db_session.execute(text("RESET ROLE"))


@pytest.mark.integration
async def test_inflight_profile_write_commits_before_intent_then_is_erased(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    async with sessions() as session, session.begin():
        account, conversation, turn, revision = await seed_scope(session)
        data = await _private_metadata(session, account, conversation, turn, revision)
    started = asyncio.Event()

    async def erase() -> None:
        async with sessions() as session, session.begin():
            started.set()
            request = await _request(session, account, conversation)
            await _advance(session, account, request)

    task: asyncio.Task[None] | None = None
    try:
        async with sessions() as writer, writer.begin():
            await writer.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
            await writer.execute(
                update(s.account_peers)
                .where(s.account_peers.c.id == data["peer"])
                .values(display_name="inflight-private")
            )
            task = asyncio.create_task(erase())
            await started.wait()
        async with asyncio.timeout(10):
            await task
        async with sessions() as reader:
            assert (
                await reader.scalar(
                    select(s.account_peers.c.display_name).where(
                        s.account_peers.c.id == data["peer"]
                    )
                )
                is None
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
