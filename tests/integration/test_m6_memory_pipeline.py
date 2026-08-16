"""M6 PostgreSQL range, manifest, constraint, and role evidence."""

from datetime import timedelta
from hashlib import sha256
from typing import cast
from uuid import uuid4, uuid7

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.schema import memory_jobs
from telegram_userbot.adapters.telegram_bot.memory_control_backend import (
    DurableMemoryControlBackend,
    MemoryControlTargetTokenCodec,
)
from telegram_userbot.domain.memory.models import InputManifest, InputSource
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.shared.redaction import SensitiveValue
from tests.integration.test_m5_context_media import NOW, seed_scope

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_m6_pending_range_claim_is_immutable_and_later_event_gets_next_generation(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _turn_id, _revision_id = await seed_scope(db_session)
    repository = MemoryRepository(db_session)
    first_id = await repository.refresh_pending_job(
        account_id=account_id,
        conversation_id=conversation_id,
        job_kind="episode",
        event_range=EventRange(1, 2),
        estimated_input_tokens=20,
        now=NOW,
    )
    assert (
        await repository.refresh_pending_job(
            account_id=account_id,
            conversation_id=conversation_id,
            job_kind="episode",
            event_range=EventRange(2, 4),
            estimated_input_tokens=30,
            now=NOW,
        )
        == first_id
    )
    owner = uuid7()
    lease = await repository.claim_next(
        conversation_id=conversation_id,
        owner=owner,
        now=NOW + timedelta(seconds=46),
    )
    assert lease is not None
    assert (lease.range_start_event_id, lease.range_end_event_id) == (1, 4)
    second_id = await repository.refresh_pending_job(
        account_id=account_id,
        conversation_id=conversation_id,
        job_kind="episode",
        event_range=EventRange(5, 5),
        estimated_input_tokens=10,
        now=NOW + timedelta(seconds=47),
    )
    assert second_id != first_id
    generations = tuple(
        await db_session.scalars(
            select(memory_jobs.c.generation)
            .where(memory_jobs.c.conversation_id == conversation_id)
            .order_by(memory_jobs.c.generation)
        )
    )
    assert generations == (1, 2)


@pytest.mark.integration
async def test_m6_manifest_membership_is_content_free_and_hash_bound(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _turn_id, revision_id = await seed_scope(db_session)
    repository = MemoryRepository(db_session)
    job_id = await repository.refresh_pending_job(
        account_id=account_id,
        conversation_id=conversation_id,
        job_kind="episode",
        event_range=EventRange(1, 1),
        estimated_input_tokens=20,
        now=NOW,
    )
    lease = await repository.claim_next(
        conversation_id=conversation_id,
        owner=uuid7(),
        now=NOW + timedelta(seconds=46),
    )
    assert lease is not None
    assert lease.id == job_id
    content = "SYNTHETIC_PRIVATE_CONTEXT_BODY"
    manifest = InputManifest(
        id=uuid4(),
        account_id=account_id,
        conversation_id=conversation_id,
        generation=1,
        range_start_event_id=1,
        range_end_event_id=1,
        sources=(
            InputSource(
                source_id=revision_id,
                revision="revision-1",
                content=content,
                content_sha256=sha256(content.encode()).digest(),
            ),
        ),
        pipeline_version="m6-v1",
        policy_version="policy-v1",
        prompt_version="prompt-v1",
        input_token_estimate=20,
    )
    await repository.create_manifest(manifest, lease=lease, now=NOW + timedelta(seconds=46))
    sealed = (
        await db_session.execute(
            select(memory_jobs.c.input_manifest_id, memory_jobs.c.sealed_at).where(
                memory_jobs.c.id == job_id
            )
        )
    ).one()
    assert sealed.input_manifest_id == manifest.id
    assert sealed.sealed_at == NOW + timedelta(seconds=46)
    assert await repository.complete_job(
        job_id=job_id,
        owner=lease.lease_owner,
        fencing_token=lease.fencing_token,
        now=NOW + timedelta(seconds=47),
        succeeded=True,
    )
    dumped = await db_session.scalar(
        text(
            "SELECT string_agg(value, ' ') FROM ("
            "SELECT row_to_json(t)::text AS value FROM memory_input_manifests t "
            "UNION ALL SELECT row_to_json(t)::text FROM memory_input_manifest_items t) q"
        )
    )
    assert content not in str(dumped)
    assert manifest.manifest_sha256.hex() in str(dumped).replace("\\x", "")


@pytest.mark.integration
async def test_m6_threshold_retry_and_expired_lease_are_runnable(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _turn_id, _revision_id = await seed_scope(db_session)
    repository = MemoryRepository(db_session)
    job_id = await repository.refresh_pending_job(
        account_id=account_id,
        conversation_id=conversation_id,
        job_kind="episode",
        event_range=EventRange(1, 1),
        estimated_input_tokens=6_000,
        now=NOW,
    )
    first_owner = uuid7()
    first = await repository.claim_next(
        conversation_id=conversation_id,
        owner=first_owner,
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    assert first.id == job_id

    replacement_owner = uuid7()
    replacement = await repository.claim_next(
        conversation_id=conversation_id,
        owner=replacement_owner,
        now=NOW + timedelta(seconds=30),
    )
    assert replacement is not None
    assert replacement.id == job_id
    assert replacement.fencing_token > first.fencing_token
    assert not await repository.complete_job(
        job_id=job_id,
        owner=first_owner,
        fencing_token=first.fencing_token,
        now=NOW + timedelta(seconds=31),
        succeeded=False,
    )
    assert await repository.complete_job(
        job_id=job_id,
        owner=replacement_owner,
        fencing_token=replacement.fencing_token,
        now=NOW + timedelta(seconds=31),
        succeeded=False,
    )
    retry = await repository.claim_next(
        conversation_id=conversation_id,
        owner=uuid7(),
        now=NOW + timedelta(seconds=31),
    )
    assert retry is not None
    assert retry.id == job_id


@pytest.mark.integration
async def test_m6_status_marks_revision_or_token_threshold_as_stale(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _turn_id, _revision_id = await seed_scope(db_session)
    repository = MemoryRepository(db_session)
    await repository.refresh_pending_job(
        account_id=account_id,
        conversation_id=conversation_id,
        job_kind="episode",
        event_range=EventRange(1, 1),
        estimated_input_tokens=6_000,
        now=NOW,
    )
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32),
        deployment_id="integration-status",
        nonce_source=lambda size: b"n" * size,
    )
    backend = DurableMemoryControlBackend(session=db_session, target_tokens=codec)
    status = await backend.status(account_id=account_id, conversation_id=conversation_id, now=NOW)
    assert status.freshness == "stale"
    assert status.pending_jobs == 1


@pytest.mark.integration
async def test_m6_embedding_indexes_and_dimension_binding_match_architecture(
    db_session: AsyncSession,
) -> None:
    index_names = set(
        await db_session.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'embedding_records'"
            )
        )
    )
    assert {
        "uq_embedding_records_memory_chunk",
        "uq_embedding_records_summary_chunk",
        "uq_embedding_records_message_chunk",
    } <= index_names
    active_index = await db_session.scalar(
        text(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indexrelid = 'uq_embedding_spaces_active'::regclass"
        )
    )
    assert active_index is not None
    assert "NULLS NOT DISTINCT" in active_index
    constraint_rows = (
        await db_session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid IN ('embedding_spaces'::regclass, "
                "'embedding_records'::regclass)"
            )
        )
    ).mappings()
    constraints: dict[str, str] = {
        cast(str, row["conname"]): cast(str, row["definition"]) for row in constraint_rows
    }
    assert (
        "FOREIGN KEY (embedding_space_id, account_id, dimensions)"
        in constraints["fk_embedding_records_space_scope"]
    )
    assert (
        "FOREIGN KEY (config_version_id, model_profile_id)"
        in constraints["fk_embedding_spaces_config_profile"]
    )


@pytest.mark.integration
async def test_m6_roles_keep_control_out_of_derived_truth_and_allow_review_commands(
    db_session: AsyncSession,
) -> None:
    tables = await db_session.scalar(
        text(
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN ('memory_jobs','memories','memory_versions','summary_versions',"
            "'embedding_spaces','embedding_records','memory_review_actions')"
        )
    )
    assert tables == 7
    can_mutate_memory = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_control_runtime', "
            "'memory_versions', 'INSERT')"
        )
    )
    can_request_review = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_control_runtime', "
            "'memory_review_actions', 'INSERT')"
        )
    )
    worker_can_apply_review = await db_session.scalar(
        text(
            "SELECT has_table_privilege('telegram_userbot_worker_runtime', "
            "'memory_review_actions', 'UPDATE')"
        )
    )
    assert can_mutate_memory is False
    assert can_request_review is True
    assert worker_can_apply_review is True


@pytest.mark.integration
async def test_m6_durable_control_backend_empty_scope_is_metadata_only(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, _turn_id, _revision_id = await seed_scope(db_session)
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32),
        deployment_id="integration",
        nonce_source=lambda size: b"n" * size,
    )
    backend = DurableMemoryControlBackend(session=db_session, target_tokens=codec)
    status = await backend.status(account_id=account_id, conversation_id=conversation_id, now=NOW)
    assert status.freshness == "fresh"
    assert status.pending_jobs == status.candidate_count == status.active_count == 0
    assert status.embedding_state == "unconfigured"
    assert not await backend.candidates(
        account_id=account_id,
        conversation_id=conversation_id,
        admin_id=42,
        bot_chat_id=42,
        now=NOW,
    )
    assert not await backend.active(
        account_id=account_id,
        conversation_id=conversation_id,
        admin_id=42,
        bot_chat_id=42,
        now=NOW,
    )
