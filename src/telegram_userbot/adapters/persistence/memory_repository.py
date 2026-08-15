"""PostgreSQL repository for the M6 asynchronous memory pipeline.

Provider calls never happen in this repository.  Every method is a short
transactional unit and uses row locks/CAS so a worker crash can be reconciled
without duplicating a generation or a review action.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    memories,
    memory_input_manifest_items,
    memory_input_manifests,
    memory_jobs,
    memory_proposal_evidence,
    memory_proposal_targets,
    memory_proposals,
    memory_review_actions,
)
from telegram_userbot.domain.memory.models import InputManifest, MemoryOperation
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.memory.validation import ValidatedProposal
from telegram_userbot.domain.shared.hashing import stable_json_bytes


@dataclass(frozen=True, slots=True)
class MemoryJobLease:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    job_kind: str
    generation: int
    range_start_event_id: int
    range_end_event_id: int
    lease_owner: UUID
    fencing_token: int
    input_manifest_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReviewAction:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    action: str
    proposal_id: UUID | None
    memory_id: UUID | None
    expected_memory_version: int | None
    expires_at: datetime


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def refresh_pending_job(  # noqa: PLR0913 - job snapshot fields are explicit
        self,
        *,
        account_id: UUID,
        conversation_id: UUID,
        job_kind: str,
        event_range: EventRange,
        estimated_input_tokens: int,
        now: datetime,
        pipeline_version: str = "m6-v1",
        policy_version: str = "policy-v1",
        prompt_version: str = "prompt-v1",
    ) -> UUID:
        if estimated_input_tokens < 0 or job_kind not in {
            "episode",
            "rolling_summary",
            "consolidation",
            "reconciliation",
        }:
            raise ValueError("memory job input is invalid")
        lock_key = f"memory_job:{account_id}:{conversation_id}:{job_kind}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        row = (
            (
                await self._session.execute(
                    select(memory_jobs)
                    .where(
                        memory_jobs.c.account_id == account_id,
                        memory_jobs.c.conversation_id == conversation_id,
                        memory_jobs.c.job_kind == job_kind,
                        memory_jobs.c.state == "pending",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            await self._session.execute(
                update(memory_jobs)
                .where(memory_jobs.c.id == row["id"], memory_jobs.c.state == "pending")
                .values(
                    range_start_event_id=min(row["range_start_event_id"], event_range.start),
                    range_end_event_id=max(row["range_end_event_id"], event_range.end),
                    eligible_revision_count=memory_jobs.c.eligible_revision_count + 1,
                    estimated_input_tokens=memory_jobs.c.estimated_input_tokens
                    + estimated_input_tokens,
                    quiet_until=now + timedelta(seconds=45),
                    job_version=memory_jobs.c.job_version + 1,
                    updated_at=now,
                )
            )
            return cast(UUID, row["id"])
        generation = (
            await self._session.scalar(
                select(memory_jobs.c.generation)
                .where(
                    memory_jobs.c.conversation_id == conversation_id,
                    memory_jobs.c.job_kind == job_kind,
                )
                .order_by(memory_jobs.c.generation.desc())
                .limit(1)
            )
            or 0
        ) + 1
        job_id = uuid4()
        idempotency_key = hashlib.sha256(
            stable_json_bytes(
                {
                    "account_id": str(account_id),
                    "conversation_id": str(conversation_id),
                    "job_kind": job_kind,
                    "generation": generation,
                }
            )
        ).digest()
        await self._session.execute(
            insert(memory_jobs).values(
                id=job_id,
                account_id=account_id,
                conversation_id=conversation_id,
                job_kind=job_kind,
                state="pending",
                generation=generation,
                range_start_event_id=event_range.start,
                range_end_event_id=event_range.end,
                eligible_revision_count=1,
                estimated_input_tokens=estimated_input_tokens,
                idempotency_key=idempotency_key,
                quiet_until=now + timedelta(seconds=45),
                hard_due_at=now + timedelta(minutes=10),
                pipeline_version=pipeline_version,
                policy_version=policy_version,
                prompt_version=prompt_version,
                input_schema_version=1,
                output_schema_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        return job_id

    async def claim_next(  # noqa: PLR0913 - lease policy snapshot is explicit
        self,
        *,
        conversation_id: UUID,
        owner: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=60),
        revision_threshold: int = 20,
        token_threshold: int = 6_000,
    ) -> MemoryJobLease | None:
        if lease_duration <= timedelta(0) or revision_threshold <= 0 or token_threshold <= 0:
            raise ValueError("memory lease policy is invalid")
        pending_due = (memory_jobs.c.state == "pending") & (
            (memory_jobs.c.quiet_until <= now)
            | (memory_jobs.c.hard_due_at <= now)
            | (memory_jobs.c.eligible_revision_count >= revision_threshold)
            | (memory_jobs.c.estimated_input_tokens >= token_threshold)
        )
        retry_due = memory_jobs.c.state == "retry_wait"
        expired_lease = (memory_jobs.c.state == "running") & (memory_jobs.c.lease_expires_at <= now)
        row = (
            (
                await self._session.execute(
                    select(memory_jobs)
                    .where(
                        memory_jobs.c.conversation_id == conversation_id,
                        pending_due | retry_due | expired_lease,
                    )
                    .order_by(memory_jobs.c.hard_due_at, memory_jobs.c.generation)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        updated = (
            (
                await self._session.execute(
                    update(memory_jobs)
                    .where(
                        memory_jobs.c.id == row["id"],
                        memory_jobs.c.state == row["state"],
                        memory_jobs.c.job_version == row["job_version"],
                    )
                    .values(
                        state="running",
                        lease_owner=owner,
                        lease_expires_at=now + lease_duration,
                        job_version=memory_jobs.c.job_version + 1,
                        updated_at=now,
                    )
                    .returning(
                        memory_jobs.c.id,
                        memory_jobs.c.account_id,
                        memory_jobs.c.conversation_id,
                        memory_jobs.c.job_kind,
                        memory_jobs.c.generation,
                        memory_jobs.c.range_start_event_id,
                        memory_jobs.c.range_end_event_id,
                        memory_jobs.c.job_version,
                        memory_jobs.c.input_manifest_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        return MemoryJobLease(
            id=cast(UUID, updated["id"]),
            account_id=cast(UUID, updated["account_id"]),
            conversation_id=cast(UUID, updated["conversation_id"]),
            job_kind=cast(str, updated["job_kind"]),
            generation=cast(int, updated["generation"]),
            range_start_event_id=cast(int, updated["range_start_event_id"]),
            range_end_event_id=cast(int, updated["range_end_event_id"]),
            lease_owner=owner,
            fencing_token=cast(int, updated["job_version"]),
            input_manifest_id=cast(UUID | None, updated["input_manifest_id"]),
        )

    async def complete_job(
        self, *, job_id: UUID, owner: UUID, fencing_token: int, now: datetime, succeeded: bool
    ) -> bool:
        statement = update(memory_jobs).where(
            memory_jobs.c.id == job_id,
            memory_jobs.c.state == "running",
            memory_jobs.c.lease_owner == owner,
            memory_jobs.c.job_version == fencing_token,
        )
        if succeeded:
            statement = statement.where(memory_jobs.c.input_manifest_id.is_not(None))
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                statement.values(
                    state="succeeded" if succeeded else "retry_wait",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now if succeeded else None,
                    job_version=memory_jobs.c.job_version + 1,
                    updated_at=now,
                )
            ),
        )
        return result.rowcount == 1

    async def create_manifest(
        self,
        manifest: InputManifest,
        *,
        lease: MemoryJobLease,
        now: datetime,
    ) -> None:
        if (
            manifest.account_id != lease.account_id
            or manifest.conversation_id != lease.conversation_id
            or manifest.generation != lease.generation
            or manifest.range_start_event_id != lease.range_start_event_id
            or manifest.range_end_event_id != lease.range_end_event_id
            or lease.input_manifest_id is not None
        ):
            raise ValueError("manifest does not match the claimed memory generation")
        await self._session.execute(
            insert(memory_input_manifests).values(
                id=manifest.id,
                account_id=manifest.account_id,
                conversation_id=manifest.conversation_id,
                memory_job_id=lease.id,
                generation=manifest.generation,
                manifest_kind="episode",
                range_start_event_id=manifest.range_start_event_id,
                range_end_event_id=manifest.range_end_event_id,
                pipeline_version=manifest.pipeline_version,
                policy_version=manifest.policy_version,
                prompt_version=manifest.prompt_version,
                input_schema_version=manifest.input_schema_version,
                output_schema_version=manifest.output_schema_version,
                input_token_estimate=manifest.input_token_estimate,
                image_count=manifest.image_count,
                manifest_sha256=manifest.manifest_sha256,
            )
        )
        for ordinal, source in enumerate(manifest.sources, 1):
            await self._session.execute(
                insert(memory_input_manifest_items).values(
                    manifest_id=manifest.id,
                    ordinal=ordinal,
                    source_type=source.source_type,
                    message_revision_id=source.source_id
                    if source.source_type == "message_revision"
                    else None,
                    media_object_id=source.source_id
                    if source.source_type == "media_object"
                    else None,
                    memory_version_id=source.source_id
                    if source.source_type == "memory_version"
                    else None,
                    summary_version_id=source.source_id
                    if source.source_type == "summary_version"
                    else None,
                    inclusion_role="episode",
                    trust_class=source.trust.value,
                    source_content_sha256=source.content_sha256,
                    selection_reason_code="memory_trigger",
                )
            )
        sealed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(memory_jobs)
                .where(
                    memory_jobs.c.id == lease.id,
                    memory_jobs.c.state == "running",
                    memory_jobs.c.lease_owner == lease.lease_owner,
                    memory_jobs.c.job_version == lease.fencing_token,
                    memory_jobs.c.input_manifest_id.is_(None),
                )
                .values(input_manifest_id=manifest.id, sealed_at=now, updated_at=now)
            ),
        )
        if sealed.rowcount != 1:
            raise RuntimeError("memory generation lease is stale or already sealed")

    async def record_proposal(  # noqa: PLR0913 - proposal identity snapshot is explicit
        self,
        validated: ValidatedProposal,
        *,
        job_id: UUID,
        model_run_id: UUID,
        proposal_ordinal: int,
        manifest: InputManifest,
        validator_policy_version: str = "m6-v1",
    ) -> bool:
        if proposal_ordinal < 0 or not validator_policy_version:
            raise ValueError("proposal persistence identity is invalid")
        proposal = validated.proposal
        if (
            proposal.account_id != manifest.account_id
            or proposal.conversation_id != manifest.conversation_id
        ):
            raise ValueError("proposal and manifest scope differ")
        idempotency_key = hashlib.sha256(
            stable_json_bytes(
                {"model_run_id": str(model_run_id), "proposal_ordinal": proposal_ordinal}
            )
        ).digest()
        persisted_proposal_id = _proposal_storage_id(model_run_id, proposal_ordinal)
        result = await self._session.execute(
            postgresql_insert(memory_proposals)
            .values(
                id=persisted_proposal_id,
                account_id=proposal.account_id,
                conversation_id=proposal.conversation_id,
                memory_job_id=job_id,
                model_run_id=model_run_id,
                idempotency_key=idempotency_key,
                proposal_ordinal=proposal_ordinal,
                operation=proposal.operation.value,
                memory_type=proposal.memory_type.value,
                semantic_key_hash=proposal.semantic_key_hash,
                payload_schema_version=1,
                proposed_payload=dict(proposal.payload),
                proposed_text=proposal.rendered_text,
                proposed_confidence=proposal.confidence,
                proposed_importance=proposal.importance,
                proposed_valid_from=proposal.valid_from,
                proposed_valid_to=proposal.valid_to,
                visual_only=proposal.visual_only,
                state=validated.state.value,
                validation_code=validated.issues[0].code if validated.issues else None,
                validator_policy_version=validator_policy_version,
                retention_class="memory_proposal",
            )
            .on_conflict_do_nothing(constraint="uq_memory_proposals_idempotency")
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            return False
        for index, target_id in enumerate(proposal.target_memory_ids):
            target = (
                (
                    await self._session.execute(
                        select(memories.c.current_version_no).where(
                            memories.c.id == target_id,
                            memories.c.account_id == proposal.account_id,
                            memories.c.conversation_id == proposal.conversation_id,
                            memories.c.status == "active",
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise ValueError("proposal target is unavailable")
            await self._session.execute(
                insert(memory_proposal_targets).values(
                    proposal_id=persisted_proposal_id,
                    account_id=proposal.account_id,
                    target_memory_id=target_id,
                    target_version_no_snapshot=target["current_version_no"],
                    target_role=_target_role(proposal.operation, index),
                )
            )
        for evidence in proposal.evidence:
            source = manifest.source(evidence.source_id)
            if source is None or source.source_type != "message_revision":
                raise ValueError("proposal evidence lacks a canonical message root")
            await self._session.execute(
                insert(memory_proposal_evidence).values(
                    proposal_id=persisted_proposal_id,
                    account_id=proposal.account_id,
                    message_revision_id=evidence.source_id,
                    evidence_role=evidence.role.value,
                    quoted_span_start=evidence.span_start,
                    quoted_span_end=evidence.span_end,
                    source_content_sha256=evidence.source_content_sha256,
                    source_normalization_version="unicode-codepoint-v1",
                    trust_class=source.trust.value,
                )
            )
        return True

    async def issue_review_action(  # noqa: PLR0913 - review token binding is explicit
        self,
        *,
        account_id: UUID,
        conversation_id: UUID,
        admin_actor_id: int,
        bot_chat_id: int,
        action: str,
        proposal_id: UUID | None,
        memory_id: UUID | None,
        token: bytes,
        expires_at: datetime,
        expected_memory_version: int | None = None,
    ) -> UUID:
        proposal_action = action in {"accept", "reject"}
        forget_action = action == "forget"
        target_matches = (proposal_action and proposal_id is not None and memory_id is None) or (
            forget_action and memory_id is not None and proposal_id is None
        )
        if not target_matches or len(token) < 16:
            raise ValueError("review action is invalid")
        action_id = uuid4()
        await self._session.execute(
            insert(memory_review_actions).values(
                id=action_id,
                account_id=account_id,
                conversation_id=conversation_id,
                action=action,
                proposal_id=proposal_id,
                memory_id=memory_id,
                expected_memory_version=expected_memory_version,
                admin_actor_id=admin_actor_id,
                bot_chat_id=bot_chat_id,
                action_token_hash=hashlib.sha256(token).digest(),
                expires_at=expires_at,
                state="pending",
            )
        )
        return action_id

    async def consume_review_action(
        self, *, token: bytes, admin_actor_id: int, bot_chat_id: int, now: datetime
    ) -> ReviewAction | None:
        row = (
            (
                await self._session.execute(
                    select(memory_review_actions)
                    .where(
                        memory_review_actions.c.action_token_hash == hashlib.sha256(token).digest(),
                        memory_review_actions.c.admin_actor_id == admin_actor_id,
                        memory_review_actions.c.bot_chat_id == bot_chat_id,
                        memory_review_actions.c.state == "pending",
                        memory_review_actions.c.expires_at > now,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        await self._session.execute(
            update(memory_review_actions)
            .where(
                memory_review_actions.c.id == row["id"], memory_review_actions.c.state == "pending"
            )
            .values(state="confirmed", used_at=now)
        )
        return ReviewAction(
            id=cast(UUID, row["id"]),
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            action=cast(str, row["action"]),
            proposal_id=cast(UUID | None, row["proposal_id"]),
            memory_id=cast(UUID | None, row["memory_id"]),
            expected_memory_version=cast(int | None, row["expected_memory_version"]),
            expires_at=cast(datetime, row["expires_at"]),
        )

    async def lock_confirmed_review_action(self, *, account_id: UUID) -> ReviewAction | None:
        row = (
            (
                await self._session.execute(
                    select(memory_review_actions)
                    .where(
                        memory_review_actions.c.account_id == account_id,
                        memory_review_actions.c.state == "confirmed",
                    )
                    .order_by(memory_review_actions.c.used_at, memory_review_actions.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ReviewAction(
            id=cast(UUID, row["id"]),
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            action=cast(str, row["action"]),
            proposal_id=cast(UUID | None, row["proposal_id"]),
            memory_id=cast(UUID | None, row["memory_id"]),
            expected_memory_version=cast(int | None, row["expected_memory_version"]),
            expires_at=cast(datetime, row["expires_at"]),
        )

    async def finish_review_action(
        self,
        *,
        action_id: UUID,
        applied: bool,
        now: datetime,
        reason_code: str | None = None,
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(memory_review_actions)
                .where(
                    memory_review_actions.c.id == action_id,
                    memory_review_actions.c.state == "confirmed",
                )
                .values(
                    state="applied" if applied else "rejected",
                    decided_at=now,
                    reason_code=reason_code,
                )
            ),
        )
        return result.rowcount == 1


def _target_role(operation: MemoryOperation, index: int) -> str:
    if operation is MemoryOperation.MERGE and index > 0:
        return "merge_source"
    if operation is MemoryOperation.SUPERSEDE:
        return "superseded"
    if operation is MemoryOperation.INVALIDATE:
        return "invalidated"
    return "primary"


def _proposal_storage_id(model_run_id: UUID, proposal_ordinal: int) -> UUID:
    """Keep untrusted provider IDs out of the durable proposal identity."""

    if proposal_ordinal < 0:
        raise ValueError("proposal ordinal must be non-negative")
    return uuid5(model_run_id, f"memory-proposal:{proposal_ordinal}")
