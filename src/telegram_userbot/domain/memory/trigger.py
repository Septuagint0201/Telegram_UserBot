"""Asynchronous memory trigger, range merge, and compensation policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from telegram_userbot.domain.shared.time import require_aware


class TriggerReason(StrEnum):
    QUIET_WINDOW = "quiet_window"
    REVISION_BATCH = "revision_batch"
    TOKEN_BATCH = "token_batch"  # noqa: S105 - trigger reason, not a credential
    HARD_DEADLINE = "hard_deadline"
    COMPENSATION_SCAN = "compensation_scan"
    OUTGOING_NON_AUTO = "outgoing_non_auto"


@dataclass(frozen=True, slots=True)
class TriggerPolicy:
    quiet_window: timedelta = timedelta(seconds=45)
    revision_threshold: int = 20
    token_threshold: int = 6_000
    hard_deadline: timedelta = timedelta(minutes=10)
    compensation_interval: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if (
            self.quiet_window <= timedelta(0)
            or self.revision_threshold <= 0
            or self.token_threshold <= 0
            or self.hard_deadline <= timedelta(0)
            or self.compensation_interval <= timedelta(0)
        ):
            raise ValueError("memory trigger thresholds must be positive")


@dataclass(frozen=True, slots=True)
class TriggerInput:
    now: datetime
    last_eligible_at: datetime | None
    oldest_uncovered_at: datetime | None
    eligible_revision_count: int
    estimated_input_tokens: int
    last_compensation_scan_at: datetime | None = None
    eligible_outgoing: bool = False
    auto_mode: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", require_aware(self.now, "now"))
        for name in ("last_eligible_at", "oldest_uncovered_at", "last_compensation_scan_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_aware(value, name))
        if self.eligible_revision_count < 0 or self.estimated_input_tokens < 0:
            raise ValueError("trigger counters cannot be negative")


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    due: bool
    reasons: tuple[TriggerReason, ...] = ()


_DEFAULT_POLICY = TriggerPolicy()


def evaluate_triggers(value: TriggerInput, policy: TriggerPolicy | None = None) -> TriggerDecision:
    """Evaluate independent OR conditions; no condition waits for Main AI."""

    policy = policy or _DEFAULT_POLICY
    reasons: list[TriggerReason] = []
    if (
        value.last_eligible_at is not None
        and value.now >= value.last_eligible_at + policy.quiet_window
    ):
        reasons.append(TriggerReason.QUIET_WINDOW)
    if value.eligible_revision_count >= policy.revision_threshold:
        reasons.append(TriggerReason.REVISION_BATCH)
    if value.estimated_input_tokens >= policy.token_threshold:
        reasons.append(TriggerReason.TOKEN_BATCH)
    if (
        value.oldest_uncovered_at is not None
        and value.now >= value.oldest_uncovered_at + policy.hard_deadline
    ):
        reasons.append(TriggerReason.HARD_DEADLINE)
    if (
        value.last_compensation_scan_at is None
        or value.now >= value.last_compensation_scan_at + policy.compensation_interval
    ) and (value.eligible_revision_count > 0 or value.oldest_uncovered_at is not None):
        reasons.append(TriggerReason.COMPENSATION_SCAN)
    if value.eligible_outgoing and not value.auto_mode:
        reasons.append(TriggerReason.OUTGOING_NON_AUTO)
    return TriggerDecision(bool(reasons), tuple(dict.fromkeys(reasons)))


@dataclass(frozen=True, slots=True, order=True)
class EventRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("event range is invalid")

    def overlaps_or_touches(self, other: EventRange) -> bool:
        return self.start <= other.end + 1 and other.start <= self.end + 1

    def merge(self, other: EventRange) -> EventRange:
        if not self.overlaps_or_touches(other):
            raise ValueError("disjoint event ranges cannot be merged")
        return EventRange(min(self.start, other.start), max(self.end, other.end))


class RangeState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class MemoryGeneration:
    generation: int
    event_range: EventRange
    state: RangeState = RangeState.PENDING
    lease_owner: str | None = None
    fencing_token: int = 0
    input_manifest_id: str | None = None

    def merge_pending(self, event_range: EventRange) -> MemoryGeneration:
        if self.state is not RangeState.PENDING:
            raise ValueError("running or sealed generation is immutable")
        return MemoryGeneration(
            generation=self.generation,
            event_range=self.event_range.merge(event_range),
            state=self.state,
            lease_owner=self.lease_owner,
            fencing_token=self.fencing_token,
            input_manifest_id=self.input_manifest_id,
        )


@dataclass(slots=True)
class GenerationQueue:
    """Small deterministic model of the DB uniqueness/CAS rules."""

    generations: list[MemoryGeneration] = field(default_factory=list)

    def refresh(self, event_range: EventRange) -> MemoryGeneration:
        if self.generations and self.generations[-1].state is RangeState.PENDING:
            current = self.generations[-1]
            if current.event_range.overlaps_or_touches(event_range):
                merged = current.merge_pending(event_range)
                self.generations[-1] = merged
                return merged
        generation = (self.generations[-1].generation + 1) if self.generations else 1
        created = MemoryGeneration(generation, event_range)
        self.generations.append(created)
        return created

    def claim(self, generation: int, owner: str, *, fencing_token: int) -> MemoryGeneration:
        if not owner or fencing_token <= 0:
            raise ValueError("claim identity is invalid")
        for index, item in enumerate(self.generations):
            if item.generation == generation:
                if item.state is not RangeState.PENDING:
                    raise ValueError("generation already claimed or sealed")
                claimed = MemoryGeneration(
                    generation=item.generation,
                    event_range=item.event_range,
                    state=RangeState.RUNNING,
                    lease_owner=owner,
                    fencing_token=fencing_token,
                )
                self.generations[index] = claimed
                return claimed
        raise KeyError(generation)

    def seal(
        self, generation: int, owner: str, fencing_token: int, manifest_id: str
    ) -> MemoryGeneration:
        for index, item in enumerate(self.generations):
            if item.generation == generation:
                if (
                    item.state is not RangeState.RUNNING
                    or item.lease_owner != owner
                    or item.fencing_token != fencing_token
                ):
                    raise ValueError("stale generation lease")
                sealed = MemoryGeneration(
                    generation=item.generation,
                    event_range=item.event_range,
                    state=RangeState.SEALED,
                    lease_owner=owner,
                    fencing_token=fencing_token,
                    input_manifest_id=manifest_id,
                )
                self.generations[index] = sealed
                return sealed
        raise KeyError(generation)
