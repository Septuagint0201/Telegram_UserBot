"""Erasure intent must close Bot preview capabilities and preserve deletion evidence."""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    PreviewChallenge,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.persistence.schema import (
    context_policy_versions,
    context_preview_deliveries,
    context_preview_requests,
    context_preview_tokens,
    conversations,
    data_erasure_requests,
    memories,
    retrieval_policy_versions,
)
from telegram_userbot.adapters.telegram_bot.context_control_backend import (
    ExactManifestPreviewRebuilder,
)
from telegram_userbot.domain.context import (
    Candidate,
    ContextCapabilities,
    ContextLayer,
    ContextPolicy,
    ContextSource,
    TrustLevel,
    build_context,
    calculate_budget,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from tests.integration.test_m5_context_media import NOW, seed_policies, seed_scope

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass(frozen=True)
class PreviewScope:
    account_id: UUID
    conversation_id: UUID
    manifest_id: UUID
    challenge: PreviewChallenge


async def _preview(
    session: AsyncSession, *, account_id: UUID | None = None, bot: str = "control-bot"
) -> PreviewScope:
    account_id, conversation_id, turn_id, revision_id = await seed_scope(
        session, existing_account_id=account_id
    )
    context_version = await session.scalar(select(context_policy_versions.c.id).limit(1))
    retrieval_version = await session.scalar(select(retrieval_policy_versions.c.id).limit(1))
    if context_version is None or retrieval_version is None:
        context_version, retrieval_version = await seed_policies(session)
    source = ContextSource(
        Candidate(revision_id, "revision-1", "current:1", ContextLayer.CURRENT, NOW, 30),
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue("SYNTHETIC_PRIVATE_CONTEXT_BODY"),
        "message_revision",
    )
    built = build_context(
        manifest_id=uuid7(),
        purpose="reactive_reply",
        logical_role="main_ai",
        sources=(source,),
        budget=calculate_budget(
            ContextPolicy("context-v1"), ContextCapabilities(32_000, 2_000, False)
        ),
        builder_version="context-builder-v1",
        prompt_version="prompt-v1",
        prompt_bundle_sha256=(b"p" * 32).hex(),
        context_policy_version="context-v1",
        retrieval_policy_version="retrieval-v1",
        capability_snapshot_sha256=(b"c" * 32).hex(),
    )
    repository = ContextRepository(session)
    await repository.save_manifest(
        account_id=account_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        background_job_id=None,
        context_policy_version_id=context_version,
        retrieval_policy_version_id=retrieval_version,
        prompt_bundle_sha256=b"p" * 32,
        capability_snapshot_sha256=b"c" * 32,
        manifest=built.manifest,
        created_at=NOW,
    )
    challenge = await repository.issue_preview(
        account_id=account_id,
        conversation_id=conversation_id,
        manifest_id=built.manifest.id,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity=bot,
        now=NOW,
    )
    return PreviewScope(account_id, conversation_id, built.manifest.id, challenge)


async def _confirm(
    repository: ContextRepository, challenge: PreviewChallenge
) -> PreviewRequestRecord:
    request = await repository.consume_preview(
        token=challenge.confirmation_token,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    assert request is not None
    return request


async def _erase(
    session: AsyncSession, scope: PreviewScope, *, kind: str = "contact", state: str = "requested"
) -> None:
    contact_id = await session.scalar(
        select(conversations.c.contact_id).where(conversations.c.id == scope.conversation_id)
    )
    memory_id = None
    if kind == "memory":
        memory_id = uuid7()
        await session.execute(
            insert(memories).values(
                id=memory_id,
                account_id=scope.account_id,
                contact_id=contact_id,
                memory_type="fact",
                semantic_key_hash=b"m" * 32,
                status="active",
                current_version_no=1,
            )
        )
    request_id = uuid7()
    await session.execute(
        insert(data_erasure_requests).values(
            id=request_id,
            account_id=scope.account_id,
            scope_type=kind,
            contact_id=contact_id if kind == "contact" else None,
            memory_id=memory_id,
            state=state,
            requested_by="synthetic-preview-erasure",
            request_idempotency_key=hashlib.sha256(request_id.bytes).digest(),
            policy_version=1,
        )
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["contact", "account"])
@pytest.mark.parametrize("state", ["requested", "failed"])
async def test_erasure_blocks_preview_before_sweep_with_account_and_bot_isolation(
    db_session: AsyncSession, kind: str, state: str
) -> None:
    target = await _preview(db_session)
    sibling = await _preview(db_session, account_id=target.account_id)
    other = await _preview(db_session)
    other_bot = await _preview(db_session, account_id=target.account_id, bot="other-bot")
    repository = ContextRepository(db_session)
    confirmed = await _confirm(repository, target.challenge)
    pending = await repository.issue_preview(
        account_id=target.account_id,
        conversation_id=target.conversation_id,
        manifest_id=target.manifest_id,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    await _erase(db_session, target, kind=kind, state=state)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
    assert (
        await repository.consume_preview(
            token=pending.confirmation_token,
            admin_user_id=42,
            bot_chat_id=42,
            bot_identity="control-bot",
            now=NOW,
        )
        is None
    )
    with pytest.raises(ValueError, match="context_preview_not_allowed"):
        await repository.issue_preview(
            account_id=target.account_id,
            conversation_id=target.conversation_id,
            manifest_id=target.manifest_id,
            admin_user_id=42,
            bot_chat_id=42,
            bot_identity="control-bot",
            now=NOW,
        )
    assert (
        await repository.begin_preview_delivery(
            request=confirmed, chunk_count=1, now=NOW, delete_after=NOW + timedelta(minutes=10)
        )
        is None
    )
    with pytest.raises(ValueError, match="context_source_unavailable"):
        await ExactManifestPreviewRebuilder(db_session).rebuild_redacted(request=confirmed)
    for _ in range(3 if kind == "account" else 2):
        assert (
            await repository.reconcile_erasure_previews(
                bot_identity="control-bot", now=NOW, limit=1
            )
            == 1
        )
    assert await repository.reconcile_erasure_previews(bot_identity="control-bot", now=NOW) == 0
    states: dict[UUID, str] = {
        row[0]: row[1]
        for row in (
            await db_session.execute(
                select(context_preview_requests.c.id, context_preview_requests.c.state)
            )
        ).all()
    }
    assert states[target.challenge.request_id] == "cancelled"
    assert states[pending.request_id] == "cancelled"
    assert states[sibling.challenge.request_id] == (
        "cancelled" if kind == "account" else "pending_confirmation"
    )
    assert states[other.challenge.request_id] == "pending_confirmation"
    assert states[other_bot.challenge.request_id] == "pending_confirmation"
    assert (
        await db_session.scalar(
            select(context_preview_tokens.c.used_at).where(
                context_preview_tokens.c.request_id == pending.request_id
            )
        )
        == NOW
    )
    # Scope-only read authority must not grant access to requester metadata or mutation.
    for sql in (
        "SELECT requested_by FROM data_erasure_requests",
        "UPDATE data_erasure_requests SET state = 'completed'",
        "SELECT telegram_chat_id FROM conversations",
        "UPDATE conversations SET deleted_at = now()",
    ):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(text(sql))


@pytest.mark.integration
async def test_memory_erasure_does_not_disable_whole_conversation_preview(
    db_session: AsyncSession,
) -> None:
    target = await _preview(db_session)
    await _erase(db_session, target, kind="memory")
    repository = ContextRepository(db_session)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
    assert await repository.reconcile_erasure_previews(bot_identity="control-bot", now=NOW) == 0
    confirmed = await _confirm(repository, target.challenge)
    assert await ExactManifestPreviewRebuilder(db_session).rebuild_redacted(request=confirmed)


@pytest.mark.integration
async def test_erasure_keeps_late_ids_unknowns_leases_and_delete_backoff(
    db_session: AsyncSession,
) -> None:
    target = await _preview(db_session)
    repository = ContextRepository(db_session)
    request = await _confirm(repository, target.challenge)
    deadline = NOW + timedelta(minutes=10)
    assert await repository.begin_preview_delivery(
        request=request, chunk_count=4, now=NOW, delete_after=deadline
    )
    for ordinal in (1, 2, 3):
        assert await repository.claim_preview_delivery_chunk(request=request, ordinal=ordinal)
    await repository.record_preview_delivery_chunk(
        request=request, ordinal=1, state="sent", message_id=99, now=NOW, delete_after=deadline
    )
    await _erase(db_session, target)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
    assert not await repository.claim_preview_delivery_chunk(request=request, ordinal=4)
    assert (
        await repository.begin_preview_delivery(
            request=request, chunk_count=4, now=NOW, delete_after=deadline
        )
        is None
    )
    # Known IDs become due immediately, before the bounded cancellation scan.
    first = await repository.due_preview_deletions(bot_identity="control-bot", now=NOW)
    assert len(first) == 1
    assert first[0].bot_message_id == 99
    assert first[0].delete_after == NOW
    assert await repository.reconcile_erasure_previews(bot_identity="control-bot", now=NOW) == 1
    await repository.record_preview_delivery_chunk(
        request=request, ordinal=2, state="sent", message_id=100, now=NOW, delete_after=deadline
    )
    await repository.record_preview_delivery_chunk(
        request=request,
        ordinal=3,
        state="send_unknown",
        message_id=None,
        now=NOW,
        delete_after=deadline,
    )
    assert not await repository.due_preview_deletions(bot_identity="other-bot", now=NOW)
    late = await repository.due_preview_deletions(bot_identity="control-bot", now=NOW)
    assert len(late) == 1
    assert late[0].bot_message_id == 100
    assert late[0].delete_after == NOW
    assert not await repository.due_preview_deletions(bot_identity="control-bot", now=NOW)
    await repository.finish_preview_deletion(deletion=first[0], deleted=False, now=NOW)
    await repository.finish_preview_deletion(deletion=late[0], deleted=True, now=NOW)
    assert not await repository.due_preview_deletions(
        bot_identity="control-bot", now=NOW + timedelta(seconds=59)
    )
    retry = await repository.due_preview_deletions(
        bot_identity="control-bot", now=NOW + timedelta(minutes=1)
    )
    assert len(retry) == 1
    assert retry[0].attempt_count == 2
    await repository.finish_preview_deletion(
        deletion=retry[0], deleted=True, now=NOW + timedelta(minutes=1)
    )
    states = tuple(
        await db_session.scalars(
            select(context_preview_deliveries.c.state)
            .where(context_preview_deliveries.c.request_id == request.request_id)
            .order_by(context_preview_deliveries.c.ordinal)
        )
    )
    assert states == ("deleted", "deleted", "send_unknown", "pending")
    parent = (
        await db_session.execute(
            select(
                context_preview_requests.c.state, context_preview_requests.c.last_error_code
            ).where(context_preview_requests.c.id == request.request_id)
        )
    ).one()
    assert parent == ("cancelled", "erasure_scope")
    assert await repository.reconcile_erasure_previews(bot_identity="control-bot", now=NOW) == 0
