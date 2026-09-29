"""Bounded calendar scans and sealed daily/weekly inputs for Memory Agent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_inputs import (
    MemoryPipelineError,
    message_source,
    message_source_query,
)
from telegram_userbot.domain.memory.models import (
    InputManifest,
    InputSource,
    SummaryKind,
    TrustClass,
)
from telegram_userbot.domain.memory.periods import SummaryPeriod, summary_timezone

UNFINISHED = ("pending", "leased", "running", "retry_wait")


class PeriodDependencyError(MemoryPipelineError):
    def __init__(
        self, period: SummaryPeriod, sources: tuple[InputSource, ...], start: int, end: int
    ) -> None:
        super().__init__("MEMORY_DAILY_INCOMPLETE", retryable=True)
        self.period, self.sources, self.start, self.end = period, sources, start, end


def sources_digest(sources: tuple[InputSource, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            [(str(item.source_id), item.revision, item.content_sha256.hex()) for item in sources],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class MemoryPeriodRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def repartition_history(
        self, *, account_id: UUID, conversation_id: UUID, timezone: str, now: datetime
    ) -> int:
        """Explicitly retire old partitions after changing the configured timezone."""
        summary_timezone(timezone, None, "UTC")
        await self.session.execute(
            select(s.accounts.c.id).where(s.accounts.c.id == account_id).with_for_update()
        )
        scope = (
            await self.session.execute(
                select(s.conversations.c.id, s.contacts.c.timezone, s.accounts.c.default_timezone)
                .join(s.contacts, s.contacts.c.id == s.conversations.c.contact_id)
                .join(s.accounts, s.accounts.c.id == s.conversations.c.account_id)
                .where(
                    s.conversations.c.id == conversation_id,
                    s.conversations.c.account_id == account_id,
                )
                .with_for_update(of=s.conversations)
            )
        ).one()
        if summary_timezone(scope.timezone, scope.default_timezone, timezone) != timezone:
            raise MemoryPipelineError("MEMORY_REBUILD_TIMEZONE_MISMATCH")
        rows = (
            await self.session.execute(
                select(
                    s.summaries.c.id,
                    s.summary_versions.c.id.label("version_id"),
                    s.summaries.c.period_key,
                )
                .join(
                    s.summary_versions,
                    (s.summary_versions.c.summary_id == s.summaries.c.id)
                    & (s.summary_versions.c.version_no == s.summaries.c.current_version_no),
                )
                .where(
                    s.summaries.c.conversation_id == conversation_id,
                    s.summaries.c.summary_kind.in_(("daily", "weekly")),
                    s.summaries.c.timezone_snapshot != timezone,
                    ~s.summaries.c.period_key.startswith("repartitioned:"),
                )
            )
        ).all()
        for row in rows:
            await self.invalidate(row.version_id, now=now)
            await self.session.execute(
                update(s.summaries)
                .where(s.summaries.c.id == row.id)
                .values(period_key=f"repartitioned:{row.id}:{row.period_key}")
            )
        jobs = (
            (
                await self.session.execute(
                    select(
                        s.memory_jobs.c.id,
                        s.memory_jobs.c.background_job_id,
                        s.background_jobs.c.payload,
                    )
                    .join(
                        s.background_jobs,
                        s.background_jobs.c.id == s.memory_jobs.c.background_job_id,
                    )
                    .where(
                        s.memory_jobs.c.conversation_id == conversation_id,
                        s.memory_jobs.c.job_kind == "consolidation",
                        s.memory_jobs.c.output_schema_version == 2,
                    )
                )
            )
            .mappings()
            .all()
        )
        for job in jobs:
            payload = dict(job["payload"])
            if payload.get("summary_period", {}).get("timezone") in {None, timezone}:
                continue
            payload["period_repartitioned"] = True
            await self.session.execute(
                update(s.background_jobs)
                .where(s.background_jobs.c.id == job["background_job_id"])
                .values(
                    payload=payload,
                    state="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await self.session.execute(
                update(s.memory_jobs)
                .where(s.memory_jobs.c.id == job["id"], s.memory_jobs.c.state.in_(UNFINISHED))
                .values(
                    state="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
        return len(rows)

    async def ready(self, conversation: UUID, *, now: datetime) -> bool:
        pending = await self.session.scalar(
            select(s.message_events.c.id)
            .where(
                s.message_events.c.conversation_id == conversation,
                s.message_events.c.projected_at.is_(None),
            )
            .limit(1)
        )
        origin = await self.session.scalar(
            select(s.messages.c.id)
            .where(
                s.messages.c.conversation_id == conversation,
                s.messages.c.source_status == "pending",
                s.messages.c.deleted_at.is_(None),
            )
            .limit(1)
        )
        latest = await self.session.scalar(
            select(func.max(s.message_events.c.observed_at)).where(
                s.message_events.c.conversation_id == conversation
            )
        )
        return (
            pending is None
            and origin is None
            and (latest is None or latest <= now - timedelta(minutes=15))
        )

    async def snapshots(self, conversation: UUID) -> tuple[SummaryPeriod, ...]:
        published = (
            (
                await self.session.execute(
                    select(s.summaries).where(
                        s.summaries.c.conversation_id == conversation,
                        s.summaries.c.summary_kind.in_(("daily", "weekly")),
                        s.summaries.c.scope_erased_at.is_(None),
                        s.summaries.c.period_start_at.is_not(None),
                        s.summaries.c.period_end_at.is_not(None),
                        s.summaries.c.timezone_snapshot.is_not(None),
                        ~s.summaries.c.period_key.contains(":part:"),
                        ~s.summaries.c.period_key.startswith("repartitioned:"),
                    )
                )
            )
            .mappings()
            .all()
        )
        periods: dict[tuple[SummaryKind, str], SummaryPeriod] = {}
        for row in published:
            period = SummaryPeriod.at(
                SummaryKind(row["summary_kind"]), row["period_start_at"], row["timezone_snapshot"]
            )
            if (period.start, period.end, period.identity) != (
                row["period_start_at"],
                row["period_end_at"],
                row["period_key"],
            ):
                raise MemoryPipelineError("MEMORY_PERIOD_INVALID")
            periods[(period.kind, period.identity)] = period
        rows = (
            (
                await self.session.execute(
                    select(s.background_jobs.c.payload)
                    .join(
                        s.memory_jobs, s.memory_jobs.c.background_job_id == s.background_jobs.c.id
                    )
                    .where(
                        s.memory_jobs.c.conversation_id == conversation,
                        s.memory_jobs.c.output_schema_version == 2,
                        s.memory_jobs.c.job_kind == "consolidation",
                    )
                    .order_by(s.memory_jobs.c.created_at, s.memory_jobs.c.id)
                )
            )
            .scalars()
            .all()
        )
        for payload in rows:
            if "summary_period" in payload:
                period = SummaryPeriod.parse(payload["summary_period"])
                if period.partition is None and not payload.get("period_repartitioned"):
                    periods.setdefault((period.kind, period.identity), period)
        return tuple(periods.values())

    async def daily_sources(
        self, account: UUID, conversation: UUID, period: SummaryPeriod
    ) -> tuple[tuple[InputSource, ...], int, int]:
        query = message_source_query(account, conversation).where(
            s.messages.c.telegram_created_at.between(period.start, period.end)
        )
        if period.partition is not None:
            power, bucket = period.partition
            query = query.where(
                s.message_revisions.c.source_event_id.between(
                    bucket * 16**power, (bucket + 1) * 16**power - 1
                )
            )
        rows = (
            (
                await self.session.execute(
                    query.order_by(
                        s.message_revisions.c.source_event_id, s.message_revisions.c.id
                    ).limit(33)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 32 or sum(len(message_source(row).content.encode()) for row in rows) > 12000:
            maximum = await self.session.scalar(
                select(func.max(query.subquery().c.source_event_id))
            )
            power = period.partition[0] - 1 if period.partition else 0
            if period.partition is None:
                while 16 ** (power + 1) <= maximum:
                    power += 1
            if power < 0:
                raise MemoryPipelineError("MEMORY_SINGLE_SOURCE_TOO_LARGE")
            scoped = query.subquery()
            bucket_column = func.floor(scoped.c.source_event_id / (16**power)).label("bucket")
            buckets = (
                (
                    await self.session.execute(
                        select(bucket_column).distinct().order_by(bucket_column)
                    )
                )
                .scalars()
                .all()
            )
            combined: list[InputSource] = []
            starts, ends = [], []
            for bucket in buckets:
                part = replace(period, partition=(power, int(bucket)))
                roots, start, end = await self.daily_sources(account, conversation, part)
                current = await self.current(conversation, part)
                if not await self.matches(current, roots):
                    raise PeriodDependencyError(part, roots, start, end)
                combined.append(
                    InputSource(
                        current["id"],
                        f"version-{current['version_no']}",
                        current["content_text"],
                        current["content_sha256"],
                        "summary_version",
                        TrustClass.TRUSTED_DERIVED,
                    )
                )
                starts.append(start)
                ends.append(end)
            return tuple(combined), min(starts), max(ends)
        events = [row["source_event_id"] for row in rows]
        return (
            tuple(message_source(row) for row in rows),
            min(events, default=1),
            max(events, default=1),
        )

    async def current(self, conversation: UUID, period: SummaryPeriod) -> Any:
        return (
            (
                await self.session.execute(
                    select(s.summary_versions, s.summaries.c.status)
                    .join(s.summaries, s.summaries.c.id == s.summary_versions.c.summary_id)
                    .where(
                        s.summaries.c.conversation_id == conversation,
                        s.summaries.c.summary_kind == period.kind.value,
                        or_(
                            s.summaries.c.period_key == period.identity,
                            s.summaries.c.id
                            == uuid5(
                                conversation, f"summary:{period.kind.value}:{period.identity}"
                            ),
                        ),
                        s.summary_versions.c.version_no == s.summaries.c.current_version_no,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def matches(self, current: Any, sources: tuple[InputSource, ...]) -> bool:
        if (
            current is None
            or current["status"] != "active"
            or current["invalidation_state"] != "active"
        ):
            return False
        rows = (
            (
                await self.session.execute(
                    select(s.summary_version_sources)
                    .where(s.summary_version_sources.c.summary_version_id == current["id"])
                    .order_by(s.summary_version_sources.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        return [
            (
                row["message_revision_id"] or row["prior_summary_version_id"],
                row["source_content_sha256"],
            )
            for row in rows
        ] == [(item.source_id, item.content_sha256) for item in sources]

    async def inputs(
        self, account: UUID, conversation: UUID, period: SummaryPeriod
    ) -> tuple[tuple[InputSource, ...], int, int]:
        if period.kind is SummaryKind.DAILY:
            return await self.daily_sources(account, conversation, period)
        snapshots = await self.snapshots(conversation)
        sources: list[InputSource] = []
        starts, ends = [], []
        # Each nonempty day must have a current summary with exactly its current
        # canonical membership. Empty days are checked again after provider I/O.
        day_start = period.start
        while day_start <= period.end:
            day = next(
                (
                    item
                    for item in snapshots
                    if item.kind is SummaryKind.DAILY and item.contains(day_start)
                ),
                None,
            )
            day = day or SummaryPeriod.at(SummaryKind.DAILY, day_start, period.timezone)
            if day.start != day_start or day.end > period.end:
                raise MemoryPipelineError("MEMORY_PERIOD_TIMEZONE_CONFLICT")
            roots, start, end = await self.daily_sources(account, conversation, day)
            if roots:
                current = await self.current(conversation, day)
                if not await self.matches(current, roots):
                    raise MemoryPipelineError("MEMORY_DAILY_INCOMPLETE", retryable=True)
                sources.append(
                    InputSource(
                        current["id"],
                        f"version-{current['version_no']}",
                        current["content_text"],
                        current["content_sha256"],
                        "summary_version",
                        TrustClass.TRUSTED_DERIVED,
                    )
                )
                starts.append(start)
                ends.append(end)
            day_start = day.end + timedelta(microseconds=1)
        return tuple(sources), min(starts, default=1), max(ends, default=1)

    async def invalidate(self, version: UUID, *, now: datetime) -> None:
        affected = {version}
        frontier = {version}
        while frontier:
            children = (
                set(
                    (
                        await self.session.execute(
                            select(s.summary_version_sources.c.summary_version_id).where(
                                s.summary_version_sources.c.prior_summary_version_id.in_(frontier)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                - affected
            )
            affected.update(children)
            frontier = children
        await self.session.execute(
            update(s.summary_versions)
            .where(s.summary_versions.c.id.in_(affected))
            .values(invalidation_state="invalidated")
        )
        await self.session.execute(
            update(s.summaries)
            .where(
                exists(
                    select(s.summary_versions.c.id).where(
                        s.summary_versions.c.id.in_(affected),
                        s.summary_versions.c.summary_id == s.summaries.c.id,
                        s.summary_versions.c.version_no == s.summaries.c.current_version_no,
                    )
                )
            )
            .values(status="invalidated", updated_at=now)
        )
        await self.session.execute(
            update(s.embedding_records)
            .where(s.embedding_records.c.summary_version_id.in_(affected))
            .values(state="invalidated", invalidated_at=now)
        )

    async def scan(
        self,
        *,
        now: datetime,
        deployment_timezone: str,
        after: UUID | None = None,
        limit: int = 100,
    ) -> tuple[int, UUID | None]:
        if not 1 <= limit <= 1000:
            raise ValueError("calendar scan limit is invalid")
        query = (
            select(
                s.conversations.c.id,
                s.conversations.c.account_id,
                s.contacts.c.timezone,
                s.accounts.c.default_timezone,
            )
            .join(s.contacts, s.contacts.c.id == s.conversations.c.contact_id)
            .join(s.accounts, s.accounts.c.id == s.conversations.c.account_id)
            .where(
                s.conversations.c.deleted_at.is_(None),
                s.conversations.c.metadata_erased_at.is_(None),
            )
            .order_by(s.conversations.c.id)
            .limit(limit)
        )
        if after is not None:
            query = query.where(s.conversations.c.id > after)
        rows = (await self.session.execute(query)).mappings().all()
        count = 0
        for row in rows:
            # Savepoints isolate an invalid timezone/oversized conversation from
            # the rest of the bounded scan; durable failures never reset budgets.
            try:
                async with self.session.begin_nested():
                    count += await self.scan_conversation(
                        row, now=now, deployment_timezone=deployment_timezone
                    )
            except MemoryPipelineError, ValueError:
                continue
        return count, rows[-1]["id"] if len(rows) == limit else None

    async def scan_conversation(  # noqa: PLR0912 - ordered admission and invalidation gates
        self, row: Any, *, now: datetime, deployment_timezone: str
    ) -> int:
        account, conversation = row["account_id"], row["id"]
        await self.session.execute(
            select(s.accounts.c.id)
            .where(s.accounts.c.id == account)
            .with_for_update(key_share=True)
        )
        await self.session.execute(
            select(s.conversations.c.id)
            .where(s.conversations.c.id == conversation)
            .with_for_update(key_share=True)
        )
        erased = await self.session.scalar(
            select(s.data_erasure_requests.c.id)
            .where(
                s.data_erasure_requests.c.account_id == account,
                or_(
                    s.data_erasure_requests.c.scope_type == "account",
                    (s.data_erasure_requests.c.scope_type == "contact")
                    & (
                        s.data_erasure_requests.c.contact_id
                        == select(s.conversations.c.contact_id)
                        .where(s.conversations.c.id == conversation)
                        .scalar_subquery()
                    ),
                ),
            )
            .limit(1)
        )
        if erased is not None:
            return 0
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"memory_job:{account}:{conversation}:consolidation"},
        )
        timezone = summary_timezone(row["timezone"], row["default_timezone"], deployment_timezone)
        snapshots = await self.snapshots(conversation)
        dates = (
            (
                await self.session.execute(
                    select(func.min(s.messages.c.telegram_created_at))
                    .where(
                        s.messages.c.conversation_id == conversation,
                        s.messages.c.deleted_at.is_(None),
                        s.messages.c.is_tombstone.is_(False),
                    )
                    .group_by(
                        func.date_trunc(
                            "day", func.timezone(timezone, s.messages.c.telegram_created_at)
                        )
                    )
                    .order_by(func.min(s.messages.c.telegram_created_at))
                )
            )
            .scalars()
            .all()
        )
        periods = {(p.kind, p.identity): p for p in snapshots}
        for occurred in dates:
            day = next(
                (p for p in snapshots if p.kind is SummaryKind.DAILY and p.contains(occurred)), None
            )
            if day is None:
                day = SummaryPeriod.at(SummaryKind.DAILY, occurred, timezone)
                if any(
                    p.kind is SummaryKind.DAILY and p.start <= day.end and day.start <= p.end
                    for p in snapshots
                ):
                    # A timezone edit cannot silently repartition an existing period.
                    raise MemoryPipelineError("MEMORY_PERIOD_TIMEZONE_CONFLICT")
            periods[(day.kind, day.identity)] = day
            week = SummaryPeriod.at(SummaryKind.WEEKLY, day.start, day.timezone)
            periods[(week.kind, week.identity)] = week
        ready = await self.ready(conversation, now=now)
        busy = await self.session.scalar(
            select(s.memory_jobs.c.id)
            .where(
                s.memory_jobs.c.conversation_id == conversation,
                s.memory_jobs.c.state.in_(UNFINISHED),
            )
            .limit(1)
        )
        enqueued = 0
        for period in sorted(
            periods.values(), key=lambda p: (p.kind is SummaryKind.WEEKLY, p.start, p.identity)
        ):
            if period.end >= now:
                continue
            current = await self.current(conversation, period)
            try:
                sources, start, end = await self.inputs(account, conversation, period)
            except MemoryPipelineError as error:
                if error.code != "MEMORY_DAILY_INCOMPLETE":
                    raise
                if current is not None:
                    await self.invalidate(current["id"], now=now)
                if (
                    isinstance(error, PeriodDependencyError)
                    and ready
                    and busy is None
                    and not enqueued
                ):
                    stale = await self.current(conversation, error.period)
                    if stale is not None:
                        await self.invalidate(stale["id"], now=now)
                    enqueued += await self.enqueue(
                        account,
                        conversation,
                        error.period,
                        error.sources,
                        error.start,
                        error.end,
                        now=now,
                    )
                continue
            if await self.matches(current, sources):
                continue
            if current is not None:
                await self.invalidate(current["id"], now=now)
            if not sources or not ready or busy is not None or enqueued:
                continue
            enqueued += await self.enqueue(
                account, conversation, period, sources, start, end, now=now
            )
        return enqueued

    async def enqueue(  # noqa: PLR0913, PLR0917 - explicit immutable calendar job inputs
        self,
        account: UUID,
        conversation: UUID,
        period: SummaryPeriod,
        sources: tuple[InputSource, ...],
        start: int,
        end: int,
        *,
        now: datetime,
    ) -> int:
        digest = sources_digest(sources)
        current = await self.current(conversation, period)
        prior_id = str(current["id"]) if current is not None else "initial"
        identity = uuid5(
            conversation, f"period:{period.kind.value}:{period.identity}:{digest}:{prior_id}"
        )
        if await self.session.scalar(
            select(s.memory_jobs.c.id).where(s.memory_jobs.c.id == identity)
        ):
            return 0
        generation = (
            await self.session.scalar(
                select(func.max(s.memory_jobs.c.generation)).where(
                    s.memory_jobs.c.conversation_id == conversation,
                    s.memory_jobs.c.job_kind == "consolidation",
                )
            )
            or 0
        ) + 1
        parent = uuid5(NAMESPACE_URL, f"telegram-userbot:memory-job:{identity}")
        await self.session.execute(
            insert(s.background_jobs).values(
                id=parent,
                account_id=account,
                queue_name="worker",
                job_type="memory.generate",
                max_attempts=5,
                idempotency_key=hashlib.sha256(b"memory-job:" + identity.bytes).digest(),
                payload_schema_version=1,
                payload={
                    "memory_job_id": str(identity),
                    "conversation_id": str(conversation),
                    "summary_period": period.document(),
                    "period_sources_sha256": digest,
                },
                available_at=now,
            )
        )
        await self.session.execute(
            insert(s.memory_jobs).values(
                id=identity,
                account_id=account,
                conversation_id=conversation,
                job_kind="consolidation",
                state="pending",
                generation=generation,
                range_start_event_id=start,
                range_end_event_id=end,
                eligible_revision_count=len(sources),
                estimated_input_tokens=sum(len(s.content.encode()) for s in sources),
                idempotency_key=hashlib.sha256(identity.bytes).digest(),
                quiet_until=now,
                hard_due_at=now,
                pipeline_version="m6-v1",
                policy_version="policy-v1",
                prompt_version="prompt-v1",
                input_schema_version=1,
                output_schema_version=2,
                background_job_id=parent,
                created_at=now,
                updated_at=now,
            )
        )
        return 1

    async def job_inputs(
        self, row: Any, *, now: datetime
    ) -> tuple[SummaryPeriod, tuple[InputSource, ...]]:
        payload = await self.session.scalar(
            select(s.background_jobs.c.payload).where(
                s.background_jobs.c.id == row["background_job_id"]
            )
        )
        if payload is None:
            raise MemoryPipelineError("MEMORY_PERIOD_INVALID")
        try:
            period = SummaryPeriod.parse(payload["summary_period"])
            digest = payload["period_sources_sha256"]
        except KeyError, TypeError, ValueError:
            raise MemoryPipelineError("MEMORY_PERIOD_INVALID") from None
        if period.end >= now or not await self.ready(row["conversation_id"], now=now):
            raise MemoryPipelineError("MEMORY_PERIOD_NOT_READY", retryable=True)
        try:
            sources, start, end = await self.inputs(
                row["account_id"], row["conversation_id"], period
            )
        except MemoryPipelineError as error:
            if error.code == "MEMORY_DAILY_INCOMPLETE":
                raise MemoryPipelineError("MEMORY_PERIOD_CHANGED") from None
            raise
        if (
            not sources
            or sources_digest(sources) != digest
            or (start, end) != (row["range_start_event_id"], row["range_end_event_id"])
        ):
            raise MemoryPipelineError("MEMORY_PERIOD_CHANGED")
        return period, sources

    async def publish(
        self,
        manifest: InputManifest,
        period: SummaryPeriod,
        content: str | None,
        *,
        job_id: UUID,
        now: datetime,
    ) -> UUID:
        if content is None:
            # Every scheduled period is nonempty. A no-change result must not
            # mark missing/stale daily coverage complete for a weekly summary.
            raise MemoryPipelineError("MEMORY_PERIOD_SUMMARY_REQUIRED")
        current = await self.current(manifest.conversation_id, period)
        version = current["version_no"] + 1 if current is not None else 1
        summary_id = uuid5(
            manifest.conversation_id, f"summary:{period.kind.value}:{period.identity}"
        )
        version_id = uuid5(job_id, "summary-version")
        if current is not None:
            await self.invalidate(current["id"], now=now)
            await self.session.execute(
                update(s.summaries)
                .where(s.summaries.c.id == summary_id)
                .values(
                    current_version_no=version,
                    period_key=period.identity,
                    status="active",
                    updated_at=now,
                )
            )
        else:
            await self.session.execute(
                insert(s.summaries).values(
                    id=summary_id,
                    account_id=manifest.account_id,
                    conversation_id=manifest.conversation_id,
                    summary_kind=period.kind.value,
                    period_key=period.identity,
                    timezone_snapshot=period.timezone,
                    period_start_at=period.start,
                    period_end_at=period.end,
                    status="active",
                    current_version_no=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self.session.execute(
            insert(s.summary_versions).values(
                id=version_id,
                account_id=manifest.account_id,
                summary_id=summary_id,
                version_no=version,
                range_start_event_id=manifest.range_start_event_id,
                range_end_event_id=manifest.range_end_event_id,
                period_start_at=period.start,
                period_end_at=period.end,
                timezone_snapshot=period.timezone,
                content_text=content,
                content_sha256=hashlib.sha256(content.encode()).digest(),
                model_run_id=uuid5(job_id, "memory-model-run"),
                model_role="memory_agent",
                prompt_version=manifest.prompt_version,
                pipeline_version=manifest.pipeline_version,
                output_schema_version=manifest.output_schema_version,
                manifest_sha256=manifest.manifest_sha256,
                invalidation_state="active",
                created_at=now,
            )
        )
        for ordinal, source in enumerate(manifest.sources, 1):
            await self.session.execute(
                insert(s.summary_version_sources).values(
                    summary_version_id=version_id,
                    account_id=manifest.account_id,
                    ordinal=ordinal,
                    message_revision_id=source.source_id
                    if source.source_type == "message_revision"
                    else None,
                    prior_summary_version_id=source.source_id
                    if source.source_type == "summary_version"
                    else None,
                    inclusion_role="episode",
                    source_content_sha256=source.content_sha256,
                    created_at=now,
                )
            )
        return version_id
