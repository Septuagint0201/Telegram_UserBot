"""Durable Memory Agent claims and immutable inputs, outside provider I/O."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_images import memory_images
from telegram_userbot.adapters.persistence.memory_inputs import (
    MemoryModelSnapshot,
    MemoryPipelineError,
    load_memory_model,
    message_source,
    message_source_query,
    reload_memory_sources,
    require_coverage,
    select_memory_sources,
)
from telegram_userbot.adapters.persistence.memory_periods import MemoryPeriodRepository
from telegram_userbot.adapters.persistence.memory_repository import MemoryJobLease, MemoryRepository
from telegram_userbot.adapters.persistence.model_runtime import RuntimeImageSnapshot
from telegram_userbot.adapters.persistence.records import JobRecord
from telegram_userbot.domain.memory.models import InputManifest
from telegram_userbot.domain.memory.periods import SummaryPeriod
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.shared.redaction import SensitiveValue

ADAPTER_VERSION = "memory-runtime-v4"


def memory_background_id(job_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"telegram-userbot:memory-job:{job_id}")


def memory_run_id(job_id: UUID) -> UUID:
    return uuid5(job_id, "memory-model-run")


def due(row: Any, now: datetime) -> bool:
    return bool(
        row["quiet_until"] <= now
        or row["hard_due_at"] <= now
        or row["eligible_revision_count"] >= 20
        or row["estimated_input_tokens"] >= 6000
    )


@dataclass(frozen=True, slots=True)
class PreparedMemory:
    lease: MemoryJobLease
    manifest: InputManifest = field(repr=False)
    model: MemoryModelSnapshot = field(repr=False)
    user_input: SensitiveValue[str] = field(repr=False)
    input_fingerprint: bytes
    images: tuple[RuntimeImageSnapshot, ...] = field(default=(), repr=False)
    period: SummaryPeriod | None = field(default=None, repr=False)


def input_document(
    manifest: InputManifest,
    targets: list[dict[str, Any]],
    authors: list[dict[str, str]] | None = None,
    period: SummaryPeriod | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": manifest.input_schema_version,
            "output_schema_version": manifest.output_schema_version,
            "range_start_event_id": manifest.range_start_event_id,
            "range_end_event_id": manifest.range_end_event_id,
            "sources": [
                {
                    "source_id": str(item.source_id),
                    "source_revision": item.revision,
                    "source_content_sha256": item.content_sha256.hex(),
                    "source_type": item.source_type,
                    "trust": item.trust.value,
                    "visual_only": item.visual_only,
                    "content": item.content,
                }
                for item in manifest.sources
            ],
            "memory_targets": targets,
            "source_authors": authors or [],
            **({"summary_period": period.document()} if period is not None else {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def input_fingerprint(
    secret: bytes, model: MemoryModelSnapshot, manifest: InputManifest, body: str
) -> bytes:
    return hmac.digest(
        secret,
        b"memory-input-v1\0"
        + ADAPTER_VERSION.encode()
        + b"\0"
        + manifest.manifest_sha256
        + model.config_id.bytes
        + model.credential_version_id.bytes
        + model.capability_sha256
        + model.prompt_sha256
        + body.encode(),
        "sha256",
    )


def manifest_value(row: Any, sources: Any) -> InputManifest:
    return InputManifest(
        id=row["input_manifest_id"],
        account_id=row["account_id"],
        conversation_id=row["conversation_id"],
        generation=row["generation"],
        range_start_event_id=row["range_start_event_id"],
        range_end_event_id=row["range_end_event_id"],
        sources=sources,
        pipeline_version=row["pipeline_version"],
        policy_version=row["policy_version"],
        prompt_version=row["prompt_version"],
        input_schema_version=row["input_schema_version"],
        output_schema_version=row["output_schema_version"],
        input_token_estimate=sum(len(item.content.encode()) for item in sources),
        image_count=sum(item.source_type == "media_object" for item in sources),
    )


def lease_value(row: Any) -> MemoryJobLease:
    return MemoryJobLease(
        **{
            key: row[key]
            for key in (
                "id",
                "account_id",
                "conversation_id",
                "job_kind",
                "generation",
                "range_start_event_id",
                "range_end_event_id",
                "lease_owner",
                "input_manifest_id",
                "pipeline_version",
                "policy_version",
                "prompt_version",
                "input_schema_version",
                "output_schema_version",
            )
        },
        fencing_token=row["job_version"],
    )


class MemoryPipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue_pending(self, *, now: datetime, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("memory compensation limit is invalid")
        older = s.memory_jobs.alias("older_memory_job")
        # One outstanding generation per conversation. This also prevents later
        # quiet-window jobs from advancing a watermark past an unfinished one.
        rows = (
            (
                await self.session.execute(
                    select(s.memory_jobs)
                    .where(
                        s.memory_jobs.c.state.in_(("pending", "running", "retry_wait")),
                        ~exists(
                            select(older.c.id).where(
                                older.c.conversation_id == s.memory_jobs.c.conversation_id,
                                older.c.state.in_(("pending", "running", "retry_wait")),
                                or_(
                                    older.c.created_at < s.memory_jobs.c.created_at,
                                    and_(
                                        older.c.created_at == s.memory_jobs.c.created_at,
                                        older.c.id < s.memory_jobs.c.id,
                                    ),
                                ),
                            )
                        ),
                    )
                    .order_by(s.memory_jobs.c.created_at, s.memory_jobs.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        count = 0
        for candidate in rows:
            await self._account_lock(candidate["account_id"])
            row = (
                (
                    await self.session.execute(
                        select(s.memory_jobs)
                        .where(
                            s.memory_jobs.c.id == candidate["id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            parent = (
                (
                    await self.session.execute(
                        select(s.background_jobs)
                        .where(
                            s.background_jobs.c.id == memory_background_id(row["id"]),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row["state"] not in {"pending", "running", "retry_wait"}:
                continue
            if parent is not None and parent["state"] in {"dead_letter", "cancelled", "failed"}:
                await self._terminal(row, now=now, code="MEMORY_RETRY_EXHAUSTED")
                continue
            if (
                parent is not None
                and parent["state"] == "succeeded"
                and (row["input_manifest_id"] is not None or row["attempt_count"] != 0)
            ):
                await self._terminal(row, now=now, code="MEMORY_PARENT_STATE_MISMATCH")
                continue
            if row["state"] != "pending" or not due(row, now):
                continue
            try:
                await self.require_scope(row)
            except MemoryPipelineError:
                continue
            if parent is None:
                await self.session.execute(
                    insert(s.background_jobs).values(
                        id=memory_background_id(row["id"]),
                        account_id=row["account_id"],
                        queue_name="worker",
                        job_type="memory.generate",
                        max_attempts=5,
                        idempotency_key=hashlib.sha256(b"memory-job:" + row["id"].bytes).digest(),
                        payload_schema_version=1,
                        payload={
                            "memory_job_id": str(row["id"]),
                            "conversation_id": str(row["conversation_id"]),
                        },
                        available_at=now,
                    )
                )
                await self.session.execute(
                    update(s.memory_jobs)
                    .where(s.memory_jobs.c.id == row["id"])
                    .values(
                        background_job_id=memory_background_id(row["id"]),
                    )
                )
                count += 1
            elif (
                parent["state"] == "succeeded"
                and row["input_manifest_id"] is None
                and row["attempt_count"] == 0
            ):
                # A newly arrived message extended quiet_until after publication.
                # No provider attempt occurred, so this wakeup can be rearmed.
                await self.session.execute(
                    update(s.background_jobs)
                    .where(s.background_jobs.c.id == parent["id"])
                    .values(
                        state="pending",
                        attempt_count=0,
                        completed_at=None,
                        available_at=now,
                        version=s.background_jobs.c.version + 1,
                        dispatch_generation=s.background_jobs.c.dispatch_generation + 1,
                        updated_at=now,
                    )
                )
                count += 1
        return count

    async def _account_lock(self, account_id: UUID) -> None:
        await self.session.execute(
            select(s.accounts.c.id)
            .where(s.accounts.c.id == account_id)
            .with_for_update(key_share=True)
        )

    async def fenced_job(self, job: JobRecord, *, now: datetime) -> Any:
        if job.account_id is None or job.job_type != "memory.generate":
            raise MemoryPipelineError("MEMORY_JOB_SCOPE_INVALID")
        await self._account_lock(job.account_id)
        parent = (
            (
                await self.session.execute(
                    select(s.background_jobs)
                    .where(
                        s.background_jobs.c.id == job.id,
                        s.background_jobs.c.account_id == job.account_id,
                        s.background_jobs.c.state == "leased",
                        s.background_jobs.c.job_type == job.job_type,
                        s.background_jobs.c.lease_owner == job.lease_owner,
                        s.background_jobs.c.fencing_token == job.fencing_token,
                        s.background_jobs.c.lease_expires_at > now,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if parent is None:
            raise MemoryPipelineError("WORKER_JOB_FENCE_LOST", retryable=True)
        try:
            if set(parent["payload"]) not in (
                {"memory_job_id", "conversation_id"},
                {"memory_job_id", "conversation_id", "summary_period", "period_sources_sha256"},
            ):
                raise ValueError  # noqa: TRY301 - normalize malformed queue metadata
            identity = UUID(parent["payload"]["memory_job_id"])
            conversation = UUID(parent["payload"]["conversation_id"])
        except ValueError, TypeError, KeyError:
            raise MemoryPipelineError("MEMORY_JOB_SCOPE_INVALID") from None
        if memory_background_id(identity) != job.id:
            raise MemoryPipelineError("MEMORY_JOB_SCOPE_INVALID")
        await self.session.execute(
            select(s.conversations.c.id)
            .where(s.conversations.c.id == conversation)
            .with_for_update(key_share=True)
        )
        row = (
            (
                await self.session.execute(
                    select(s.memory_jobs)
                    .where(
                        s.memory_jobs.c.id == identity,
                        s.memory_jobs.c.account_id == job.account_id,
                        s.memory_jobs.c.conversation_id == conversation,
                        s.memory_jobs.c.background_job_id == job.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise MemoryPipelineError("MEMORY_JOB_SCOPE_INVALID")
        if ("summary_period" in parent["payload"]) != (
            row["job_kind"] == "consolidation" and row["output_schema_version"] == 2
        ):
            raise MemoryPipelineError("MEMORY_JOB_SCOPE_INVALID")
        return row

    async def require_scope(self, row: Any) -> None:
        contact = await self.session.scalar(
            select(s.conversations.c.contact_id).where(
                s.conversations.c.id == row["conversation_id"]
            )
        )
        blocked = await self.session.scalar(
            select(s.data_erasure_requests.c.id)
            .where(
                s.data_erasure_requests.c.account_id == row["account_id"],
                or_(
                    s.data_erasure_requests.c.scope_type == "account",
                    and_(
                        s.data_erasure_requests.c.scope_type == "contact",
                        s.data_erasure_requests.c.contact_id == contact,
                    ),
                ),
            )
            .limit(1)
        )
        if blocked is not None:
            raise MemoryPipelineError("MEMORY_SCOPE_ERASED")

    async def prepare(
        self, job: JobRecord, *, secret: bytes, now: datetime
    ) -> PreparedMemory | None:
        row = await self.fenced_job(job, now=now)
        if row["state"] in {"succeeded", "cancelled", "dead_letter"}:
            return None
        await self.require_scope(row)
        if row["state"] == "pending" and not due(row, now):
            return None
        if (
            row["pipeline_version"],
            row["policy_version"],
            row["input_schema_version"],
            row["output_schema_version"],
        ) != (
            "m6-v1",
            "policy-v1",
            1,
            {
                "episode": 1,
                "reconciliation": 1,
                "rolling_summary": 2,
                "consolidation": 2 if row["output_schema_version"] == 2 else 3,
            }[row["job_kind"]],
        ):
            raise MemoryPipelineError("MEMORY_PIPELINE_UNSUPPORTED")
        start = row["range_start_event_id"]
        if row["input_manifest_id"] is None and row["job_kind"] != "consolidation":
            watermark = await self.watermark(row)
            start = min(start, watermark + 1)
        row = (
            (
                await self.session.execute(
                    update(s.memory_jobs)
                    .where(s.memory_jobs.c.id == row["id"])
                    .values(
                        state="running",
                        lease_owner=job.lease_owner,
                        lease_expires_at=now + timedelta(seconds=660),
                        attempt_count=job.attempt_count,
                        job_version=s.memory_jobs.c.job_version + 1,
                        range_start_event_id=start,
                        updated_at=now,
                    )
                    .returning(s.memory_jobs)
                )
            )
            .mappings()
            .one()
        )
        if row["input_manifest_id"] is None and row["job_kind"] in {
            "episode",
            "rolling_summary",
            "reconciliation",
        }:
            row = await self._partition_backlog(row, now=now)
        prepared = await self._inputs(row, secret=secret, now=now)
        run_id = memory_run_id(row["id"])
        existing = (
            (await self.session.execute(select(s.model_runs).where(s.model_runs.c.id == run_id)))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            await self.session.execute(
                insert(s.model_runs).values(
                    id=run_id,
                    account_id=row["account_id"],
                    conversation_id=row["conversation_id"],
                    memory_job_id=row["id"],
                    logical_role="memory_agent",
                    model_profile_id=prepared.model.config.profile_id,
                    purpose=f"memory_{row['job_kind']}",
                    generation_no=row["generation"],
                    state="running",
                    config_version_id=prepared.model.config_id,
                    credential_version_id=prepared.model.credential_version_id,
                    memory_input_manifest_id=prepared.manifest.id,
                    prompt_version=row["prompt_version"],
                    prompt_bundle_sha256=prepared.model.prompt_sha256,
                    capability_snapshot_sha256=prepared.model.capability_sha256,
                    orchestration_claim_fingerprint=hashlib.sha256(
                        row["id"].bytes + prepared.manifest.manifest_sha256
                    ).digest(),
                    input_fingerprint=prepared.input_fingerprint,
                    adapter_version=ADAPTER_VERSION,
                    request_schema_version=1,
                    output_schema_version=row["output_schema_version"],
                    normalizer_version="v1",
                    started_at=now,
                )
            )
        else:
            if (
                existing["input_fingerprint"] != prepared.input_fingerprint
                or existing["adapter_version"] != ADAPTER_VERSION
            ):
                raise MemoryPipelineError("MEMORY_INPUT_CHANGED")
            await self.session.execute(
                update(s.model_runs)
                .where(s.model_runs.c.id == run_id)
                .values(state="running", error_code=None)
            )
        await self.session.execute(
            update(s.model_run_attempts)
            .where(
                s.model_run_attempts.c.model_run_id == run_id,
                s.model_run_attempts.c.state == "started",
            )
            .values(state="unknown", completed_at=now, error_code="MEMORY_ATTEMPT_LEASE_REPLACED")
        )
        await self.session.execute(
            insert(s.model_run_attempts).values(
                model_run_id=run_id,
                attempt_no=job.attempt_count,
                state="started",
                started_at=now,
            )
        )
        return prepared

    async def _partition_backlog(self, row: Any, *, now: datetime) -> Any:
        rows = (
            (
                await self.session.execute(
                    message_source_query(row["account_id"], row["conversation_id"])
                    .where(
                        s.message_revisions.c.source_event_id.between(
                            row["range_start_event_id"], row["range_end_event_id"]
                        )
                    )
                    .order_by(s.message_revisions.c.source_event_id, s.message_revisions.c.id)
                    .limit(33)
                )
            )
            .mappings()
            .all()
        )
        size, cutoff = 0, None
        for index, source in enumerate(rows):
            size += len(message_source(source).content.encode()) + 512
            if index >= 32 or (index > 0 and size > 12000):
                cutoff = source["source_event_id"] - 1
                break
        if cutoff is None:
            return row
        if cutoff < row["range_start_event_id"]:
            raise MemoryPipelineError("MEMORY_SINGLE_SOURCE_TOO_LARGE")
        updated = (
            (
                await self.session.execute(
                    update(s.memory_jobs)
                    .where(s.memory_jobs.c.id == row["id"])
                    .values(range_end_event_id=cutoff)
                    .returning(s.memory_jobs)
                )
            )
            .mappings()
            .one()
        )
        await MemoryRepository(self.session).refresh_pending_job(
            account_id=row["account_id"],
            conversation_id=row["conversation_id"],
            job_kind=row["job_kind"],
            event_range=EventRange(cutoff + 1, row["range_end_event_id"]),
            estimated_input_tokens=12000,
            now=now,
            pipeline_version=row["pipeline_version"],
            policy_version=row["policy_version"],
            prompt_version=row["prompt_version"],
        )
        return updated

    async def _inputs(self, row: Any, *, secret: bytes, now: datetime) -> PreparedMemory:
        sealed = None
        if row["input_manifest_id"] is not None:
            sealed = (
                (
                    await self.session.execute(
                        select(s.memory_input_manifests).where(
                            s.memory_input_manifests.c.id == row["input_manifest_id"],
                            s.memory_input_manifests.c.scope_erased_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if sealed is None:
                raise MemoryPipelineError("MEMORY_SOURCE_CHANGED")
        model = await load_memory_model(
            self.session,
            prompt_version=row["prompt_version"],
            now=now,
            config_id=sealed["model_config_version_id"] if sealed else None,
            credential_version_id=sealed["credential_version_id"] if sealed else None,
        )
        period = None
        images: tuple[RuntimeImageSnapshot, ...] = ()
        if row["job_kind"] == "consolidation" and row["output_schema_version"] == 2:
            period, sources = await MemoryPeriodRepository(self.session).job_inputs(row, now=now)
        else:
            sources = (
                await reload_memory_sources(self.session, sealed)
                if sealed
                else await select_memory_sources(self.session, row)
            )
            await require_coverage(self.session, row, sources)
            image_sources, images = await memory_images(
                self.session,
                account_id=row["account_id"],
                sources=sources,
                capabilities=model.capabilities,
                now=now,
                event_range=EventRange(row["range_start_event_id"], row["range_end_event_id"]),
            )
            sources = (*sources, *image_sources)
        manifest = manifest_value(
            {
                **dict(row),
                "input_manifest_id": row["input_manifest_id"] or uuid5(row["id"], "memory-input"),
            },
            sources,
        )
        if sealed is not None and (
            sealed["manifest_sha256"] != manifest.manifest_sha256
            or sealed["prompt_bundle_sha256"] != model.prompt_sha256
            or sealed["capability_snapshot_sha256"] != model.capability_sha256
        ):
            raise MemoryPipelineError("MEMORY_INPUT_CHANGED")
        target_ids = [item.source_id for item in sources if item.source_type == "memory_version"]
        targets = (
            await self.session.execute(
                select(
                    s.memory_versions.c.id,
                    s.memory_versions.c.memory_id,
                    s.memory_versions.c.version_no,
                )
                .where(s.memory_versions.c.id.in_(target_ids))
                .order_by(s.memory_versions.c.memory_id)
            )
        ).all()
        blocked = await self.session.scalar(
            select(s.data_erasure_requests.c.id)
            .where(
                s.data_erasure_requests.c.account_id == row["account_id"],
                s.data_erasure_requests.c.scope_type == "memory",
                s.data_erasure_requests.c.memory_id.in_([item.memory_id for item in targets]),
            )
            .limit(1)
        )
        if blocked is not None:
            raise MemoryPipelineError("MEMORY_SOURCE_CHANGED")
        author_rows = (
            await self.session.execute(
                select(
                    s.message_revisions.c.id,
                    s.messages.c.role,
                    s.messages.c.source,
                    s.messages.c.direction,
                )
                .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
                .where(
                    s.message_revisions.c.id.in_(
                        [
                            item.source_id
                            for item in sources
                            if item.source_type == "message_revision"
                        ]
                    ),
                )
                .order_by(s.message_revisions.c.id)
            )
        ).all()
        body = input_document(
            manifest,
            [
                {
                    "memory_id": str(item.memory_id),
                    "version_id": str(item.id),
                    "version_no": item.version_no,
                }
                for item in targets
            ],
            [
                {
                    "source_id": str(item.id),
                    "role": item.role,
                    "origin": item.source,
                    "direction": item.direction,
                }
                for item in author_rows
            ],
            period,
        )
        # UTF-8 bytes are a conservative upper estimate, avoiding silent input
        # truncation when an exact model tokenizer is not available.
        if (
            len(body.encode())
            + len(model.prompt.reveal_for_use().encode())
            + 2048
            + len(images) * model.capabilities.auto_image_tokens
            + (model.config.max_output_tokens or 0)
            > model.capabilities.max_context_tokens
        ):
            raise MemoryPipelineError("MEMORY_INPUT_LIMIT_EXCEEDED")
        if sealed is None:
            await MemoryRepository(self.session).create_manifest(
                manifest,
                lease=lease_value(row),
                now=now,
                model_config_version_id=model.config_id,
                credential_version_id=model.credential_version_id,
                prompt_bundle_sha256=model.prompt_sha256,
                capability_snapshot_sha256=model.capability_sha256,
                timezone_snapshot=period.timezone if period else None,
            )
        return PreparedMemory(
            lease_value(row),
            manifest,
            model,
            SensitiveValue(body),
            input_fingerprint(secret, model, manifest, body),
            images,
            period,
        )

    async def verify_completion(
        self, job: JobRecord, prepared: PreparedMemory, *, secret: bytes, now: datetime
    ) -> Any:
        row = await self.fenced_job(job, now=now)
        if (
            row["state"] != "running"
            or row["lease_owner"] != prepared.lease.lease_owner
            or row["job_version"] != prepared.lease.fencing_token
            or row["lease_expires_at"] <= now
        ):
            raise MemoryPipelineError("MEMORY_JOB_FENCE_LOST", retryable=True)
        await self.require_scope(row)
        current = await self._inputs(row, secret=secret, now=now)
        if current.input_fingerprint != prepared.input_fingerprint:
            raise MemoryPipelineError("MEMORY_INPUT_CHANGED")
        return row

    async def watermark(self, row: Any) -> int:
        if row["job_kind"] == "rolling_summary":
            value = await self.session.scalar(
                select(s.summary_watermarks.c.last_included_event_id).where(
                    s.summary_watermarks.c.conversation_id == row["conversation_id"],
                    s.summary_watermarks.c.summary_kind == "rolling",
                )
            )
        else:
            value = await self.session.scalar(
                select(s.memory_watermarks.c.last_contiguous_decided_event_id).where(
                    s.memory_watermarks.c.conversation_id == row["conversation_id"],
                    s.memory_watermarks.c.watermark_kind
                    == (row["job_kind"] if row["job_kind"] != "consolidation" else "episode"),
                )
            )
        return int(value or 0)

    async def fail(
        self,
        job: JobRecord,
        *,
        code: str,
        retryable: bool,
        now: datetime,
        expected: MemoryJobLease | None = None,
    ) -> None:
        row = await self.fenced_job(job, now=now)
        if row["state"] in {"succeeded", "cancelled", "dead_letter"}:
            return
        if expected is not None and (
            row["lease_owner"] != expected.lease_owner
            or row["job_version"] != expected.fencing_token
        ):
            raise MemoryPipelineError("MEMORY_JOB_FENCE_LOST", retryable=True)
        terminal = not retryable or job.attempt_count >= job.max_attempts
        if terminal:
            await self._terminal(row, now=now, code=code)
        else:
            await self.session.execute(
                update(s.memory_jobs)
                .where(s.memory_jobs.c.id == row["id"])
                .values(
                    state="retry_wait",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    job_version=s.memory_jobs.c.job_version + 1,
                )
            )
            await self.session.execute(
                update(s.model_runs)
                .where(s.model_runs.c.id == memory_run_id(row["id"]))
                .values(state="retry_wait", error_code=code)
            )
        await self.session.execute(
            update(s.model_run_attempts)
            .where(
                s.model_run_attempts.c.model_run_id == memory_run_id(row["id"]),
                s.model_run_attempts.c.attempt_no == job.attempt_count,
                s.model_run_attempts.c.state == "started",
            )
            .values(
                state="terminal_failed" if terminal else "retryable_failed",
                completed_at=now,
                error_code=code,
            )
        )

    async def _terminal(self, row: Any, *, now: datetime, code: str) -> None:
        await self.session.execute(
            update(s.memory_jobs)
            .where(s.memory_jobs.c.id == row["id"])
            .values(
                state="dead_letter",
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
                job_version=s.memory_jobs.c.job_version + 1,
            )
        )
        await self.session.execute(
            update(s.model_runs)
            .where(s.model_runs.c.id == memory_run_id(row["id"]))
            .values(state="failed", error_code=code, completed_at=now)
        )
