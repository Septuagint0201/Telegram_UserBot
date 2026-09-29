"""Atomic memory decisions, summaries, watermarks and embedding production."""

from __future__ import annotations

import hmac
import json
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.embedding_runtime import EmbeddingRuntimeRepository
from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError
from telegram_userbot.adapters.persistence.memory_periods import MemoryPeriodRepository
from telegram_userbot.adapters.persistence.memory_pipeline import (
    MemoryPipelineRepository,
    PreparedMemory,
    memory_run_id,
)
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.records import JobRecord
from telegram_userbot.domain.memory import EventRange, rolling_summary_due, validate_proposal
from telegram_userbot.domain.memory.models import (
    MemoryProposal,
    ProposalState,
    SummaryKind,
    SummarySource,
    SummaryVersion,
)


class MemoryResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def complete(  # noqa: PLR0913 - explicit fenced result metadata
        self,
        job: JobRecord,
        prepared: PreparedMemory,
        output: Any,
        *,
        raw: str,
        input_tokens: int,
        output_tokens: int,
        secret: bytes,
        now: datetime,
    ) -> None:
        pipeline = MemoryPipelineRepository(self.session)
        row = await pipeline.verify_completion(job, prepared, secret=secret, now=now)
        watermark = await pipeline.watermark(row)
        if row["job_kind"] != "consolidation" and (
            row["range_start_event_id"] > watermark + 1 or row["range_end_event_id"] < watermark
        ):
            raise MemoryPipelineError("MEMORY_COVERAGE_CHANGED")
        if prepared.period is not None:
            summary_id = await MemoryPeriodRepository(self.session).publish(
                prepared.manifest,
                prepared.period,
                output["summary_text"],
                job_id=prepared.lease.id,
                now=now,
            )
            await EmbeddingRuntimeRepository(self.session).stage_target(
                account_id=row["account_id"],
                target_kind="summary_version",
                target_id=summary_id,
                now=now,
            )
        elif row["job_kind"] == "rolling_summary":
            await self._summary(prepared, output, now=now)
        else:
            await self._proposals(prepared, output, now=now)
            if row["job_kind"] != "consolidation":
                await self.session.execute(
                    insert(s.memory_watermarks)
                    .values(
                        account_id=row["account_id"],
                        conversation_id=row["conversation_id"],
                        watermark_kind=row["job_kind"],
                        last_scanned_event_id=row["range_end_event_id"],
                        last_contiguous_decided_event_id=row["range_end_event_id"],
                        last_succeeded_job_id=row["id"],
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        constraint="pk_memory_watermarks",
                        set_={
                            "last_scanned_event_id": row["range_end_event_id"],
                            "last_contiguous_decided_event_id": row["range_end_event_id"],
                            "last_succeeded_job_id": row["id"],
                            "version": s.memory_watermarks.c.version + 1,
                            "updated_at": now,
                        },
                    )
                )
        embedding = EmbeddingRuntimeRepository(self.session)
        if row["job_kind"] == "episode":
            await self._schedule_summary(prepared, now=now)
        for source in prepared.manifest.sources:
            if (
                source.source_type == "message_revision"
                and json.loads(source.content)["kind"] != "none"
            ):
                await embedding.stage_target(
                    account_id=row["account_id"],
                    target_kind="message_revision",
                    target_id=source.source_id,
                    now=now,
                )
        await self.session.execute(
            update(s.model_runs)
            .where(s.model_runs.c.id == memory_run_id(row["id"]))
            .values(
                state="succeeded",
                completed_at=now,
                finish_reason="complete",
                result_kind="memory_decision",
                is_complete=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                output_fingerprint=hmac.digest(
                    secret, b"memory-output-v1\0" + raw.encode(), "sha256"
                ),
            )
        )
        await self.session.execute(
            update(s.model_run_attempts)
            .where(
                s.model_run_attempts.c.model_run_id == memory_run_id(row["id"]),
                s.model_run_attempts.c.attempt_no == job.attempt_count,
            )
            .values(
                state="succeeded",
                completed_at=now,
                http_status=200,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        if not await MemoryRepository(self.session).complete_job(
            job_id=row["id"],
            owner=prepared.lease.lease_owner,
            fencing_token=prepared.lease.fencing_token,
            now=now,
            succeeded=True,
        ):
            raise MemoryPipelineError("MEMORY_JOB_FENCE_LOST", retryable=True)

    async def _schedule_summary(self, prepared: PreparedMemory, *, now: datetime) -> None:
        manifest = prepared.manifest
        watermark = (
            await self.session.scalar(
                select(s.summary_watermarks.c.last_included_event_id).where(
                    s.summary_watermarks.c.conversation_id == manifest.conversation_id,
                    s.summary_watermarks.c.summary_kind == "rolling",
                )
            )
            or 0
        )
        count, byte_count = (
            await self.session.execute(
                select(
                    func.count(s.message_revisions.c.id),
                    func.coalesce(
                        func.sum(
                            func.octet_length(
                                func.coalesce(
                                    s.message_revisions.c.text_content,
                                    s.message_revisions.c.caption,
                                )
                            )
                        ),
                        0,
                    ),
                )
                .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
                .where(
                    s.messages.c.account_id == manifest.account_id,
                    s.messages.c.conversation_id == manifest.conversation_id,
                    s.messages.c.current_revision_no == s.message_revisions.c.revision_no,
                    s.messages.c.deleted_at.is_(None),
                    s.messages.c.is_tombstone.is_(False),
                    s.messages.c.source_status != "pending",
                    s.message_revisions.c.redacted_at.is_(None),
                    or_(
                        s.message_revisions.c.body_kind.in_(("text", "caption")),
                        exists(
                            select(s.message_media.c.id).where(
                                s.message_media.c.message_revision_id == s.message_revisions.c.id,
                                s.message_media.c.media_kind.in_(("photo", "image_document")),
                            )
                        ),
                    ),
                    s.message_revisions.c.source_event_id > watermark,
                    s.message_revisions.c.source_event_id <= manifest.range_end_event_id,
                )
            )
        ).one()
        estimate = (int(byte_count) + 3) // 4
        if rolling_summary_due(eligible_revision_count=count, estimated_tokens=estimate):
            await MemoryRepository(self.session).refresh_pending_job(
                account_id=manifest.account_id,
                conversation_id=manifest.conversation_id,
                job_kind="rolling_summary",
                event_range=EventRange(watermark + 1, manifest.range_end_event_id),
                estimated_input_tokens=estimate,
                now=now,
            )

    async def _proposals(
        self, prepared: PreparedMemory, proposals: tuple[MemoryProposal, ...], *, now: datetime
    ) -> None:
        repository = MemoryRepository(self.session)
        targets = {
            UUID(item["memory_id"]): item["version_no"]
            for item in json.loads(prepared.user_input.reveal_for_use())["memory_targets"]
        }
        for ordinal, received in enumerate(proposals):
            # A model can omit visual_only or cite a nearby caption for an image
            # inference. Runs that saw pixels never auto-promote any proposal.
            proposal = (
                replace(received, visual_only=True) if prepared.manifest.image_count else received
            )
            proposal = replace(
                proposal,
                evidence=tuple(
                    replace(evidence, visual_only=True)
                    if (source := prepared.manifest.source(evidence.source_id)) is not None
                    and source.visual_only
                    else evidence
                    for evidence in proposal.evidence
                ),
                visual_only=proposal.visual_only
                or any(
                    (source := prepared.manifest.source(evidence.source_id)) is not None
                    and source.visual_only
                    for evidence in proposal.evidence
                ),
            )
            if any(target not in targets for target in proposal.target_memory_ids):
                raise MemoryPipelineError("MEMORY_TARGET_OUTSIDE_INPUT")
            validated = validate_proposal(proposal, prepared.manifest)
            # "accepted" is the durable commit marker, not just a validation
            # verdict. Stage first so accept_validated_proposal writes truth.
            staged = (
                replace(validated, state=ProposalState.VALIDATING)
                if validated.state is ProposalState.ACCEPTED
                else validated
            )
            identity = await repository.record_proposal(
                staged,
                job_id=prepared.lease.id,
                model_run_id=memory_run_id(prepared.lease.id),
                proposal_ordinal=ordinal,
                manifest=prepared.manifest,
                validator_policy_version=prepared.manifest.policy_version,
            )
            if validated.state is ProposalState.ACCEPTED:
                result = await repository.accept_validated_proposal(
                    validated,
                    recorded_proposal_id=identity,
                    expected_versions={
                        target: targets[target] for target in proposal.target_memory_ids
                    },
                    acceptance_kind="reconciliation"
                    if prepared.lease.job_kind == "reconciliation"
                    else "automatic",
                    now=now,
                )
                active = await self.session.scalar(
                    select(s.memory_versions.c.id)
                    .join(s.memories, s.memories.c.id == s.memory_versions.c.memory_id)
                    .where(
                        s.memory_versions.c.id == result.memory_version_id,
                        s.memories.c.status == "active",
                        s.memory_versions.c.rendered_text.is_not(None),
                    )
                )
                if active is not None:
                    await EmbeddingRuntimeRepository(self.session).stage_target(
                        account_id=prepared.manifest.account_id,
                        target_kind="memory_version",
                        target_id=active,
                        now=now,
                    )

    async def _summary(self, prepared: PreparedMemory, output: Any, *, now: datetime) -> None:
        manifest = prepared.manifest
        prior = next(
            (item for item in manifest.sources if item.source_type == "summary_version"), None
        )
        prior_row = (
            (
                await self.session.execute(
                    select(s.summary_versions).where(
                        s.summary_versions.c.id == prior.source_id,
                    )
                )
            )
            .mappings()
            .one()
            if prior
            else None
        )
        if output["summary_text"] is None:
            await self.session.execute(
                insert(s.summary_watermarks)
                .values(
                    account_id=manifest.account_id,
                    conversation_id=manifest.conversation_id,
                    summary_kind="rolling",
                    last_included_event_id=manifest.range_end_event_id,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="pk_summary_watermarks",
                    set_={
                        "last_included_event_id": manifest.range_end_event_id,
                        "version": s.summary_watermarks.c.version + 1,
                        "updated_at": now,
                    },
                )
            )
            return
        sources = tuple(
            item
            for item in manifest.sources
            if item.source_type in {"message_revision", "summary_version"}
        )
        summary = SummaryVersion(
            id=uuid5(prepared.lease.id, "summary-version"),
            summary_id=prior_row["summary_id"]
            if prior_row
            else uuid5(manifest.conversation_id, f"rolling:{prepared.lease.id}"),
            version_no=prior_row["version_no"] + 1 if prior_row else 1,
            kind=SummaryKind.ROLLING,
            range_start_event_id=manifest.range_start_event_id,
            range_end_event_id=manifest.range_end_event_id,
            content_text=output["summary_text"],
            sources=tuple(
                SummarySource(
                    item.source_id,
                    "prior_summary_version"
                    if item.source_type == "summary_version"
                    else "message_revision",
                    item.content_sha256,
                    ordinal,
                )
                for ordinal, item in enumerate(sources, 1)
            ),
            manifest_sha256=manifest.manifest_sha256,
            created_at=now,
        )
        await MemoryRepository(self.session).publish_summary(
            summary,
            account_id=manifest.account_id,
            conversation_id=manifest.conversation_id,
            expected_version=prior_row["version_no"] if prior_row else None,
            model_run_id=memory_run_id(prepared.lease.id),
            pipeline_version=manifest.pipeline_version,
            output_schema_version=manifest.output_schema_version,
            now=now,
        )
        await EmbeddingRuntimeRepository(self.session).stage_target(
            account_id=manifest.account_id,
            target_kind="summary_version",
            target_id=summary.id,
            now=now,
        )
