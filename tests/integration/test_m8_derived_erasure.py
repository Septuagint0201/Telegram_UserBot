"""Real-role derived erasure, dependency, snapshot and late-write regressions."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.domain.memory.models import SummaryKind, SummarySource, SummaryVersion
from tests.integration.test_m5_context_media import NOW, seed_scope
from tests.integration.test_m6_account_scope_constraints import _seed_memory_run, _seed_profile
from tests.integration.test_m7_proactive_pipeline import seed_budget_binding
from tests.integration.test_m8_scope_erasure import _advance, _request

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _memory(
    session: AsyncSession, account: UUID, conversation: UUID, revision: UUID
) -> tuple[UUID, UUID]:
    memory, version = uuid7(), uuid7()
    await session.execute(
        insert(s.memories).values(
            id=memory,
            account_id=account,
            memory_type="fact",
            semantic_key_hash=memory.bytes * 2,
            status="active",
            current_version_no=1,
            contact_id=select(s.conversations.c.contact_id)
            .where(s.conversations.c.id == conversation)
            .scalar_subquery(),
            conversation_id=conversation,
        )
    )
    await session.execute(
        insert(s.memory_versions).values(
            id=version,
            account_id=account,
            memory_id=memory,
            version_no=1,
            operation="create",
            payload_schema_version=1,
            payload={"value": "synthetic"},
            rendered_text="synthetic",
            importance=0.5,
            confidence=0.9,
            time_precision="unknown",
            validator_policy_version="synthetic",
            acceptance_kind="automatic",
        )
    )
    await session.execute(
        insert(s.memory_evidence).values(
            account_id=account,
            memory_version_id=version,
            message_revision_id=revision,
            evidence_role="primary",
            trust_class="user_statement",
            source_content_sha256=b"e" * 32,
        )
    )
    return memory, version


async def _summary(
    session: AsyncSession, account: UUID, conversation: UUID, revision: UUID
) -> UUID:
    version = uuid7()
    source_hash = await session.scalar(
        select(s.message_revisions.c.content_sha256).where(s.message_revisions.c.id == revision)
    )
    assert isinstance(source_hash, bytes)
    await MemoryRepository(session).publish_summary(
        SummaryVersion(
            id=version,
            summary_id=uuid7(),
            version_no=1,
            kind=SummaryKind.ROLLING,
            range_start_event_id=1,
            range_end_event_id=1,
            content_text="PRIVATE_SUMMARY",
            sources=(SummarySource(revision, "message_revision", source_hash, 1),),
            manifest_sha256=b"s" * 32,
        ),
        account_id=account,
        conversation_id=conversation,
        now=NOW,
    )
    return version


async def _outbound(
    session: AsyncSession, account: UUID, conversation: UUID, turn: UUID, state: str
) -> UUID:
    group, intent = uuid7(), uuid7()
    await session.execute(
        insert(s.outbound_delivery_groups).values(
            id=group,
            account_id=account,
            conversation_id=conversation,
            source="system",
            state="unknown" if state == "unknown" else "planned",
            intent_count=1,
            idempotency_key=group.bytes * 2,
            logical_content_sha256=b"g" * 32,
            mode_version=1,
            content_revision=0,
        )
    )
    await session.execute(
        insert(s.outbound_intents).values(
            id=intent,
            delivery_group_id=group,
            account_id=account,
            conversation_id=conversation,
            idempotency_key=intent.bytes * 2,
            sequence_no=0,
            telegram_random_id=intent.int % (2**63 - 1),
            text_content="PRIVATE_OUTBOUND",
            payload_sha256=b"o" * 32,
            state=state,
            unknown_since=NOW if state == "unknown" else None,
        )
    )
    return intent


@pytest.mark.parametrize("scope", ["contact", "account"])
@pytest.mark.integration
async def test_derived_bodies_vectors_and_outcomes_are_erased_in_scope(
    db_session: AsyncSession, scope: str
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    _, sibling, sibling_turn, sibling_revision = await seed_scope(
        db_session, existing_account_id=account
    )
    other, other_conversation, _, other_revision = await seed_scope(db_session)
    memory, version = await _memory(db_session, account, conversation, revision)
    _, sibling_version = await _memory(db_session, account, sibling, sibling_revision)
    _, other_version = await _memory(db_session, other, other_conversation, other_revision)
    summary = await _summary(db_session, account, conversation, revision)
    pending = await _outbound(db_session, account, conversation, turn, "pending")
    unknown = await _outbound(db_session, account, conversation, turn, "unknown")
    sibling_outbound = await _outbound(db_session, account, sibling, sibling_turn, "pending")
    profile, config, _, _ = await _seed_profile(
        db_session, logical_role="embedding", profile_kind="embedding", protocol="embedding"
    )
    space = uuid7()
    await db_session.execute(
        insert(s.embedding_spaces).values(
            id=space,
            account_id=account,
            model_profile_id=profile,
            config_version_id=config,
            model_name_snapshot="synthetic",
            dimensions=2,
            distance_metric="cosine",
            normalization="l2",
            chunker_version="synthetic",
            generation=1,
            state="building",
        )
    )
    for source in (version, sibling_version):
        await db_session.execute(
            insert(s.embedding_records).values(
                id=uuid7(),
                account_id=account,
                embedding_space_id=space,
                memory_version_id=source,
                chunk_index=0,
                chunker_version="synthetic",
                source_sha256=b"v" * 32,
                vector_payload=[0.5, 0.5],
                dimensions=2,
                state="ready",
            )
        )
    request = await _request(db_session, account, conversation, scope=scope)
    await _advance(db_session, account, request)
    await _advance(db_session, account, request)
    bodies = {r.id: r for r in (await db_session.execute(select(s.memory_versions))).mappings()}
    assert bodies[version].payload == {}
    assert bodies[version].rendered_text is None
    assert bodies[version].redacted_at == bodies[version].scope_erased_at == NOW
    assert (bodies[sibling_version].rendered_text is None) == (scope == "account")
    assert bodies[other_version].rendered_text == "synthetic"
    assert (
        await db_session.scalar(
            select(s.memories.c.semantic_key_hash).where(s.memories.c.id == memory)
        )
        is None
    )
    assert (
        await db_session.execute(
            select(
                s.summary_versions.c.content_text,
                s.summary_versions.c.content_sha256,
                s.summary_versions.c.manifest_sha256,
            ).where(s.summary_versions.c.id == summary)
        )
    ).one() == (None, None, None)
    vectors = set(
        await db_session.scalars(
            select(s.embedding_records.c.memory_version_id).where(
                s.embedding_records.c.account_id == account
            )
        )
    )
    assert vectors == ({sibling_version} if scope == "contact" else set())
    for intent, expected_state in ((pending, "cancelled"), (unknown, "unknown")):
        row = (
            (
                await db_session.execute(
                    select(s.outbound_intents).where(s.outbound_intents.c.id == intent)
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == expected_state
        assert row["text_content"] is None
        assert row["payload_sha256"] is None
        assert row["telegram_random_id"] > 0
    assert (
        await db_session.scalar(
            select(s.outbound_intents.c.text_content).where(
                s.outbound_intents.c.id == sibling_outbound
            )
        )
        is None
    ) == (scope == "account")
    assert (
        await db_session.scalar(
            select(s.erasure_progress.c.state).where(
                s.erasure_progress.c.request_id == request,
                s.erasure_progress.c.step_name == "derived_redaction",
            )
        )
        == "completed"
    )
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.completed_at).where(
                s.data_erasure_requests.c.id == request
            )
        )
        is None
    )


@pytest.mark.integration
async def test_dependency_chain_is_erased_but_unrelated_contact_survives(
    db_session: AsyncSession,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, sibling, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    _, source = await _memory(db_session, account, conversation, revision)
    _, dependent = await _memory(db_session, account, sibling, sibling_revision)
    # The dependent memory has both independent and erased evidence.
    await db_session.execute(
        insert(s.memory_evidence).values(
            account_id=account,
            memory_version_id=dependent,
            other_memory_version_id=source,
            evidence_role="supporting",
            trust_class="trusted_derived",
            source_content_sha256=b"d" * 32,
        )
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.memory_versions.c.rendered_text).where(s.memory_versions.c.id == dependent)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(s.message_revisions.c.text_content).where(
                s.message_revisions.c.id == sibling_revision
            )
        )
        == "SYNTHETIC_PRIVATE_CONTEXT_BODY"
    )
    assert set(
        await db_session.scalars(
            select(s.memory_evidence.c.source_content_sha256).where(
                s.memory_evidence.c.account_id == account
            )
        )
    ) == {None}
    # Rebuilding a payload or inserting a new version under the erased identity fails.
    for statement in (
        update(s.memory_versions)
        .where(s.memory_versions.c.id == dependent)
        .values(rendered_text="RESURRECT"),
        update(s.memory_versions)
        .where(s.memory_versions.c.id == dependent)
        .values(scope_erased_at=None),
    ):
        with pytest.raises(DBAPIError, match="ERASURE_PAYLOAD_IMMUTABLE"):
            async with db_session.begin_nested():
                await db_session.execute(statement)


@pytest.mark.integration
async def test_durable_intent_blocks_late_writes_before_the_worker_runs(
    db_session: AsyncSession,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, version = await _memory(db_session, account, conversation, revision)
    await _request(db_session, account, conversation)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    with pytest.raises(DBAPIError, match="ERASURE_SCOPE_WRITE_BLOCKED"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.memory_versions)
                .where(s.memory_versions.c.id == version)
                .values(rendered_text="LATE_BODY")
            )
    await db_session.execute(text("RESET ROLE"))


@pytest.mark.integration
async def test_inflight_derived_write_commits_before_erasure_and_is_removed(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as seed, seed.begin():
        account, conversation, _, revision = await seed_scope(seed)
        _, version = await _memory(seed, account, conversation, revision)
    task = None
    started = asyncio.Event()

    async def erase() -> None:
        async with sessions() as worker, worker.begin():
            started.set()
            request = await _request(worker, account, conversation)
            await _advance(worker, account, request)

    try:
        async with sessions() as writer, writer.begin():
            await writer.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
            await writer.execute(
                update(s.memory_versions)
                .where(s.memory_versions.c.id == version)
                .values(rendered_text="IN_FLIGHT_BODY")
            )
            task = asyncio.create_task(erase())
            await started.wait()
        async with asyncio.timeout(10):
            await task
        async with sessions() as reader:
            assert (
                await reader.scalar(
                    select(s.memory_versions.c.rendered_text).where(
                        s.memory_versions.c.id == version
                    )
                )
                is None
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.integration
async def test_immutable_snapshots_proposals_and_copilot_have_one_way_erasure(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, revision = await seed_scope(db_session)
    contact = await db_session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
    )
    assert isinstance(contact, UUID)
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
    job, memory_manifest = (
        await db_session.execute(
            select(s.model_runs.c.memory_job_id, s.model_runs.c.memory_input_manifest_id).where(
                s.model_runs.c.id == run
            )
        )
    ).one()
    await db_session.execute(
        insert(s.memory_input_manifest_items).values(
            manifest_id=memory_manifest,
            account_id=account,
            ordinal=1,
            source_type="message_revision",
            message_revision_id=revision,
            inclusion_role="episode",
            trust_class="user_statement",
            source_content_sha256=b"h" * 32,
            selection_reason_code="synthetic",
        )
    )
    await db_session.execute(
        update(s.memory_jobs)
        .where(s.memory_jobs.c.id == job)
        .values(input_manifest_id=memory_manifest, sealed_at=NOW)
    )
    proposal = uuid7()
    await db_session.execute(
        insert(s.memory_proposals).values(
            id=proposal,
            account_id=account,
            contact_id=contact,
            conversation_id=conversation,
            memory_job_id=job,
            model_run_id=run,
            idempotency_key=proposal.bytes * 2,
            proposal_ordinal=1,
            operation="create",
            memory_type="fact",
            semantic_key_hash=b"k" * 32,
            payload_schema_version=1,
            proposed_payload={"value": "PRIVATE_PROPOSAL"},
            proposed_text="PRIVATE_PROPOSAL",
            proposed_confidence=0.9,
            proposed_importance=0.5,
            state="candidate",
            validator_policy_version="synthetic",
            retention_class="synthetic",
        )
    )
    await db_session.execute(
        insert(s.memory_proposal_evidence).values(
            proposal_id=proposal,
            account_id=account,
            message_revision_id=revision,
            evidence_role="primary",
            source_content_sha256=b"h" * 32,
            source_normalization_version="synthetic",
            trust_class="user_statement",
        )
    )
    draft = uuid7()
    await db_session.execute(
        insert(s.copilot_drafts).values(
            id=draft,
            account_id=account,
            contact_id=contact,
            conversation_id=conversation,
            turn_id=turn,
            draft_kind="reactive",
            state="ready",
            current_revision_no=1,
            account_control_version_snapshot=1,
            mode_version_snapshot=1,
            content_revision_snapshot=0,
            requested_by="synthetic",
        )
    )
    await db_session.execute(
        insert(s.copilot_draft_revisions).values(
            id=uuid7(),
            account_id=account,
            conversation_id=conversation,
            draft_id=draft,
            revision_no=1,
            author_type="model",
            content_text="PRIVATE_DRAFT",
            content_sha256=b"d" * 32,
        )
    )
    candidate, decision, policy = await seed_budget_binding(
        db_session, account, contact, conversation
    )
    proactive_job, manifest, occurrence, prompt = uuid7(), uuid7(), uuid7(), uuid7()
    await db_session.execute(
        insert(s.proactive_jobs).values(
            id=proactive_job,
            account_id=account,
            conversation_id=conversation,
            candidate_id=candidate,
            job_kind="candidate_due",
            idempotency_key=proactive_job.bytes * 2,
            available_at=NOW,
        )
    )
    await db_session.execute(
        insert(s.prompt_versions).values(
            id=prompt,
            logical_role="main_ai",
            version_no=91,
            template_sha256=b"p" * 32,
            template_body="synthetic",
            created_by_admin_id=42,
        )
    )
    await db_session.execute(
        insert(s.proactive_occurrences).values(
            id=occurrence,
            account_id=account,
            contact_id=contact,
            conversation_id=conversation,
            occurrence_key=occurrence.bytes * 2,
            generation=1,
            reason="promise_due",
            state="eligible",
            window_start_at=NOW,
            window_end_at=NOW + timedelta(hours=1),
            hard_deadline_at=NOW + timedelta(hours=1),
            timezone_name="UTC",
            local_date=NOW.date(),
            importance=0.5,
            source_type="message_revision",
            source_id=revision,
            source_version="1",
            quiet_bypass_possible=False,
        )
    )
    await db_session.execute(
        insert(s.proactive_occurrence_evidence).values(
            occurrence_id=occurrence,
            account_id=account,
            ordinal=1,
            source_type="message_revision",
            source_id=revision,
            source_version="1",
            source_hash=b"e" * 32,
            summary="PRIVATE_EVIDENCE",
            current=True,
            active=True,
            explicit=True,
        )
    )
    await db_session.execute(
        insert(s.proactive_input_manifests).values(
            id=manifest,
            account_id=account,
            conversation_id=conversation,
            proactive_job_id=proactive_job,
            candidate_id=candidate,
            proactive_decision_id=decision,
            logical_role="main_ai",
            purpose="proactive_final",
            job_fencing_token=1,
            candidate_generation=1,
            candidate_key=candidate.bytes * 2,
            candidate_membership_hash=b"m" * 32,
            decision_snapshot={"topic": "PRIVATE_SNAPSHOT"},
            decision_snapshot_sha256=b"d" * 32,
            mode_version=1,
            content_revision=0,
            activity_revision=0,
            policy_version_id=policy,
            policy_version_no=1,
            policy_snapshot={},
            policy_snapshot_sha256=b"p" * 32,
            timezone_snapshot="UTC",
            context_contract_version="synthetic",
            prompt_version_id=prompt,
            prompt_version="synthetic",
            prompt_bundle_sha256=b"p" * 32,
            model_config_version_id=config,
            credential_version_id=credential,
            capability_snapshot_sha256=b"c" * 32,
            input_schema_version=1,
            output_schema_version=2,
            input_token_estimate=10,
            occurrence_count=1,
        )
    )
    await db_session.execute(
        insert(s.proactive_input_manifest_items).values(
            manifest_id=manifest,
            account_id=account,
            ordinal=1,
            occurrence_id=occurrence,
            occurrence_ordinal=1,
            occurrence_generation=1,
            occurrence_key=occurrence.bytes * 2,
            evidence_ordinal=1,
            source_type="message_revision",
            source_id=revision,
            source_version="1",
            source_hash=b"e" * 32,
            summary="PRIVATE_SNAPSHOT_ITEM",
        )
    )
    await db_session.execute(
        update(s.proactive_input_manifests)
        .where(s.proactive_input_manifests.c.id == manifest)
        .values(manifest_sha256=b"m" * 32, sealed_at=NOW)
    )
    with pytest.raises(DBAPIError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.proactive_input_manifests)
                .where(s.proactive_input_manifests.c.id == manifest)
                .values(decision_snapshot={"topic": "ILLEGAL"})
            )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    await _advance(db_session, account, request)
    for table, column in (
        (s.memory_proposals, "proposed_text"),
        (s.copilot_draft_revisions, "content_text"),
        (s.proactive_input_manifest_items, "summary"),
        (s.proactive_occurrence_evidence, "summary"),
        (s.proactive_input_manifests, "decision_snapshot"),
        (s.memory_input_manifest_items, "source_content_sha256"),
    ):
        assert set(
            await db_session.scalars(select(table.c[column]).where(table.c.account_id == account))
        ) == {None}
    assert (
        await db_session.scalar(
            select(s.proactive_input_manifests.c.sealed_at).where(
                s.proactive_input_manifests.c.id == manifest
            )
        )
        == NOW
    )
    # A marker cannot be used to smuggle unrelated identity/provenance changes.
    with pytest.raises(DBAPIError, match=r"immutable|ERASURE"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.proactive_input_manifests)
                .where(s.proactive_input_manifests.c.id == manifest)
                .values(candidate_generation=99)
            )


@pytest.mark.integration
async def test_erased_unknown_delivery_accepts_a_late_exact_receipt(
    db_session: AsyncSession,
) -> None:
    account, conversation, turn, _ = await seed_scope(db_session)
    intent = await _outbound(db_session, account, conversation, turn, "unknown")
    random_id = await db_session.scalar(
        select(s.outbound_intents.c.telegram_random_id).where(s.outbound_intents.c.id == intent)
    )
    assert isinstance(random_id, int)
    await db_session.execute(
        insert(s.outbound_attempts).values(
            intent_id=intent,
            account_id=account,
            attempt_no=1,
            state="unknown",
            started_at=NOW,
            finished_at=NOW,
            error_code="SEND_UNKNOWN",
        )
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    repository = TelegramLifecycleRepository(db_session)
    assert await repository.get_intent(account_id=account, intent_id=intent) is None
    assert (
        await repository.reconcile_outbound_message_id(
            account_id=account,
            telegram_random_id=random_id,
            telegram_message_id=99,
            now=NOW + timedelta(seconds=1),
        )
        == "reconciled"
    )
    row = (
        (
            await db_session.execute(
                select(s.outbound_intents).where(s.outbound_intents.c.id == intent)
            )
        )
        .mappings()
        .one()
    )
    assert row["state"] == "sent"
    assert row["telegram_message_id"] == 99
    assert row["text_content"] is None
    assert row["payload_sha256"] is None
    assert (
        await db_session.scalar(
            select(s.outbound_attempts.c.state).where(s.outbound_attempts.c.intent_id == intent)
        )
        == "unknown"
    )


@pytest.mark.integration
async def test_upgrade_keeps_existing_evidence_and_refuses_lossy_rollback(
    isolated_scope_erasure_session: AsyncSession,
) -> None:
    db_session = isolated_scope_erasure_session
    account, conversation, _, revision = await seed_scope(db_session)
    _, version = await _memory(db_session, account, conversation, revision)
    root = Path(__file__).parents[2]
    connection = await db_session.connection()
    # The normal deploy transaction starts after application writes commit.
    # Flush deferred circular identity FKs before emulating ALTER TABLE here.
    await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    def migrate(sync_connection: object, direction: str) -> None:
        config = Config(str(root / "alembic.ini"))
        config.attributes["connection"] = sync_connection
        if direction == "down":
            command.downgrade(config, "0029_unsupported_peer_events")
        else:
            command.upgrade(config, "head")

    await connection.run_sync(migrate, "down")
    assert (
        await db_session.scalar(
            text("SELECT source_content_sha256 FROM memory_evidence WHERE memory_version_id = :id"),
            {"id": version},
        )
        == b"e" * 32
    )
    await connection.run_sync(migrate, "up")
    await db_session.execute(
        text((root / "deploy/postgres/m8_roles.sql").read_text(encoding="utf-8"))
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    with pytest.raises(RuntimeError, match="MIGRATION_0034_DOWNGRADE_REQUIRES_NO_COMPLETION"):
        async with connection.begin_nested():
            await connection.run_sync(migrate, "down")
    assert (
        await db_session.scalar(text("SELECT version_num FROM alembic_version"))
        == "0036_worker_complete"
    )
    assert (
        await db_session.scalar(
            select(s.memory_versions.c.rendered_text).where(s.memory_versions.c.id == version)
        )
        is None
    )


@pytest.mark.integration
async def test_interrupted_redaction_rolls_back_all_payloads_then_replays(
    db_session: AsyncSession,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    memory, version = await _memory(db_session, account, conversation, revision)
    with pytest.raises(DBAPIError, match="scope_live_payload_required"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.memories).where(s.memories.c.id == memory).values(semantic_key_hash=None)
            )
    request = await _request(db_session, account, conversation)
    with pytest.raises(RuntimeError, match="synthetic_interruption"):  # noqa: PT012 - crash boundary
        async with db_session.begin_nested():
            await _advance(db_session, account, request)
            raise RuntimeError("synthetic_interruption")
    assert (
        await db_session.scalar(
            select(s.memory_versions.c.rendered_text).where(s.memory_versions.c.id == version)
        )
        == "synthetic"
    )
    assert (
        await db_session.scalar(
            select(s.message_revisions.c.text_content).where(s.message_revisions.c.id == revision)
        )
        == "SYNTHETIC_PRIVATE_CONTEXT_BODY"
    )
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.state).where(s.data_erasure_requests.c.id == request)
        )
        == "requested"
    )
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.memory_versions.c.rendered_text).where(s.memory_versions.c.id == version)
        )
        is None
    )
