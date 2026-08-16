"""Pure domain contracts for the deterministic proactive pipeline.

The proactive worker deals in these immutable values.  They deliberately do not
contain provider, Telegram, SQLAlchemy, or scheduler objects; persistence and
side effects belong to the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import new as hmac_new
from math import isfinite
from typing import Final
from uuid import UUID


class ReasonCode(StrEnum):
    PROMISE_DUE = "promise_due"
    EVENT_UPCOMING = "event_upcoming"
    EVENT_FOLLOWUP = "event_followup"
    RELATIONSHIP_RECONNECT = "relationship_reconnect"
    EXPLICIT_FOLLOWUP = "explicit_followup"


class OccurrenceState(StrEnum):
    SCHEDULED = "scheduled"
    ELIGIBLE = "eligible"
    GROUPED = "grouped"
    EVALUATED = "evaluated"
    SUPPRESSED = "suppressed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class CandidateState(StrEnum):
    OPEN = "open"
    EVALUATING = "evaluating"
    SEND_SELECTED = "send_selected"
    DEFERRED_ONCE = "deferred_once"
    EVALUATED_NONE = "evaluated_none"
    FAILED_MODEL = "failed_model"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class ProactiveAction(StrEnum):
    SEND_NOW = "send_now"
    DEFER_ONCE = "defer_once"
    NONE = "none"


class RelationshipLevel(StrEnum):
    CLOSE = "close"
    FRIEND = "friend"
    ACQUAINTANCE = "acquaintance"
    UNKNOWN = "unknown"


class ReservationState(StrEnum):
    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"
    SEND_UNKNOWN = "send_unknown"


class SuppressionReason(StrEnum):
    DISABLED = "disabled"
    UNSUPPORTED_PEER = "unsupported_peer"
    MODE_SUPPRESSED = "mode_suppressed"
    QUIET_HOURS = "quiet_hours"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MINIMUM_INTERVAL = "minimum_interval"
    CONVERSATION_ACTIVE = "conversation_active"
    CONFLICTING_WORK = "conflicting_work"
    EVIDENCE_INVALID = "evidence_invalid"
    WINDOW_NOT_OPEN = "window_not_open"
    WINDOW_EXPIRED = "window_expired"
    POLICY_CHANGED = "policy_changed"


REASON_PRIORITY: Final[dict[ReasonCode, int]] = {
    ReasonCode.PROMISE_DUE: 0,
    ReasonCode.EVENT_UPCOMING: 1,
    ReasonCode.EVENT_FOLLOWUP: 2,
    ReasonCode.EXPLICIT_FOLLOWUP: 3,
    ReasonCode.RELATIONSHIP_RECONNECT: 4,
}
DECISION_CODES: Final[frozenset[str]] = frozenset(
    {"timely_support", "better_later_in_window", "not_natural_now", "insufficient_context"}
)


def _check_uuid(value: UUID, name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")


def _check_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TypedEvidence:
    """A typed, current evidence root; no free-form model evidence is accepted."""

    source_type: str
    source_id: UUID
    source_version: str
    source_hash: bytes
    summary: str
    current: bool = True
    active: bool = True
    explicit: bool = True

    def __post_init__(self) -> None:
        _check_uuid(self.source_id, "source_id")
        if self.source_type not in {
            "life_event",
            "intention",
            "relationship",
            "message_revision",
            "rule",
        }:
            raise ValueError("unsupported evidence source type")
        if not self.source_version or len(self.source_version) > 128:
            raise ValueError("evidence source version is invalid")
        if len(self.source_hash) != 32:
            raise ValueError("evidence hash must be 32 bytes")
        if not self.summary or len(self.summary) > 500:
            raise ValueError("evidence summary must be 1..500 characters")

    @property
    def valid(self) -> bool:
        return self.current and self.active and bool(self.source_hash)


@dataclass(frozen=True, slots=True)
class ProactivePolicy:
    """Versioned policy defaults.  Absolute quiet is intentionally immutable."""

    version_id: UUID
    version_no: int = 1
    enabled: bool = False
    scheduler_scan_seconds: int = 900
    activity_suppression_seconds: int = 1800
    quiet_start_local: time = time(22, 0)
    quiet_end_local: time = time(8, 0)
    absolute_no_send_start_local: time = time(0, 0)
    absolute_no_send_end_local: time = time(7, 0)
    bypass_importance_threshold: float = 0.90
    contact_bypass_daily_limit: int = 1
    account_daily_limit: int = 10
    close_daily_limit: int = 2
    friend_daily_limit: int = 1
    acquaintance_daily_limit: int = 1
    close_min_interval: timedelta = timedelta(hours=6)
    friend_min_interval: timedelta = timedelta(hours=12)
    acquaintance_min_interval: timedelta = timedelta(hours=24)
    close_reconnect_after: timedelta = timedelta(days=3)
    friend_reconnect_after: timedelta = timedelta(days=7)
    allowed_reasons: frozenset[ReasonCode] = frozenset(ReasonCode)
    context_contract_version: str = "proactive-context-v1"

    def __post_init__(self) -> None:
        _check_uuid(self.version_id, "version_id")
        if self.version_no < 1 or self.scheduler_scan_seconds <= 0:
            raise ValueError("policy version and scan interval must be positive")
        if self.activity_suppression_seconds <= 0:
            raise ValueError("activity suppression must be positive")
        if self.absolute_no_send_start_local != time(
            0, 0
        ) or self.absolute_no_send_end_local != time(7, 0):
            raise ValueError("absolute no-send window is fixed to 00:00-07:00")
        if not 0 <= self.bypass_importance_threshold <= 1 or not isfinite(
            self.bypass_importance_threshold
        ):
            raise ValueError("bypass threshold must be between zero and one")
        if self.contact_bypass_daily_limit not in {0, 1}:
            raise ValueError("bypass daily limit must be zero or one")
        limits = (
            self.account_daily_limit,
            self.close_daily_limit,
            self.friend_daily_limit,
            self.acquaintance_daily_limit,
        )
        if any(value < 0 for value in limits):
            raise ValueError("daily limits cannot be negative")
        intervals = (
            self.close_min_interval,
            self.friend_min_interval,
            self.acquaintance_min_interval,
        )
        if any(value <= timedelta(0) for value in intervals):
            raise ValueError("minimum intervals must be positive")
        if not self.context_contract_version:
            raise ValueError("context contract version is required")

    def daily_limit(self, relationship: RelationshipLevel) -> int:
        if relationship is RelationshipLevel.CLOSE:
            return self.close_daily_limit
        if relationship is RelationshipLevel.FRIEND:
            return self.friend_daily_limit
        return self.acquaintance_daily_limit

    def minimum_interval(self, relationship: RelationshipLevel) -> timedelta:
        if relationship is RelationshipLevel.CLOSE:
            return self.close_min_interval
        if relationship is RelationshipLevel.FRIEND:
            return self.friend_min_interval
        return self.acquaintance_min_interval


@dataclass(frozen=True, slots=True)
class ContactSettings:
    contact_id: UUID
    version: int = 1
    enabled: bool = True
    daily_limit: int | None = None
    minimum_interval: timedelta | None = None
    relationship_level: RelationshipLevel = RelationshipLevel.UNKNOWN
    timezone_name: str | None = None

    def __post_init__(self) -> None:
        _check_uuid(self.contact_id, "contact_id")
        if self.version < 1:
            raise ValueError("contact settings version must be positive")
        if self.daily_limit is not None and self.daily_limit < 0:
            raise ValueError("contact daily limit cannot be negative")
        if self.minimum_interval is not None and self.minimum_interval <= timedelta(0):
            raise ValueError("contact minimum interval must be positive")


@dataclass(frozen=True, slots=True)
class RuleOccurrence:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    occurrence_key: bytes
    generation: int
    reason: ReasonCode
    state: OccurrenceState
    window_start_at: datetime
    window_end_at: datetime
    hard_deadline_at: datetime
    timezone_name: str
    local_date: date
    importance: float
    evidence: tuple[TypedEvidence, ...]
    source_type: str
    source_id: UUID
    source_version: str
    quiet_bypass_possible: bool = False
    policy_version_id: UUID | None = None
    contact_setting_version: int | None = None
    relationship_state_version: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("account_id", self.account_id),
            ("contact_id", self.contact_id),
            ("conversation_id", self.conversation_id),
            ("source_id", self.source_id),
        ):
            _check_uuid(value, name)
        if len(self.occurrence_key) != 32 or self.generation < 1:
            raise ValueError("occurrence identity is invalid")
        _check_aware(self.window_start_at, "window_start_at")
        _check_aware(self.window_end_at, "window_end_at")
        _check_aware(self.hard_deadline_at, "hard_deadline_at")
        if (
            not self.window_start_at < self.window_end_at
            or self.hard_deadline_at > self.window_end_at
        ):
            raise ValueError("occurrence window is invalid")
        if not 0 <= self.importance <= 1 or not isfinite(self.importance):
            raise ValueError("importance must be between zero and one")
        if (
            self.reason not in ReasonCode
            or not self.evidence
            or any(not item.valid for item in self.evidence)
        ):
            raise ValueError("occurrence evidence is invalid")
        if self.quiet_bypass_possible and (
            self.reason not in {ReasonCode.EVENT_UPCOMING, ReasonCode.PROMISE_DUE}
            or self.importance < 0.90
        ):
            raise ValueError("quiet bypass is not allowed for this occurrence")


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    candidate_key: bytes
    generation: int
    membership_hash: bytes
    occurrences: tuple[RuleOccurrence, ...]
    window_start_at: datetime
    window_end_at: datetime
    policy_version_id: UUID | None
    timezone_name: str
    mode_version: int
    content_revision: int
    activity_revision: int
    state: CandidateState = CandidateState.OPEN

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("account_id", self.account_id),
            ("contact_id", self.contact_id),
            ("conversation_id", self.conversation_id),
        ):
            _check_uuid(value, name)
        if len(self.candidate_key) != 32 or len(self.membership_hash) != 32 or self.generation < 1:
            raise ValueError("candidate identity is invalid")
        if not self.occurrences or any(
            item.contact_id != self.contact_id for item in self.occurrences
        ):
            raise ValueError("candidate must contain same-contact occurrences")
        _check_aware(self.window_start_at, "window_start_at")
        _check_aware(self.window_end_at, "window_end_at")
        if self.window_start_at >= self.window_end_at:
            raise ValueError("candidate window is invalid")
        if self.mode_version < 1 or self.content_revision < 0 or self.activity_revision < 0:
            raise ValueError("candidate snapshots are invalid")


@dataclass(frozen=True, slots=True)
class AgentDecision:
    candidate_id: UUID
    action: ProactiveAction
    decision_code: str
    selected_occurrence_ids: tuple[UUID, ...]
    topic: str | None
    priority: float
    defer_until: datetime | None = None
    defer_count: int = 0

    def __post_init__(self) -> None:
        _check_uuid(self.candidate_id, "candidate_id")
        if self.decision_code not in DECISION_CODES:
            raise ValueError("decision code is not allowlisted")
        if not 0 <= self.priority <= 1 or not isfinite(self.priority):
            raise ValueError("decision priority must be between zero and one")
        if self.topic is not None and (
            not self.topic or len(self.topic) > 120 or "\n" in self.topic
        ):
            raise ValueError("topic must be a short single-line brief")
        if self.defer_count not in {0, 1}:
            raise ValueError("defer count must be zero or one")
        if self.action is ProactiveAction.NONE:
            if (
                self.selected_occurrence_ids
                or self.topic is not None
                or self.defer_until is not None
                or self.priority != 0
            ):
                raise ValueError("none decision cannot select or schedule work")
        elif not self.selected_occurrence_ids or self.topic is None:
            raise ValueError("send/defer decision requires selected occurrence and topic")
        if self.action is ProactiveAction.DEFER_ONCE:
            if self.defer_until is None or self.defer_count != 1:
                raise ValueError("defer_once requires one defer and a deadline")
        elif self.defer_until is not None or self.defer_count != 0:
            raise ValueError("only defer_once may carry defer state")


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    account_daily: int
    contact_daily: int
    bypass_daily: int = 0

    def __post_init__(self) -> None:
        if min(self.account_daily, self.contact_daily, self.bypass_daily) < 0:
            raise ValueError("budget limits cannot be negative")


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    id: UUID
    reservation_key: bytes
    account_id: UUID
    contact_id: UUID
    local_date: date
    bypass: bool
    state: ReservationState
    expires_at: datetime

    def __post_init__(self) -> None:
        _check_uuid(self.id, "id")
        _check_uuid(self.account_id, "account_id")
        _check_uuid(self.contact_id, "contact_id")
        if (
            len(self.reservation_key) != 32
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("reservation identity or expiry is invalid")


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    reason: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class ProactiveContext:
    """Text-only context envelope handed to Main AI after all preliminary gates."""

    candidate_id: UUID
    topic: str
    reasons: tuple[tuple[str, str], ...]
    timezone_name: str
    local_time: str
    relationship: RelationshipLevel
    freshness: str = "fresh"

    def __post_init__(self) -> None:
        _check_uuid(self.candidate_id, "candidate_id")
        if not self.topic or len(self.topic) > 120 or not self.reasons:
            raise ValueError("proactive context is incomplete")
        if self.freshness not in {"fresh", "degraded", "stale"}:
            raise ValueError("unknown context freshness")


def derive_key(secret: bytes, *parts: str) -> bytes:
    """Length-delimited HMAC identity helper; callers never use message text as a key."""

    if not secret:
        raise ValueError("idempotency secret is required")
    encoded = bytearray(b"proactive-v1\0")
    for part in parts:
        raw = part.encode("utf-8")
        encoded.extend(len(raw).to_bytes(4, "big"))
        encoded.extend(raw)
    return hmac_new(secret, bytes(encoded), "sha256").digest()


def membership_digest(occurrences: tuple[RuleOccurrence, ...]) -> bytes:
    payload = "|".join(
        f"{item.id}:{item.generation}:{item.occurrence_key.hex()}" for item in occurrences
    ).encode()
    return sha256(payload).digest()
