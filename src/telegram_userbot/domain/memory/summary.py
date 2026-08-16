"""Immutable summary membership and contiguous watermark rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from telegram_userbot.domain.memory.models import SummaryKind, SummaryStatus, SummaryVersion


class SummaryCoverageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SummaryWatermark:
    conversation_id: UUID
    kind: SummaryKind
    last_included_event_id: int = 0
    version: int = 1
    last_summary_version_id: UUID | None = None

    def advance(self, summary: SummaryVersion) -> SummaryWatermark:
        if (
            summary.kind is not self.kind
            or summary.range_start_event_id > self.last_included_event_id + 1
        ):
            raise SummaryCoverageError("summary watermark cannot skip an uncovered event")
        if summary.range_end_event_id < self.last_included_event_id:
            raise SummaryCoverageError("summary watermark cannot move backwards")
        return SummaryWatermark(
            conversation_id=self.conversation_id,
            kind=self.kind,
            last_included_event_id=summary.range_end_event_id,
            version=self.version + 1,
            last_summary_version_id=summary.id,
        )


@dataclass(slots=True)
class SummaryStore:
    versions: dict[UUID, SummaryVersion] = field(default_factory=dict)
    current: dict[UUID, UUID] = field(default_factory=dict)
    conversations: dict[UUID, UUID] = field(default_factory=dict)
    watermarks: dict[tuple[UUID, SummaryKind], SummaryWatermark] = field(default_factory=dict)

    def publish(
        self,
        summary: SummaryVersion,
        *,
        conversation_id: UUID,
        expected_version: int | None = None,
    ) -> SummaryWatermark:
        bound_conversation_id = self.conversations.get(summary.summary_id)
        if bound_conversation_id is not None and bound_conversation_id != conversation_id:
            raise SummaryCoverageError("summary belongs to another conversation")
        key = (conversation_id, summary.kind)
        existing_version = self.versions.get(summary.id)
        if existing_version is not None:
            if not _summary_replay_matches(existing_version, summary):
                raise SummaryCoverageError("summary replay does not match its manifest")
            watermark = self.watermarks.get(key)
            if watermark is None:
                raise SummaryCoverageError("summary replay has no durable watermark")
            return watermark
        current_id = self.current.get(summary.summary_id)
        if current_id is not None:
            current = self.versions[current_id]
            if summary.kind is not current.kind:
                raise SummaryCoverageError("summary kind cannot change across versions")
            if expected_version is not None and current.version_no != expected_version:
                raise SummaryCoverageError("summary current pointer changed")
            if summary.version_no != current.version_no + 1:
                raise SummaryCoverageError("summary versions must be contiguous")
        elif summary.version_no != 1:
            raise SummaryCoverageError("first summary version must be one")
        watermark = self.watermarks.get(
            key,
            SummaryWatermark(conversation_id, summary.kind),
        )
        advanced = watermark.advance(summary)
        self.versions[summary.id] = summary
        self.current[summary.summary_id] = summary.id
        self.conversations[summary.summary_id] = conversation_id
        self.watermarks[key] = advanced
        return advanced

    def invalidate_for_sources(self, source_ids: set[UUID]) -> int:
        count = 0
        for version_id, version in tuple(self.versions.items()):
            if version.status is not SummaryStatus.INVALIDATED and any(
                source.source_id in source_ids for source in version.sources
            ):
                self.versions[version_id] = SummaryVersion(
                    id=version.id,
                    summary_id=version.summary_id,
                    version_no=version.version_no,
                    kind=version.kind,
                    range_start_event_id=version.range_start_event_id,
                    range_end_event_id=version.range_end_event_id,
                    content_text=version.content_text,
                    sources=version.sources,
                    manifest_sha256=version.manifest_sha256,
                    status=SummaryStatus.INVALIDATED,
                    created_at=version.created_at,
                )
                count += 1
        return count


def _summary_replay_matches(existing: SummaryVersion, replay: SummaryVersion) -> bool:
    """Compare immutable publication fields while ignoring a replay timestamp."""

    return (
        existing.id == replay.id
        and existing.summary_id == replay.summary_id
        and existing.version_no == replay.version_no
        and existing.kind is replay.kind
        and existing.range_start_event_id == replay.range_start_event_id
        and existing.range_end_event_id == replay.range_end_event_id
        and existing.content_text == replay.content_text
        and existing.sources == replay.sources
        and existing.manifest_sha256 == replay.manifest_sha256
        and existing.status is replay.status
    )


def rolling_summary_due(*, eligible_revision_count: int, estimated_tokens: int) -> bool:
    return eligible_revision_count >= 50 or estimated_tokens >= 12_000


def summary_period(
    kind: SummaryKind,
    occurred_at: datetime,
    *,
    timezone_name: str = "UTC",
) -> tuple[str, datetime, datetime]:
    """Return a stable local period key and UTC boundaries."""

    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("summary timestamp must be timezone-aware")
    if not timezone_name:
        raise ValueError("summary timezone is required")
    try:
        local = occurred_at.astimezone(ZoneInfo(timezone_name))
    except Exception as exc:
        raise ValueError("summary timezone is invalid") from exc
    if kind is SummaryKind.DAILY:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        return (local.date().isoformat(), start.astimezone(UTC), end.astimezone(UTC))
    if kind is SummaryKind.WEEKLY:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7) - timedelta(microseconds=1)
        return (start.date().isoformat(), start.astimezone(UTC), end.astimezone(UTC))
    utc = local.astimezone(UTC)
    return ("rolling", utc, utc)
