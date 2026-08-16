"""PostgreSQL repository for the M6 asynchronous memory pipeline.

Provider calls never happen in this repository.  Every method is a short
transactional unit and uses row locks/CAS so a worker crash can be reconciled
without duplicating a generation or a review action.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    data_erasure_requests,
    embedding_records,
    embedding_spaces,
    erasure_ledger,
    media_objects,
    memories,
    memory_evidence,
    memory_input_manifest_items,
    memory_input_manifests,
    memory_jobs,
    memory_proposal_evidence,
    memory_proposal_targets,
    memory_proposals,
    memory_review_actions,
    memory_versions,
    message_revisions,
    messages,
    model_runs,
    summaries,
    summary_version_sources,
    summary_versions,
    summary_watermarks,
)
from telegram_userbot.domain.memory.lifecycle import (
    AcceptanceResult,
    MemoryConflictError,
)
from telegram_userbot.domain.memory.models import (
    EmbeddingRecord,
    EmbeddingRecordState,
    EmbeddingSpace,
    InputManifest,
    MemoryOperation,
    ProposalState,
    SummaryVersion,
)
from telegram_userbot.domain.memory.summary import SummaryCoverageError, SummaryWatermark
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

    async def _require_message_revision_scope(
        self, source_id: UUID, *, account_id: UUID, conversation_id: UUID
    ) -> None:
        found = await self._session.scalar(
            select(message_revisions.c.id)
            .join(messages, messages.c.id == message_revisions.c.message_id)
            .where(
                message_revisions.c.id == source_id,
                message_revisions.c.account_id == account_id,
                messages.c.account_id == account_id,
                messages.c.conversation_id == conversation_id,
            )
        )
        if found is None:
            raise ValueError("message revision is outside the requested scope")

    async def _require_manifest_source_scope(
        self, source: Any, *, account_id: UUID, conversation_id: UUID
    ) -> None:
        if source.source_type == "message_revision":
            await self._require_message_revision_scope(
                source.source_id, account_id=account_id, conversation_id=conversation_id
            )
            return
        if source.source_type == "media_object":
            found = await self._session.scalar(
                select(media_objects.c.id).where(
                    media_objects.c.id == source.source_id,
                    media_objects.c.account_id == account_id,
                )
            )
        elif source.source_type == "memory_version":
            found = await self._session.scalar(
                select(memory_versions.c.id)
                .join(memories, memories.c.id == memory_versions.c.memory_id)
                .where(
                    memory_versions.c.id == source.source_id,
                    memory_versions.c.account_id == account_id,
                    memories.c.account_id == account_id,
                    memories.c.conversation_id == conversation_id,
                )
            )
        elif source.source_type == "summary_version":
            found = await self._session.scalar(
                select(summary_versions.c.id)
                .join(summaries, summaries.c.id == summary_versions.c.summary_id)
                .where(
                    summary_versions.c.id == source.source_id,
                    summary_versions.c.account_id == account_id,
                    summaries.c.account_id == account_id,
                    summaries.c.conversation_id == conversation_id,
                )
            )
        else:
            raise ValueError("unsupported manifest source type")
        if found is None:
            raise ValueError("manifest source is outside the requested scope")

    async def _require_embedding_target_scope(
        self, record: EmbeddingRecord, *, account_id: UUID
    ) -> None:
        if record.target_kind == "memory_version":
            query = (
                select(memory_versions.c.id)
                .join(memories, memories.c.id == memory_versions.c.memory_id)
                .where(
                    memory_versions.c.id == record.target_id,
                    memories.c.id == memory_versions.c.memory_id,
                )
            )
            query = query.where(
                memory_versions.c.account_id == account_id,
                memories.c.account_id == account_id,
            )
        elif record.target_kind == "summary_version":
            query = (
                select(summary_versions.c.id)
                .join(summaries, summaries.c.id == summary_versions.c.summary_id)
                .where(
                    summary_versions.c.id == record.target_id,
                    summaries.c.id == summary_versions.c.summary_id,
                )
            )
            query = query.where(
                summary_versions.c.account_id == account_id,
                summaries.c.account_id == account_id,
            )
        else:
            query = (
                select(message_revisions.c.id)
                .join(messages, messages.c.id == message_revisions.c.message_id)
                .where(
                    message_revisions.c.id == record.target_id,
                    messages.c.id == message_revisions.c.message_id,
                )
            )
            query = query.where(
                message_revisions.c.account_id == account_id,
                messages.c.account_id == account_id,
            )
        if await self._session.scalar(query) is None:
            raise ValueError("embedding target is outside the requested account scope")

    async def _embedding_record_belongs_to_account(self, record: Any, *, account_id: UUID) -> bool:
        if record["memory_version_id"] is not None:
            query = select(memory_versions.c.id).where(
                memory_versions.c.id == record["memory_version_id"],
                memory_versions.c.account_id == account_id,
            )
        elif record["summary_version_id"] is not None:
            query = select(summary_versions.c.id).where(
                summary_versions.c.id == record["summary_version_id"],
                summary_versions.c.account_id == account_id,
            )
        else:
            query = select(message_revisions.c.id).where(
                message_revisions.c.id == record["message_revision_id"],
                message_revisions.c.account_id == account_id,
            )
        return await self._session.scalar(query) is not None

    async def _require_erasure_derived_scope(
        self,
        derived_ids: dict[str, set[UUID]] | None,
        *,
        account_id: UUID,
        conversation_id: UUID | None,
    ) -> None:
        supported_derived_kinds = {"embedding", "summary"}
        if set(derived_ids or ()) - supported_derived_kinds:
            raise ValueError("erasure derived target kind is unsupported")
        for kind, targets in (derived_ids or {}).items():
            for target_id in targets:
                if kind == "embedding":
                    record = (
                        (
                            await self._session.execute(
                                select(embedding_records).where(
                                    embedding_records.c.id == target_id,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if (
                        record is None
                        or record["account_id"] != account_id
                        or not await self._embedding_record_belongs_to_account(
                            record, account_id=account_id
                        )
                    ):
                        raise ValueError("erasure embedding target is outside account scope")
                    continue
                query = (
                    select(summary_versions.c.id)
                    .join(summaries, summaries.c.id == summary_versions.c.summary_id)
                    .where(
                        summary_versions.c.id == target_id,
                        summary_versions.c.account_id == account_id,
                        summaries.c.account_id == account_id,
                    )
                )
                if conversation_id is not None:
                    query = query.where(summaries.c.conversation_id == conversation_id)
                if await self._session.scalar(query) is None:
                    raise ValueError("erasure summary target is outside account scope")

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
        for source in manifest.sources:
            await self._require_manifest_source_scope(
                source,
                account_id=manifest.account_id,
                conversation_id=manifest.conversation_id,
            )
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
                    account_id=manifest.account_id,
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
        persisted_job = await self._session.scalar(
            select(memory_jobs.c.id).where(
                memory_jobs.c.id == job_id,
                memory_jobs.c.account_id == proposal.account_id,
                memory_jobs.c.conversation_id == proposal.conversation_id,
                memory_jobs.c.input_manifest_id == manifest.id,
            )
        )
        if persisted_job is None:
            raise ValueError("proposal job and manifest are outside the requested scope")
        persisted_run = await self._session.scalar(
            select(model_runs.c.id).where(
                model_runs.c.id == model_run_id,
                model_runs.c.account_id == proposal.account_id,
                model_runs.c.conversation_id == proposal.conversation_id,
                model_runs.c.logical_role == "memory_agent",
            )
        )
        if persisted_run is None:
            raise ValueError("proposal model run is outside the requested scope")
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
            await self._require_message_revision_scope(
                evidence.source_id,
                account_id=proposal.account_id,
                conversation_id=proposal.conversation_id,
            )
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

    async def accept_validated_proposal(  # noqa: PLR0912, PLR0915 - transaction transitions are explicit
        self,
        validated: ValidatedProposal,
        *,
        acceptance_kind: str = "automatic",
        expected_versions: dict[UUID, int] | None = None,
        now: datetime | None = None,
        allow_candidate: bool = False,
    ) -> AcceptanceResult:
        """Apply one validated proposal using row locks and deterministic IDs.

        The caller owns the transaction.  Replaying the same proposal returns the
        prior result and never creates a second memory version.
        """

        proposal = validated.proposal
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        if acceptance_kind not in {"automatic", "manual", "reconciliation", "migration"}:
            raise ValueError("unknown acceptance kind")
        proposal_row = await self._proposal_row(proposal)
        if proposal_row is None:
            raise ValueError("proposal has not been recorded")
        proposal_id = cast(UUID, proposal_row["id"])
        for evidence in proposal.evidence:
            await self._require_message_revision_scope(
                evidence.source_id,
                account_id=proposal.account_id,
                conversation_id=proposal.conversation_id,
            )
        if proposal_row["state"] == ProposalState.ACCEPTED.value:
            prior_version_id = cast(UUID | None, proposal_row["accepted_memory_version_id"])
            prior_memory_id = None
            if prior_version_id is not None:
                prior_memory_id = cast(
                    UUID | None,
                    await self._session.scalar(
                        select(memory_versions.c.memory_id).where(
                            memory_versions.c.id == prior_version_id
                        )
                    ),
                )
            return AcceptanceResult(
                proposal.id,
                ProposalState.ACCEPTED,
                prior_memory_id,
                prior_version_id,
                True,
            )
        if proposal_row["state"] in {
            ProposalState.REJECTED.value,
            ProposalState.INVALIDATED.value,
            ProposalState.EXPIRED.value,
            ProposalState.ERROR.value,
        }:
            return AcceptanceResult(
                proposal.id,
                ProposalState(proposal_row["state"]),
                None,
                None,
                True,
            )
        if validated.state is not ProposalState.ACCEPTED and not (
            allow_candidate and validated.state is ProposalState.CANDIDATE
        ):
            await self._session.execute(
                update(memory_proposals)
                .where(memory_proposals.c.id == proposal_id)
                .values(
                    state=validated.state.value,
                    decision_reason_code=validated.issues[0].code if validated.issues else None,
                    decided_at=current_time,
                )
            )
            return AcceptanceResult(proposal.id, validated.state, None, None)

        expected_versions = expected_versions or {}
        target_ids = proposal.target_memory_ids
        target_rows = []
        if target_ids:
            target_rows = list(
                (
                    await self._session.execute(
                        select(memories)
                        .where(
                            memories.c.id.in_(target_ids),
                            memories.c.account_id == proposal.account_id,
                            memories.c.conversation_id == proposal.conversation_id,
                        )
                        .order_by(memories.c.id)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
        by_id = {cast(UUID, row["id"]): row for row in target_rows}
        if len(by_id) != len(target_ids):
            raise MemoryConflictError("proposal target is unavailable")
        target_rows = [by_id[target_id] for target_id in target_ids]
        for target_id in target_ids:
            row = by_id[target_id]
            expected = expected_versions.get(target_id, cast(int, row["current_version_no"]))
            if row["current_version_no"] != expected or row["status"] != "active":
                raise MemoryConflictError("target version changed before acceptance")
            if proposal.operation in {MemoryOperation.UPDATE, MemoryOperation.INVALIDATE} and (
                row["memory_type"] != proposal.memory_type.value
                or row["semantic_key_hash"] != proposal.semantic_key_hash
            ):
                raise MemoryConflictError("target memory identity changed")

        memory_id: UUID
        version_no: int
        version_id: UUID
        status = "active"
        if proposal.operation in {MemoryOperation.CREATE, MemoryOperation.SUPERSEDE}:
            collision = await self._session.scalar(
                select(memories.c.id)
                .where(
                    memories.c.account_id == proposal.account_id,
                    memories.c.conversation_id == proposal.conversation_id,
                    memories.c.semantic_key_hash == proposal.semantic_key_hash,
                    memories.c.status == "active",
                )
                .limit(1)
            )
            if collision is not None and (
                proposal.operation is MemoryOperation.CREATE
                or not target_rows
                or collision != target_rows[0]["id"]
            ):
                raise MemoryConflictError("semantic key already has an active memory")
            memory_id = uuid5(
                proposal.account_id,
                f"memory:{proposal.conversation_id}:{proposal.id}:{proposal.semantic_key_hash.hex()}",
            )
            version_no = 1
            version_id = uuid5(memory_id, "memory-version:1")
            await self._session.execute(
                postgresql_insert(memories)
                .values(
                    id=memory_id,
                    account_id=proposal.account_id,
                    conversation_id=proposal.conversation_id,
                    memory_type=proposal.memory_type.value,
                    semantic_key_hash=proposal.semantic_key_hash,
                    status="active",
                    current_version_no=1,
                    superseded_by_memory_id=None,
                    created_at=current_time,
                    updated_at=current_time,
                )
                .on_conflict_do_nothing(constraint="memories_pkey")
            )
            if proposal.operation is MemoryOperation.SUPERSEDE and target_rows:
                await self._session.execute(
                    update(memories)
                    .where(memories.c.id == target_rows[0]["id"])
                    .values(
                        status="superseded",
                        superseded_by_memory_id=memory_id,
                        updated_at=current_time,
                    )
                )
        else:
            if not target_rows:
                raise MemoryConflictError("operation target is missing")
            memory_id = cast(UUID, target_rows[0]["id"])
            version_no = cast(int, target_rows[0]["current_version_no"]) + 1
            version_id = uuid5(memory_id, f"memory-version:{version_no}")
            status = "invalidated" if proposal.operation is MemoryOperation.INVALIDATE else "active"

        await self._session.execute(
            postgresql_insert(memory_versions)
            .values(
                id=version_id,
                account_id=proposal.account_id,
                memory_id=memory_id,
                version_no=version_no,
                operation=proposal.operation.value,
                payload_schema_version=1,
                payload=dict(proposal.payload),
                rendered_text=proposal.rendered_text,
                importance=proposal.importance,
                confidence=proposal.confidence,
                observed_at=current_time,
                valid_from=proposal.valid_from,
                valid_to=proposal.valid_to,
                time_precision="unknown",
                model_run_id=proposal_row["model_run_id"],
                model_role="memory_agent",
                prompt_version=None,
                validator_policy_version=proposal_row["validator_policy_version"],
                acceptance_kind=acceptance_kind,
                created_at=current_time,
            )
            .on_conflict_do_nothing(constraint="memory_versions_pkey")
        )
        await self._session.execute(
            update(memories)
            .where(memories.c.id == memory_id)
            .values(current_version_no=version_no, status=status, updated_at=current_time)
        )
        if proposal.operation is MemoryOperation.MERGE:
            for source in target_rows[1:]:
                await self._session.execute(
                    update(memories)
                    .where(memories.c.id == source["id"])
                    .values(
                        status="superseded",
                        superseded_by_memory_id=memory_id,
                        updated_at=current_time,
                    )
                )
        for evidence in proposal.evidence:
            await self._session.execute(
                postgresql_insert(memory_evidence)
                .values(
                    memory_version_id=version_id,
                    account_id=proposal.account_id,
                    message_revision_id=evidence.source_id,
                    summary_version_id=None,
                    other_memory_version_id=None,
                    media_object_id=None,
                    evidence_role=evidence.role.value,
                    trust_class=evidence.trust.value,
                    source_content_sha256=evidence.source_content_sha256,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(constraint="pk_memory_evidence")
            )
        await self._session.execute(
            update(memory_proposals)
            .where(memory_proposals.c.id == proposal_id)
            .values(
                state="accepted",
                accepted_memory_version_id=version_id,
                decision_actor_type="service",
                decision_actor_id="memory_worker",
                decision_reason_code=None,
                decided_at=current_time,
            )
        )
        return AcceptanceResult(proposal.id, ProposalState.ACCEPTED, memory_id, version_id)

    async def publish_summary(  # noqa: PLR0912, PLR0913 - summary transitions are explicit
        self,
        summary: SummaryVersion,
        *,
        account_id: UUID,
        conversation_id: UUID,
        expected_version: int | None = None,
        period_key: str | None = None,
        timezone_snapshot: str | None = None,
        model_run_id: UUID | None = None,
        pipeline_version: str = "m6-v1",
        output_schema_version: int = 1,
        now: datetime | None = None,
    ) -> SummaryWatermark:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"summary:{account_id}:{conversation_id}:{summary.kind.value}")},
        )
        for source in summary.sources:
            if source.source_kind == "message_revision":
                await self._require_message_revision_scope(
                    source.source_id,
                    account_id=account_id,
                    conversation_id=conversation_id,
                )
                continue
            found = await self._session.scalar(
                select(summary_versions.c.id)
                .join(summaries, summaries.c.id == summary_versions.c.summary_id)
                .where(
                    summary_versions.c.id == source.source_id,
                    summary_versions.c.account_id == account_id,
                    summaries.c.account_id == account_id,
                    summaries.c.conversation_id == conversation_id,
                )
            )
            if found is None:
                raise ValueError("prior summary source is outside the requested scope")
        watermark_row = (
            (
                await self._session.execute(
                    select(summary_watermarks)
                    .where(
                        summary_watermarks.c.account_id == account_id,
                        summary_watermarks.c.conversation_id == conversation_id,
                        summary_watermarks.c.summary_kind == summary.kind.value,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if watermark_row is None:
            last_included, watermark_version = 0, 1
        else:
            last_included = cast(int, watermark_row["last_included_event_id"])
            watermark_version = cast(int, watermark_row["version"])
        if summary.range_start_event_id > last_included + 1:
            raise SummaryCoverageError("summary watermark cannot skip an uncovered event")
        if summary.range_end_event_id < last_included:
            raise SummaryCoverageError("summary watermark cannot move backwards")
        summary_row = (
            (
                await self._session.execute(
                    select(summaries)
                    .where(
                        summaries.c.id == summary.summary_id,
                        summaries.c.account_id == account_id,
                        summaries.c.conversation_id == conversation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if summary_row is None:
            if summary.version_no != 1:
                raise SummaryCoverageError("first summary version must be one")
            await self._session.execute(
                insert(summaries).values(
                    id=summary.summary_id,
                    account_id=account_id,
                    conversation_id=conversation_id,
                    summary_kind=summary.kind.value,
                    period_key=period_key or f"{summary.kind.value}:{summary.range_start_event_id}",
                    timezone_snapshot=timezone_snapshot,
                    period_start_at=None,
                    period_end_at=None,
                    status=summary.status.value,
                    current_version_no=summary.version_no,
                    created_at=current_time,
                    updated_at=current_time,
                )
            )
        else:
            if summary_row["summary_kind"] != summary.kind.value:
                raise SummaryCoverageError("summary kind does not match its durable identity")
            existing_version = (
                (
                    await self._session.execute(
                        select(summary_versions).where(
                            summary_versions.c.id == summary.id,
                            summary_versions.c.account_id == account_id,
                            summary_versions.c.summary_id == summary.summary_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_version is not None:
                if not _summary_version_matches(
                    existing_version,
                    summary,
                    model_run_id=model_run_id,
                    pipeline_version=pipeline_version,
                    output_schema_version=output_schema_version,
                ):
                    raise SummaryCoverageError("summary replay does not match its manifest")
                return SummaryWatermark(
                    conversation_id,
                    summary.kind,
                    last_included,
                    watermark_version,
                    cast(UUID | None, watermark_row["last_summary_version_id"])
                    if watermark_row is not None
                    else None,
                )
            if (
                expected_version is not None
                and summary_row["current_version_no"] != expected_version
            ):
                raise SummaryCoverageError("summary current pointer changed")
            if summary.version_no != summary_row["current_version_no"] + 1:
                raise SummaryCoverageError("summary versions must be contiguous")
            await self._session.execute(
                update(summaries)
                .where(
                    summaries.c.id == summary.summary_id,
                    summaries.c.account_id == account_id,
                    summaries.c.conversation_id == conversation_id,
                )
                .values(
                    current_version_no=summary.version_no,
                    status=summary.status.value,
                    updated_at=current_time,
                )
            )
            await self._session.execute(
                update(summary_versions)
                .where(
                    summary_versions.c.summary_id == summary.summary_id,
                    summary_versions.c.account_id == account_id,
                    summary_versions.c.id != summary.id,
                    summary_versions.c.invalidation_state == "active",
                )
                .values(invalidation_state="invalidated")
            )
        await self._session.execute(
            postgresql_insert(summary_versions)
            .values(
                id=summary.id,
                account_id=account_id,
                summary_id=summary.summary_id,
                version_no=summary.version_no,
                range_start_event_id=summary.range_start_event_id,
                range_end_event_id=summary.range_end_event_id,
                content_text=summary.content_text,
                content_sha256=hashlib.sha256(summary.content_text.encode()).digest(),
                model_run_id=model_run_id,
                model_role="memory_agent" if model_run_id is not None else None,
                prompt_version=None,
                pipeline_version=pipeline_version,
                output_schema_version=output_schema_version,
                manifest_sha256=summary.manifest_sha256,
                invalidation_state=summary.status.value,
                created_at=current_time,
            )
            .on_conflict_do_nothing(constraint="summary_versions_pkey")
        )
        for source in summary.sources:
            await self._session.execute(
                postgresql_insert(summary_version_sources)
                .values(
                    summary_version_id=summary.id,
                    account_id=account_id,
                    ordinal=source.ordinal,
                    message_revision_id=source.source_id
                    if source.source_kind == "message_revision"
                    else None,
                    prior_summary_version_id=source.source_id
                    if source.source_kind == "prior_summary_version"
                    else None,
                    inclusion_role="episode",
                    source_content_sha256=source.content_sha256,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(constraint="pk_summary_version_sources")
            )
        if watermark_row is None:
            await self._session.execute(
                insert(summary_watermarks).values(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    summary_kind=summary.kind.value,
                    last_included_event_id=summary.range_end_event_id,
                    last_summary_version_id=summary.id,
                    version=2,
                    updated_at=current_time,
                )
            )
        else:
            moved = cast(
                CursorResult[Any],
                await self._session.execute(
                    update(summary_watermarks)
                    .where(
                        summary_watermarks.c.account_id == account_id,
                        summary_watermarks.c.conversation_id == conversation_id,
                        summary_watermarks.c.summary_kind == summary.kind.value,
                        summary_watermarks.c.version == watermark_version,
                    )
                    .values(
                        last_included_event_id=summary.range_end_event_id,
                        last_summary_version_id=summary.id,
                        version=watermark_version + 1,
                        updated_at=current_time,
                    )
                ),
            )
            if moved.rowcount != 1:
                raise SummaryCoverageError("summary watermark changed during publication")
        return SummaryWatermark(
            conversation_id,
            summary.kind,
            summary.range_end_event_id,
            watermark_version + 1 if watermark_row is not None else 2,
            summary.id,
        )

    async def write_embedding_records(  # noqa: PLR0912, PLR0913 - explicit transition inputs
        self,
        space: EmbeddingSpace,
        records: tuple[EmbeddingRecord, ...],
        *,
        account_id: UUID,
        model_profile_id: UUID,
        config_version_id: UUID,
        activate: bool = False,
        now: datetime | None = None,
    ) -> int:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        if any(
            record.space_id != space.id or len(record.vector) != space.dimensions
            for record in records
        ):
            raise ValueError("embedding record dimension or space mismatch")
        if activate and not records:
            raise ValueError("cannot activate an embedding space without records")
        if activate and any(record.state is not EmbeddingRecordState.READY for record in records):
            raise ValueError("embedding activation requires ready records")
        target_indexes: dict[tuple[str, UUID], list[int]] = {}
        for record in records:
            target_indexes.setdefault((record.target_kind, record.target_id), []).append(
                record.chunk_index
            )
        if any(
            sorted(indexes) != list(range(max(indexes) + 1)) for indexes in target_indexes.values()
        ):
            raise ValueError("embedding record chunks are not contiguous")
        for record in records:
            await self._require_embedding_target_scope(record, account_id=account_id)
        await self._session.execute(
            postgresql_insert(embedding_spaces)
            .values(
                id=space.id,
                account_id=account_id,
                model_profile_id=model_profile_id,
                profile_kind="embedding",
                config_version_id=config_version_id,
                model_name_snapshot=space.model_name,
                dimensions=space.dimensions,
                distance_metric=space.distance_metric,
                normalization=space.normalization,
                chunker_version=space.chunker_version,
                state=space.state.value,
                generation=space.generation,
                created_at=current_time,
            )
            .on_conflict_do_nothing(constraint="embedding_spaces_pkey")
        )
        persisted_space = (
            (
                await self._session.execute(
                    select(embedding_spaces)
                    .where(embedding_spaces.c.id == space.id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if (
            persisted_space["account_id"] != account_id
            or persisted_space["model_profile_id"] != model_profile_id
            or persisted_space["config_version_id"] != config_version_id
            or persisted_space["model_name_snapshot"] != space.model_name
            or persisted_space["dimensions"] != space.dimensions
            or persisted_space["distance_metric"] != space.distance_metric
            or persisted_space["normalization"] != space.normalization
            or persisted_space["chunker_version"] != space.chunker_version
            or persisted_space["generation"] != space.generation
        ):
            raise ValueError("embedding space replay does not match durable identity")
        if activate and persisted_space["state"] not in {"building", "active"}:
            raise ValueError("only a building or active embedding space can be activated")
        for record in records:
            target_columns: dict[str, object] = {
                "memory_version_id": record.target_id
                if record.target_kind == "memory_version"
                else None,
                "summary_version_id": record.target_id
                if record.target_kind == "summary_version"
                else None,
                "message_revision_id": record.target_id
                if record.target_kind == "message_revision"
                else None,
            }
            existing = (
                (
                    await self._session.execute(
                        select(embedding_records)
                        .where(
                            embedding_records.c.embedding_space_id == space.id,
                            embedding_records.c.chunk_index == record.chunk_index,
                            *[
                                getattr(embedding_records.c, key) == value
                                for key, value in target_columns.items()
                                if value is not None
                            ],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            values = {
                "id": record.id,
                "account_id": account_id,
                "embedding_space_id": space.id,
                **target_columns,
                "chunk_index": record.chunk_index,
                "chunker_version": space.chunker_version,
                "source_sha256": record.source_sha256,
                "vector_payload": list(record.vector),
                "dimensions": space.dimensions,
                "state": record.state.value,
                "created_at": current_time,
            }
            if existing is None:
                await self._session.execute(insert(embedding_records).values(**values))
            elif (
                existing["id"] != record.id
                or existing["account_id"] != account_id
                or existing["source_sha256"] != record.source_sha256
                or existing["vector_payload"] != list(record.vector)
                or existing["dimensions"] != space.dimensions
                or existing["chunker_version"] != space.chunker_version
                or existing["state"] != record.state.value
            ):
                raise ValueError("embedding record replay does not match durable identity")
        if activate:
            non_ready = await self._session.scalar(
                select(func.count())
                .select_from(embedding_records)
                .where(
                    embedding_records.c.embedding_space_id == space.id,
                    embedding_records.c.state != EmbeddingRecordState.READY.value,
                )
            )
            if non_ready:
                raise ValueError("embedding space coverage is not fully ready")
            await self._session.execute(
                update(embedding_spaces)
                .where(
                    embedding_spaces.c.model_profile_id == model_profile_id,
                    embedding_spaces.c.account_id == account_id,
                    embedding_spaces.c.state == "active",
                    embedding_spaces.c.id != space.id,
                )
                .values(state="retired", retired_at=current_time)
            )
            await self._session.execute(
                update(embedding_spaces)
                .where(
                    embedding_spaces.c.id == space.id,
                    embedding_spaces.c.account_id == account_id,
                    embedding_spaces.c.model_profile_id == model_profile_id,
                )
                .values(state="active", activated_at=current_time)
            )
        return len(records)

    async def forget_memory(
        self,
        *,
        account_id: UUID,
        memory_id: UUID,
        reason: str = "policy",
        now: datetime | None = None,
    ) -> bool:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        row = (
            (
                await self._session.execute(
                    select(memories)
                    .where(
                        memories.c.id == memory_id,
                        memories.c.account_id == account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        await self._session.execute(
            update(memories)
            .where(memories.c.id == memory_id, memories.c.account_id == account_id)
            .values(status="forgotten", forgotten_at=current_time, updated_at=current_time)
        )
        await self._session.execute(
            update(memory_versions)
            .where(
                memory_versions.c.memory_id == memory_id,
                memory_versions.c.account_id == account_id,
            )
            .values(
                payload={}, rendered_text=None, redacted_at=current_time, redaction_reason=reason
            )
        )
        await self._session.execute(
            update(embedding_records)
            .where(
                embedding_records.c.memory_version_id.in_(
                    select(memory_versions.c.id).where(
                        memory_versions.c.memory_id == memory_id,
                        memory_versions.c.account_id == account_id,
                    )
                )
            )
            .values(state="invalidated", invalidated_at=current_time)
        )
        return True

    async def record_erasure(  # noqa: PLR0913 - erasure audit identity is explicit
        self,
        source_id: UUID | None = None,
        *,
        account_id: UUID,
        memory_id: UUID | None = None,
        reason: str = "policy",
        policy_version: int = 1,
        requested_by: str = "memory_worker",
        scope_secret: bytes = b"memory-erasure-v1",
        now: datetime | None = None,
        derived_ids: dict[str, set[UUID]] | None = None,
    ) -> UUID:
        if policy_version < 1 or not requested_by or not scope_secret:
            raise ValueError("erasure request metadata is invalid")
        memory_id = memory_id or source_id
        if memory_id is None:
            raise ValueError("erasure source is required")
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        memory_row = (
            (
                await self._session.execute(
                    select(memories.c.id, memories.c.conversation_id).where(
                        memories.c.id == memory_id,
                        memories.c.account_id == account_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if memory_row is None:
            raise ValueError("erasure memory is outside the requested account scope")
        await self._require_erasure_derived_scope(
            derived_ids,
            account_id=account_id,
            conversation_id=cast(UUID | None, memory_row["conversation_id"]),
        )
        key = hmac.new(
            scope_secret, account_id.bytes + memory_id.bytes + reason.encode(), "sha256"
        ).digest()
        request_id = uuid5(account_id, f"erasure:{memory_id}:{key.hex()}")
        await self._session.execute(
            postgresql_insert(data_erasure_requests)
            .values(
                id=request_id,
                account_id=account_id,
                scope_type="memory",
                memory_id=memory_id,
                contact_id=None,
                state="completed",
                requested_by=requested_by,
                request_idempotency_key=key,
                policy_version=policy_version,
                created_at=current_time,
                updated_at=current_time,
                completed_at=current_time,
            )
            .on_conflict_do_nothing(constraint="uq_data_erasure_requests_idempotency")
        )
        await self._session.execute(
            postgresql_insert(erasure_ledger)
            .values(
                account_scope_hmac=hmac.new(scope_secret, account_id.bytes, "sha256").digest(),
                scope_type="memory",
                target_scope_hmac=hmac.new(scope_secret, memory_id.bytes, "sha256").digest(),
                request_id=request_id,
                policy_version=policy_version,
                completed_at=current_time,
            )
            .on_conflict_do_nothing(index_elements=[erasure_ledger.c.request_id])
        )
        await self.forget_memory(
            account_id=account_id, memory_id=memory_id, reason=reason, now=current_time
        )
        for kind, targets in (derived_ids or {}).items():
            if not targets:
                continue
            if kind == "embedding":
                await self._session.execute(
                    update(embedding_records)
                    .where(
                        embedding_records.c.id.in_(targets),
                        embedding_records.c.account_id == account_id,
                    )
                    .values(state="invalidated", invalidated_at=current_time)
                )
            elif kind == "summary":
                await self._session.execute(
                    update(summary_versions)
                    .where(
                        summary_versions.c.id.in_(targets),
                        summary_versions.c.account_id == account_id,
                    )
                    .values(invalidation_state="invalidated", redacted_at=current_time)
                )
        return request_id

    async def _proposal_row(self, proposal: Any) -> Any:
        row = (
            (
                await self._session.execute(
                    select(memory_proposals)
                    .where(
                        memory_proposals.c.id == proposal.id,
                        memory_proposals.c.account_id == proposal.account_id,
                        memory_proposals.c.conversation_id == proposal.conversation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return row
        exact = (
            (
                await self._session.execute(
                    select(memory_proposals)
                    .where(
                        memory_proposals.c.account_id == proposal.account_id,
                        memory_proposals.c.conversation_id == proposal.conversation_id,
                        memory_proposals.c.semantic_key_hash == proposal.semantic_key_hash,
                        memory_proposals.c.operation == proposal.operation.value,
                        memory_proposals.c.memory_type == proposal.memory_type.value,
                        memory_proposals.c.proposed_payload == dict(proposal.payload),
                        memory_proposals.c.proposed_text == proposal.rendered_text,
                        memory_proposals.c.proposed_confidence == proposal.confidence,
                        memory_proposals.c.proposed_importance == proposal.importance,
                        memory_proposals.c.state.in_(
                            ("received", "validating", "candidate", "accepted")
                        ),
                    )
                    .order_by(memory_proposals.c.created_at.desc(), memory_proposals.c.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if exact is not None:
            return exact
        return (
            (
                await self._session.execute(
                    select(memory_proposals)
                    .where(
                        memory_proposals.c.account_id == proposal.account_id,
                        memory_proposals.c.conversation_id == proposal.conversation_id,
                        memory_proposals.c.semantic_key_hash == proposal.semantic_key_hash,
                        memory_proposals.c.state.in_(
                            ("received", "validating", "candidate", "accepted")
                        ),
                    )
                    .order_by(memory_proposals.c.created_at.desc(), memory_proposals.c.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

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


def _summary_version_matches(
    row: Any,
    summary: SummaryVersion,
    *,
    model_run_id: UUID | None,
    pipeline_version: str,
    output_schema_version: int,
) -> bool:
    return all(
        row[column] == value
        for column, value in {
            "id": summary.id,
            "summary_id": summary.summary_id,
            "version_no": summary.version_no,
            "range_start_event_id": summary.range_start_event_id,
            "range_end_event_id": summary.range_end_event_id,
            "content_text": summary.content_text,
            "content_sha256": hashlib.sha256(summary.content_text.encode()).digest(),
            "model_run_id": model_run_id,
            "pipeline_version": pipeline_version,
            "output_schema_version": output_schema_version,
            "manifest_sha256": summary.manifest_sha256,
            "invalidation_state": summary.status.value,
        }.items()
    )
