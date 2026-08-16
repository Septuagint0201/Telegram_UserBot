"""PostgreSQL rejects remaining cross-account durable references."""

from datetime import timedelta
from typing import Any
from uuid import uuid7

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    background_jobs,
    context_manifests,
    context_preview_requests,
    control_commands,
    conversation_turns,
    embedding_records,
    embedding_spaces,
    media_objects,
    memory_jobs,
    message_media,
    message_revisions,
    turn_messages,
)
from tests.integration.test_m5_context_media import NOW, seed_policies, seed_scope
from tests.integration.test_m6_account_scope_constraints import _seed_profile

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _reject(session: AsyncSession, statement: Any) -> None:
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(statement)


@pytest.mark.integration
async def test_remaining_account_owned_references_reject_cross_scope(
    db_session: AsyncSession,
) -> None:
    account_a, conversation_a, turn_a, revision_a = await seed_scope(db_session)
    account_b, conversation_b, turn_b, revision_b = await seed_scope(db_session)
    revision_rows = (
        await db_session.execute(
            select(
                message_revisions.c.id,
                message_revisions.c.message_id,
                message_revisions.c.source_event_id,
            ).where(message_revisions.c.id.in_((revision_a, revision_b)))
        )
    ).mappings()
    revisions = {row["id"]: row for row in revision_rows}
    message_a = revisions[revision_a]["message_id"]
    event_b = revisions[revision_b]["source_event_id"]

    await _reject(
        db_session,
        update(message_revisions)
        .where(message_revisions.c.id == revision_a)
        .values(source_event_id=event_b),
    )

    media_a, media_b = uuid7(), uuid7()
    for media_id, account_id in ((media_a, account_a), (media_b, account_b)):
        await db_session.execute(
            insert(media_objects).values(
                id=media_id,
                account_id=account_id,
                object_kind="original",
                status="pending",
                retention_class="media_original_30d",
            )
        )
    await _reject(
        db_session,
        insert(media_objects).values(
            id=uuid7(),
            account_id=account_a,
            object_kind="provider_copy",
            status="pending",
            parent_object_id=media_b,
            retention_class="media_provider_copy_24h",
        ),
    )
    await _reject(
        db_session,
        insert(message_media).values(
            id=uuid7(),
            account_id=account_a,
            message_revision_id=revision_a,
            media_object_id=media_b,
            media_kind="photo",
            position=0,
            metadata_schema_version=1,
            metadata={},
        ),
    )

    await _reject(
        db_session,
        update(conversation_turns)
        .where(conversation_turns.c.id == turn_a)
        .values(supersedes_turn_id=turn_b),
    )
    await _reject(
        db_session,
        insert(turn_messages).values(
            turn_id=turn_a,
            account_id=account_a,
            conversation_id=conversation_a,
            message_id=message_a,
            message_revision_no=1,
            ordinal=1,
            source_event_id=event_b,
        ),
    )

    background_a, background_b = uuid7(), uuid7()
    for job_id, account_id in ((background_a, account_a), (background_b, account_b)):
        await db_session.execute(
            insert(background_jobs).values(
                id=job_id,
                account_id=account_id,
                queue_name="account-scope-test",
                job_type="synthetic",
                idempotency_key=job_id.bytes + job_id.bytes,
                payload_schema_version=1,
                payload={},
            )
        )

    context_policy_id, retrieval_policy_id = await seed_policies(db_session)
    manifest_id = uuid7()
    manifest_values = {
        "id": manifest_id,
        "account_id": account_a,
        "conversation_id": conversation_a,
        "owner_kind": "turn",
        "turn_id": turn_a,
        "purpose": "reactive_reply",
        "logical_role": "main_ai",
        "builder_version": "scope-v1",
        "prompt_version": "scope-v1",
        "prompt_bundle_sha256": b"p" * 32,
        "context_policy_version_id": context_policy_id,
        "retrieval_policy_version_id": retrieval_policy_id,
        "retrieval_policy_version": "scope-v1",
        "token_policy_version": "scope-v1",
        "token_estimator_version": "scope-v1",
        "capability_snapshot_sha256": b"c" * 32,
        "memory_freshness": "fresh",
        "effective_input_budget": 100,
        "safety_reserve_tokens": 0,
        "estimated_instruction_tokens": 0,
        "estimated_text_tokens": 0,
        "estimated_image_tokens": 0,
        "estimated_structural_tokens": 0,
        "input_token_estimate": 0,
        "image_count": 0,
        "omission_count": 0,
        "source_revision_vector_sha256": b"s" * 32,
        "manifest_sha256": b"m" * 32,
    }
    await db_session.execute(insert(context_manifests).values(**manifest_values))
    await _reject(
        db_session,
        insert(context_manifests).values(
            **{
                **manifest_values,
                "id": uuid7(),
                "conversation_id": None,
                "owner_kind": "background_job",
                "turn_id": None,
                "background_job_id": background_b,
                "manifest_sha256": b"b" * 32,
            }
        ),
    )
    await _reject(
        db_session,
        insert(memory_jobs).values(
            id=uuid7(),
            account_id=account_a,
            conversation_id=conversation_a,
            background_job_id=background_b,
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
        ),
    )

    control_id = uuid7()
    await db_session.execute(
        insert(control_commands).values(
            id=control_id,
            account_id=account_b,
            conversation_id=conversation_b,
            bot_identity="control",
            telegram_update_id=control_id.int % 2**63,
            admin_telegram_user_id=42,
            bot_chat_id=42,
            command_kind="context_preview",
            idempotency_key=b"d" * 32,
            state="pending",
        )
    )
    await _reject(
        db_session,
        insert(context_preview_requests).values(
            id=uuid7(),
            control_command_id=control_id,
            bot_identity="control",
            admin_user_id=42,
            bot_chat_id=42,
            account_id=account_a,
            conversation_id=conversation_a,
            context_manifest_id=manifest_id,
            manifest_sha256=b"m" * 32,
            source_revision_vector_sha256=b"s" * 32,
            state="pending_confirmation",
            token_expires_at=NOW + timedelta(minutes=5),
        ),
    )

    embedding_profile, embedding_config, _, _ = await _seed_profile(
        db_session,
        logical_role="embedding",
        profile_kind="embedding",
        protocol="embedding",
    )
    space_b = uuid7()
    await db_session.execute(
        insert(embedding_spaces).values(
            id=space_b,
            account_id=account_b,
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
            embedding_space_id=space_b,
            message_revision_id=revision_a,
            chunk_index=0,
            chunker_version="scope-v1",
            source_sha256=b"h" * 32,
            vector_payload=[0.1, 0.2],
            dimensions=2,
            state="ready",
        ),
    )
