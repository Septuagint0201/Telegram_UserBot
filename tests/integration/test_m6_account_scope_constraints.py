"""PostgreSQL rejects cross-account M5/M6 references at the schema boundary."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    context_manifest_items,
    context_manifests,
    embedding_records,
    embedding_spaces,
    memories,
    memory_evidence,
    memory_input_manifest_items,
    memory_input_manifests,
    memory_jobs,
    memory_proposal_evidence,
    memory_proposals,
    memory_relations,
    memory_review_actions,
    memory_versions,
    model_capability_snapshots,
    model_config_versions,
    model_credential_versions,
    model_credentials,
    model_endpoints,
    model_profiles,
    model_runs,
    summaries,
    summary_version_sources,
    summary_versions,
    summary_watermarks,
)
from tests.integration.test_m5_context_media import NOW, seed_policies, seed_scope

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _reject(session: AsyncSession, statement: Any) -> None:
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(statement)


async def _seed_profile(
    session: AsyncSession,
    *,
    logical_role: str,
    profile_kind: str,
    protocol: str,
) -> tuple[UUID, UUID, UUID, UUID]:
    profile_id, credential_id, credential_version_id = uuid7(), uuid7(), uuid7()
    endpoint_id, capability_id, config_id = uuid7(), uuid7(), uuid7()
    await session.execute(
        insert(model_endpoints).values(
            id=endpoint_id,
            label=f"scope-{logical_role}",
            base_url="https://provider.example.invalid/v1",
            canonical_sha256=b"e" * 32,
            network_policy_id=uuid7(),
            network_policy_version=1,
            network_category="public",
            created_by_admin_id=42,
        )
    )
    await session.execute(
        insert(model_profiles).values(
            id=profile_id,
            logical_role=logical_role,
            profile_kind=profile_kind,
            state="disabled",
            version=1,
        )
    )
    await session.execute(
        insert(model_credentials).values(
            id=credential_id,
            profile_id=profile_id,
            status="active",
            active_version_no=1,
            latest_version_no=1,
            version=1,
        )
    )
    await session.execute(
        insert(model_credential_versions).values(
            id=credential_version_id,
            credential_id=credential_id,
            profile_id=profile_id,
            version_no=1,
            algorithm="aes_256_gcm",
            key_version=1,
            aad_schema_version=1,
            nonce=b"n" * 12,
            ciphertext=b"ciphertext",
            secret_fingerprint=b"f" * 32,
        )
    )
    await session.execute(
        insert(model_capability_snapshots).values(
            id=capability_id,
            endpoint_id=endpoint_id,
            protocol=protocol,
            model_name=f"scope-{logical_role}",
            supports_text=True,
            supports_temperature=profile_kind == "generation",
            supports_reasoning_effort=False,
            supports_image=False,
            supports_stream=False,
            supports_structured_output=True,
            max_context_tokens=32_000,
            max_output_tokens_limit=512 if profile_kind == "generation" else None,
            supported_input_roles=["system", "user", "assistant"],
            embedding_dimensions=[2] if profile_kind == "embedding" else [],
            status="valid",
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    await session.execute(
        insert(model_config_versions).values(
            id=config_id,
            profile_id=profile_id,
            profile_kind=profile_kind,
            version_no=1,
            endpoint_id=endpoint_id,
            credential_id=credential_id,
            capability_snapshot_id=capability_id,
            protocol=protocol,
            model_name=f"scope-{logical_role}",
            temperature=0.2 if profile_kind == "generation" else None,
            max_output_tokens=512 if profile_kind == "generation" else None,
            timeout_seconds=30,
            enabled=True,
            config_sha256=b"c" * 32,
            created_by_admin_id=42,
            validated_at=NOW,
        )
    )
    return profile_id, config_id, credential_version_id, capability_id


async def _seed_memory_run(  # noqa: PLR0913 - mirrors the persisted run identity
    session: AsyncSession,
    *,
    account_id: UUID,
    conversation_id: UUID,
    profile_id: UUID,
    config_id: UUID,
    credential_version_id: UUID,
) -> UUID:
    run_id = uuid7()
    await session.execute(
        insert(model_runs).values(
            id=run_id,
            account_id=account_id,
            conversation_id=conversation_id,
            logical_role="memory_agent",
            model_profile_id=profile_id,
            purpose="memory_extraction",
            generation_no=1,
            state="succeeded",
            config_version_id=config_id,
            credential_version_id=credential_version_id,
            prompt_version="scope-v1",
            prompt_bundle_sha256=b"p" * 32,
            capability_snapshot_sha256=b"c" * 32,
            input_fingerprint=b"i" * 32,
            adapter_version="scope-v1",
            request_schema_version=1,
            output_schema_version=1,
            normalizer_version="scope-v1",
        )
    )
    return run_id


async def _seed_memory(
    session: AsyncSession, *, account_id: UUID, memory_id: UUID, version_id: UUID
) -> None:
    await session.execute(
        insert(memories).values(
            id=memory_id,
            account_id=account_id,
            memory_type="fact",
            semantic_key_hash=b"k" * 32,
            status="active",
            current_version_no=1,
        )
    )
    await session.execute(
        insert(memory_versions).values(
            id=version_id,
            account_id=account_id,
            memory_id=memory_id,
            version_no=1,
            operation="create",
            payload_schema_version=1,
            payload={"value": "synthetic"},
            rendered_text="synthetic",
            importance=0.5,
            confidence=0.9,
            time_precision="unknown",
            validator_policy_version="scope-v1",
            acceptance_kind="automatic",
        )
    )


@pytest.mark.integration
async def test_m5_m6_cross_account_references_are_rejected(
    db_session: AsyncSession,
) -> None:
    account_a, conversation_a, turn_a, _revision_a = await seed_scope(db_session)
    account_b, conversation_b, _turn_b, revision_b = await seed_scope(db_session)
    context_policy_id, retrieval_policy_id = await seed_policies(db_session)

    context_manifest_id = uuid7()
    await db_session.execute(
        insert(context_manifests).values(
            id=context_manifest_id,
            account_id=account_a,
            conversation_id=conversation_a,
            owner_kind="turn",
            turn_id=turn_a,
            purpose="reactive_reply",
            logical_role="main_ai",
            builder_version="scope-v1",
            prompt_version="scope-v1",
            prompt_bundle_sha256=b"p" * 32,
            context_policy_version_id=context_policy_id,
            retrieval_policy_version_id=retrieval_policy_id,
            retrieval_policy_version="scope-v1",
            token_policy_version="scope-v1",  # noqa: S106 - policy version, not a secret
            token_estimator_version="scope-v1",  # noqa: S106 - version label, not a secret
            capability_snapshot_sha256=b"c" * 32,
            memory_freshness="fresh",
            effective_input_budget=100,
            safety_reserve_tokens=0,
            estimated_instruction_tokens=0,
            estimated_text_tokens=0,
            estimated_image_tokens=0,
            estimated_structural_tokens=0,
            input_token_estimate=0,
            image_count=0,
            omission_count=0,
            source_revision_vector_sha256=b"s" * 32,
            manifest_sha256=b"m" * 32,
        )
    )
    await _reject(
        db_session,
        insert(context_manifest_items).values(
            manifest_id=context_manifest_id,
            account_id=account_a,
            ordinal=1,
            layer="current",
            canonical_role="user",
            source_actor="contact",
            source_type="message_revision",
            source_id=revision_b,
            source_revision="revision-1",
            message_revision_id=revision_b,
            trust_level="untrusted_user",
            token_estimate=0,
            estimated_image_tokens=0,
            content_sha256=b"h" * 32,
            rendered_part_sha256=b"r" * 32,
        ),
    )

    job_id = uuid7()
    await db_session.execute(
        insert(memory_jobs).values(
            id=job_id,
            account_id=account_a,
            conversation_id=conversation_a,
            job_kind="episode",
            state="pending",
            generation=1,
            range_start_event_id=1,
            range_end_event_id=1,
            idempotency_key=b"j" * 32,
            quiet_until=NOW,
            hard_due_at=NOW,
            pipeline_version="scope-v1",
            policy_version="scope-v1",
            prompt_version="scope-v1",
            input_schema_version=1,
            output_schema_version=1,
        )
    )
    memory_manifest_id = uuid7()
    await db_session.execute(
        insert(memory_input_manifests).values(
            id=memory_manifest_id,
            account_id=account_a,
            conversation_id=conversation_a,
            memory_job_id=job_id,
            generation=1,
            manifest_kind="episode",
            range_start_event_id=1,
            range_end_event_id=1,
            pipeline_version="scope-v1",
            policy_version="scope-v1",
            prompt_version="scope-v1",
            input_schema_version=1,
            output_schema_version=1,
            input_token_estimate=0,
            image_count=0,
            manifest_sha256=b"q" * 32,
        )
    )
    await _reject(
        db_session,
        insert(memory_input_manifest_items).values(
            manifest_id=memory_manifest_id,
            account_id=account_a,
            ordinal=1,
            source_type="message_revision",
            message_revision_id=revision_b,
            inclusion_role="episode",
            trust_class="untrusted_user",
            source_content_sha256=b"h" * 32,
            selection_reason_code="scope_test",
        ),
    )

    summary_a, summary_version_a = uuid7(), uuid7()
    summary_b, summary_version_b = uuid7(), uuid7()
    for account_id, conversation_id, summary_id, version_id in (
        (account_a, conversation_a, summary_a, summary_version_a),
        (account_b, conversation_b, summary_b, summary_version_b),
    ):
        await db_session.execute(
            insert(summaries).values(
                id=summary_id,
                account_id=account_id,
                conversation_id=conversation_id,
                summary_kind="rolling",
                status="active",
                current_version_no=1,
            )
        )
        await db_session.execute(
            insert(summary_versions).values(
                id=version_id,
                account_id=account_id,
                summary_id=summary_id,
                version_no=1,
                range_start_event_id=1,
                range_end_event_id=1,
                pipeline_version="scope-v1",
                output_schema_version=1,
                manifest_sha256=b"v" * 32,
                invalidation_state="active",
            )
        )
    await _reject(
        db_session,
        insert(summary_version_sources).values(
            summary_version_id=summary_version_a,
            account_id=account_a,
            ordinal=1,
            message_revision_id=revision_b,
            inclusion_role="episode",
            source_content_sha256=b"h" * 32,
        ),
    )
    await _reject(
        db_session,
        insert(summary_watermarks).values(
            account_id=account_a,
            conversation_id=conversation_a,
            summary_kind="rolling",
            last_included_event_id=1,
            last_summary_version_id=summary_version_b,
            version=1,
        ),
    )

    embedding_profile, embedding_config, _embedding_credential, _ = await _seed_profile(
        db_session, logical_role="embedding", profile_kind="embedding", protocol="embedding"
    )
    embedding_space_id = uuid7()
    await db_session.execute(
        insert(embedding_spaces).values(
            id=embedding_space_id,
            account_id=account_a,
            model_profile_id=embedding_profile,
            profile_kind="embedding",
            config_version_id=embedding_config,
            model_name_snapshot="scope-embedding",
            dimensions=2,
            distance_metric="cosine",
            normalization="l2",
            chunker_version="scope-v1",
            state="active",
            generation=1,
            activated_at=NOW,
        )
    )
    await _reject(
        db_session,
        insert(embedding_records).values(
            id=uuid7(),
            account_id=account_a,
            embedding_space_id=embedding_space_id,
            message_revision_id=revision_b,
            chunk_index=0,
            chunker_version="scope-v1",
            source_sha256=b"h" * 32,
            vector_payload=[0.1, 0.2],
            dimensions=2,
            state="ready",
        ),
    )

    memory_profile, memory_config, memory_credential, _ = await _seed_profile(
        db_session,
        logical_role="memory_agent",
        profile_kind="generation",
        protocol="openai_responses",
    )
    run_a = await _seed_memory_run(
        db_session,
        account_id=account_a,
        conversation_id=conversation_a,
        profile_id=memory_profile,
        config_id=memory_config,
        credential_version_id=memory_credential,
    )
    run_b = await _seed_memory_run(
        db_session,
        account_id=account_b,
        conversation_id=conversation_b,
        profile_id=memory_profile,
        config_id=memory_config,
        credential_version_id=memory_credential,
    )
    proposal_id = uuid7()
    await _reject(
        db_session,
        insert(memory_proposals).values(
            id=uuid7(),
            account_id=account_a,
            conversation_id=conversation_a,
            memory_job_id=job_id,
            model_run_id=run_b,
            model_role="memory_agent",
            idempotency_key=b"x" * 32,
            proposal_ordinal=0,
            operation="create",
            memory_type="fact",
            semantic_key_hash=b"k" * 32,
            payload_schema_version=1,
            proposed_payload={"value": "synthetic"},
            proposed_confidence=0.9,
            proposed_importance=0.5,
            state="candidate",
            validator_policy_version="scope-v1",
            retention_class="standard",
        ),
    )
    await db_session.execute(
        insert(memory_proposals).values(
            id=proposal_id,
            account_id=account_a,
            conversation_id=conversation_a,
            memory_job_id=job_id,
            model_run_id=run_a,
            model_role="memory_agent",
            idempotency_key=b"y" * 32,
            proposal_ordinal=0,
            operation="create",
            memory_type="fact",
            semantic_key_hash=b"k" * 32,
            payload_schema_version=1,
            proposed_payload={"value": "synthetic"},
            proposed_confidence=0.9,
            proposed_importance=0.5,
            state="candidate",
            validator_policy_version="scope-v1",
            retention_class="standard",
        )
    )
    await _reject(
        db_session,
        insert(memory_proposal_evidence).values(
            proposal_id=proposal_id,
            account_id=account_a,
            message_revision_id=revision_b,
            evidence_role="primary",
            source_content_sha256=b"h" * 32,
            source_normalization_version="scope-v1",
            trust_class="untrusted_user",
        ),
    )

    memory_a, memory_version_a = uuid7(), uuid7()
    memory_b, memory_version_b = uuid7(), uuid7()
    await _seed_memory(
        db_session, account_id=account_a, memory_id=memory_a, version_id=memory_version_a
    )
    await _seed_memory(
        db_session, account_id=account_b, memory_id=memory_b, version_id=memory_version_b
    )
    await _reject(
        db_session,
        insert(memory_evidence).values(
            memory_version_id=memory_version_a,
            account_id=account_a,
            message_revision_id=revision_b,
            evidence_role="primary",
            trust_class="untrusted_user",
            source_content_sha256=b"h" * 32,
        ),
    )
    await _reject(
        db_session,
        insert(memory_relations).values(
            from_version_id=memory_version_a,
            to_version_id=memory_version_b,
            account_id=account_a,
            relation_type="supports",
        ),
    )
    await _reject(
        db_session,
        insert(memory_review_actions).values(
            id=uuid7(),
            account_id=account_a,
            action="forget",
            memory_id=memory_b,
            admin_actor_id=42,
            bot_chat_id=42,
            action_token_hash=b"a" * 32,
            expires_at=NOW + timedelta(minutes=5),
            state="pending",
        ),
    )
