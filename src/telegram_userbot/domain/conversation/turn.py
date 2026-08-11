"""Pure turn, grace-window, send-gate, and Telegram text-splitting rules."""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from telegram_userbot.domain.conversation.mode import EffectiveMode, ModeResolution


class TurnState(StrEnum):
    COLLECTING = "collecting"
    READY = "ready"
    GENERATING = "generating"
    OUTPUT_READY = "output_ready"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GenerationRaceDecision(StrEnum):
    UNCHANGED = "unchanged"
    WAIT_FOR_GRACE = "wait_for_grace"
    AUTHORIZE_GRACE = "authorize_grace"
    SUPERSEDE = "supersede"


@dataclass(frozen=True, slots=True)
class DebouncePolicy:
    quiet_seconds: int = 3
    hard_cap_seconds: int = 10
    generation_grace_seconds: int = 3

    def __post_init__(self) -> None:
        if min(self.quiet_seconds, self.hard_cap_seconds, self.generation_grace_seconds) <= 0:
            raise ValueError("orchestrator deadlines must be positive")
        if self.quiet_seconds > self.hard_cap_seconds:
            raise ValueError("quiet window cannot exceed hard cap")

    def collection_deadlines(
        self, *, started_at: datetime, observed_at: datetime
    ) -> tuple[datetime, datetime]:
        hard = started_at + timedelta(seconds=self.hard_cap_seconds)
        quiet = min(observed_at + timedelta(seconds=self.quiet_seconds), hard)
        return quiet, hard


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    account_control_version: int
    mode_version: int
    content_revision: int
    generation_no: int

    def __post_init__(self) -> None:
        if min(self.account_control_version, self.mode_version, self.generation_no) < 1:
            raise ValueError("work versions must be positive")
        if self.content_revision < 0:
            raise ValueError("content revision cannot be negative")


@dataclass(frozen=True, slots=True)
class FinalGateInput:
    snapshot: WorkSnapshot
    current: ModeResolution
    required_mode: EffectiveMode
    active_work: bool
    lease_owned: bool
    duplicate_delivery: bool = False
    grace_authorized: bool = False
    source_valid: bool = True


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str


def evaluate_final_gate(value: FinalGateInput) -> GateDecision:
    """Fail closed before durable intent creation and again before Telegram RPC."""

    current = value.current
    checks = (
        (current.operational_state.value == "READY", current.block_reason or "OPERATIONAL_BLOCK"),
        (current.effective_mode is value.required_mode, "EFFECTIVE_MODE_MISMATCH"),
        (
            current.account_control_version == value.snapshot.account_control_version,
            "CONTROL_VERSION_STALE",
        ),
        (current.mode_version == value.snapshot.mode_version, "MODE_VERSION_STALE"),
        (
            current.content_revision == value.snapshot.content_revision or value.grace_authorized,
            "CONTENT_REVISION_STALE",
        ),
        (value.active_work, "WORK_NOT_ACTIVE"),
        (value.lease_owned, "LEASE_LOST"),
        (not value.duplicate_delivery, "DUPLICATE_DELIVERY"),
        (value.source_valid, "SOURCE_INVALIDATED"),
    )
    for passed, reason in checks:
        if not passed:
            return GateDecision(False, reason)
    return GateDecision(True, "AUTHORIZED")


def generation_race_decision(  # noqa: PLR0913 - race dimensions must remain explicit
    *,
    has_new_incoming: bool,
    has_non_grace_change: bool,
    run_started_at: datetime,
    checked_at: datetime,
    model_completed_at: datetime | None,
    grace_seconds: int = 3,
) -> GenerationRaceDecision:
    if has_non_grace_change:
        return GenerationRaceDecision.SUPERSEDE
    if not has_new_incoming:
        return GenerationRaceDecision.UNCHANGED
    deadline = run_started_at + timedelta(seconds=grace_seconds)
    if model_completed_at is not None and model_completed_at <= deadline:
        return GenerationRaceDecision.AUTHORIZE_GRACE
    if checked_at < deadline:
        return GenerationRaceDecision.WAIT_FOR_GRACE
    return GenerationRaceDecision.SUPERSEDE


def split_telegram_text(
    text: str, *, max_chars: int = 4096, max_chunks: int = 16
) -> tuple[str, ...]:
    """Split deterministically without starting a chunk on a combining mark."""

    if not text or max_chars < 1 or max_chunks < 1:
        raise ValueError("text and splitter limits must be positive")
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(chunks) == max_chunks:
            raise ValueError("MODEL_OUTPUT_TOO_LONG")
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        boundary = max_chars
        while boundary > 0 and unicodedata.combining(remaining[boundary]):
            boundary -= 1
        if boundary == 0:
            boundary = max_chars
        preferred = max(
            remaining.rfind("\n", 0, boundary + 1),
            remaining.rfind(" ", 0, boundary + 1),
        )
        if preferred >= max(1, boundary // 2):
            boundary = preferred + (1 if remaining[preferred] == "\n" else 0)
        chunk = remaining[:boundary]
        if not chunk:
            raise ValueError("splitter made no progress")
        chunks.append(chunk)
        remaining = remaining[boundary:]
    return tuple(chunks)
