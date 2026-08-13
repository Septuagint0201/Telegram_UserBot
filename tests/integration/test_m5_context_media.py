from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from telegram_userbot.adapters.persistence.context_repository import ContextRepository
from telegram_userbot.adapters.persistence.schema import (
    account_peers,
    accounts,
    contacts,
    context_manifests,
    context_policies,
    context_policy_versions,
    conversation_turns,
    conversations,
    message_events,
    message_revisions,
    messages,
    retrieval_policies,
    retrieval_policy_versions,
    telegram_peers,
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

NOW = datetime(2030, 6, 1, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_scope(session: AsyncSession) -> tuple[UUID, UUID, UUID, UUID]:
    account_id, peer_id, account_peer_id = uuid7(), uuid7(), uuid7()
    contact_id, conversation_id, turn_id = uuid7(), uuid7(), uuid7()
    await session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="m5-synthetic-owner",
            status="active",
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
    await session.execute(
        insert(conversation_turns).values(
            id=turn_id,
            account_id=account_id,
            conversation_id=conversation_id,
            state="completed",
            trigger_kind="incoming",
            collection_sequence=1,
        )
    )
    event_id = await session.scalar(
        insert(message_events)
        .values(
            event_uuid=uuid7(),
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
    message_id, revision_id = uuid7(), uuid7()
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
            text_content="SYNTHETIC_PRIVATE_CONTEXT_BODY",
            entities_schema_version=1,
            entities=[],
            content_sha256=b"h" * 32,
            source_event_id=event_id,
        )
    )
    return account_id, conversation_id, turn_id, revision_id


async def seed_policies(session: AsyncSession) -> tuple[UUID, UUID]:
    context_policy_id, context_version_id = uuid7(), uuid7()
    retrieval_policy_id, retrieval_version_id = uuid7(), uuid7()
    await session.execute(
        insert(context_policies).values(
            id=context_policy_id,
            logical_role="main_ai",
            purpose="reactive_reply",
            version=1,
        )
    )
    await session.execute(
        insert(context_policy_versions).values(
            id=context_version_id,
            policy_id=context_policy_id,
            version_no=1,
            status="active",
            max_input_tokens=24_000,
            safety_reserve_basis_points=500,
            minimum_safety_reserve_tokens=1_024,
            current_budget_basis_points=2_000,
            recent_budget_basis_points=3_000,
            profile_budget_basis_points=1_500,
            structured_budget_basis_points=1_500,
            semantic_budget_basis_points=1_000,
            summary_budget_basis_points=1_000,
            structured_limit=12,
            semantic_limit=8,
            ann_candidate_limit=64,
            current_image_limit=10,
            fallback_auto_image_tokens=2_048,
            token_estimator_policy="utf8_bytes_v1",  # noqa: S106 - not a credential
            activated_at=NOW,
        )
    )
    await session.execute(
        context_policies.update()
        .where(context_policies.c.id == context_policy_id)
        .values(active_version_id=context_version_id)
    )
    await session.execute(
        insert(retrieval_policies).values(id=retrieval_policy_id, policy_name="main-v1", version=1)
    )
    await session.execute(
        insert(retrieval_policy_versions).values(
            id=retrieval_version_id,
            policy_id=retrieval_policy_id,
            version_no=1,
            status="active",
            structured_weights={},
            semantic_weights={},
            half_life_schema_version=1,
            half_life_policy={},
            tie_break_version="stable-v1",
            source_default_schema_version=1,
            source_defaults={},
            content_sha256=b"r" * 32,
            activated_at=NOW,
        )
    )
    await session.execute(
        retrieval_policies.update()
        .where(retrieval_policies.c.id == retrieval_policy_id)
        .values(active_version_id=retrieval_version_id)
    )
    return context_version_id, retrieval_version_id


@pytest.mark.integration
async def test_m5_manifest_persists_content_free_and_preview_is_one_time(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, turn_id, revision_id = await seed_scope(db_session)
    context_version_id, retrieval_version_id = await seed_policies(db_session)
    source = ContextSource(
        Candidate(revision_id, "revision-1", "current:1", ContextLayer.CURRENT, NOW, 30),
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue("SYNTHETIC_PRIVATE_CONTEXT_BODY"),
        "message_revision",
    )
    budget = calculate_budget(
        ContextPolicy("context-v1"), ContextCapabilities(32_000, 2_000, False)
    )
    built = build_context(
        manifest_id=uuid7(),
        purpose="reactive_reply",
        logical_role="main_ai",
        sources=(source,),
        budget=budget,
        builder_version="context-builder-v1",
        prompt_version="prompt-v1",
        prompt_bundle_sha256=(b"p" * 32).hex(),
        context_policy_version="context-v1",
        retrieval_policy_version="retrieval-v1",
        capability_snapshot_sha256=(b"c" * 32).hex(),
    )
    repository = ContextRepository(db_session)
    with pytest.raises(ValueError, match="context_prompt_snapshot_mismatch"):
        await repository.save_manifest(
            account_id=account_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            background_job_id=None,
            context_policy_version_id=context_version_id,
            retrieval_policy_version_id=retrieval_version_id,
            prompt_bundle_sha256=b"x" * 32,
            capability_snapshot_sha256=b"c" * 32,
            manifest=built.manifest,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="context_capability_snapshot_mismatch"):
        await repository.save_manifest(
            account_id=account_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            background_job_id=None,
            context_policy_version_id=context_version_id,
            retrieval_policy_version_id=retrieval_version_id,
            prompt_bundle_sha256=b"p" * 32,
            capability_snapshot_sha256=b"x" * 32,
            manifest=built.manifest,
            created_at=NOW,
        )
    await repository.save_manifest(
        account_id=account_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        background_job_id=None,
        context_policy_version_id=context_version_id,
        retrieval_policy_version_id=retrieval_version_id,
        prompt_bundle_sha256=b"p" * 32,
        capability_snapshot_sha256=b"c" * 32,
        manifest=built.manifest,
        created_at=NOW,
    )
    summary = await repository.latest_summary(
        account_id=account_id, conversation_id=conversation_id
    )
    assert summary is not None
    assert summary.input_token_estimate == built.manifest.input_token_estimate
    persisted = await db_session.scalar(
        select(context_manifests.c.manifest_sha256).where(
            context_manifests.c.id == built.manifest.id
        )
    )
    assert persisted == bytes.fromhex(built.manifest.manifest_sha256)
    raw_database = await db_session.scalar(
        text(
            "SELECT string_agg(value, ' ') FROM ("
            "SELECT row_to_json(t)::text AS value FROM context_manifests t "
            "UNION ALL SELECT row_to_json(t)::text FROM context_manifest_items t) q"
        )
    )
    assert "SYNTHETIC_PRIVATE_CONTEXT_BODY" not in str(raw_database)

    challenge = await repository.issue_preview(
        account_id=account_id,
        conversation_id=conversation_id,
        manifest_id=built.manifest.id,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    first = await repository.consume_preview(
        token=challenge.confirmation_token,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    second = await repository.consume_preview(
        token=challenge.confirmation_token,
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    assert first is not None
    assert second is None
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT * FROM public.context_preview_sources("
                    ":request_id, 42, 42, 'control-bot')"
                ),
                {"request_id": challenge.request_id},
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["source_eligible"] is True
    assert rows[0]["source_content"] == "SYNTHETIC_PRIVATE_CONTEXT_BODY"

    await db_session.execute(
        message_revisions.update()
        .where(message_revisions.c.id == revision_id)
        .values(
            text_content=None,
            entities=None,
            content_sha256=None,
            redacted_at=NOW,
            redaction_reason="telegram_delete",
        )
    )
    redacted = (
        (
            await db_session.execute(
                text(
                    "SELECT * FROM public.context_preview_sources("
                    ":request_id, 42, 42, 'control-bot')"
                ),
                {"request_id": challenge.request_id},
            )
        )
        .mappings()
        .one()
    )
    assert redacted["source_eligible"] is False
    assert redacted["source_content"] is None


@pytest.mark.integration
async def test_m5_constraints_and_control_role_fail_closed(db_session: AsyncSession) -> None:
    async with db_session.begin_nested():
        with pytest.raises(DBAPIError):
            await db_session.execute(
                insert(context_policy_versions).values(
                    id=uuid7(),
                    policy_id=uuid7(),
                    version_no=1,
                    status="active",
                    max_input_tokens=24_000,
                    safety_reserve_basis_points=500,
                    minimum_safety_reserve_tokens=1_024,
                    current_budget_basis_points=1,
                    recent_budget_basis_points=1,
                    profile_budget_basis_points=1,
                    structured_budget_basis_points=1,
                    semantic_budget_basis_points=1,
                    summary_budget_basis_points=1,
                    structured_limit=12,
                    semantic_limit=8,
                    ann_candidate_limit=64,
                    current_image_limit=10,
                    fallback_auto_image_tokens=2_048,
                    token_estimator_policy="utf8_bytes_v1",  # noqa: S106 - not a credential
                )
            )
    privilege = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_control_runtime', "
            "'message_revisions', 'SELECT')"
        )
    )
    assert privilege is False
    preview_privilege = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_control_runtime', "
            "'context_preview_requests', 'INSERT')"
        )
    )
    assert preview_privilege is True
    content_privilege = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_control_runtime', "
            "'message_revisions', 'SELECT')"
        )
    )
    assert content_privilege is False
    execute_privilege = await db_session.scalar(
        text(
            "SELECT has_function_privilege('telegram_userbot_control_runtime', "
            "'public.context_preview_sources(uuid,bigint,bigint,text)', 'EXECUTE')"
        )
    )
    assert execute_privilege is True


@pytest.mark.integration
async def test_m5_control_role_executes_preview_function_without_direct_content_access(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT text_content FROM message_revisions LIMIT 1"))
        finally:
            await transaction.rollback()

    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
            rows = await connection.execute(
                text(
                    "SELECT * FROM public.context_preview_sources("
                    ":request_id, :admin_id, :bot_chat_id, :bot_identity)"
                ),
                {
                    "request_id": uuid7(),
                    "admin_id": 42,
                    "bot_chat_id": 42,
                    "bot_identity": "control-bot",
                },
            )
            assert rows.one_or_none() is None
        finally:
            await transaction.rollback()
