"""Deterministic proactive occurrence materialization and candidate grouping."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from uuid import UUID, uuid5

from telegram_userbot.domain.proactive.models import (
    REASON_PRIORITY,
    Candidate,
    CandidateState,
    ContactSettings,
    OccurrenceState,
    ProactivePolicy,
    ReasonCode,
    RelationshipLevel,
    RuleOccurrence,
    SuppressionReason,
    TypedEvidence,
    derive_key,
    membership_digest,
)
from telegram_userbot.domain.proactive.time import (
    load_timezone,
    local_interval_to_utc,
    quiet_decision,
)

OCCURRENCE_NAMESPACE = UUID("b62f7b0f-8e7e-4e6b-8f55-49f4cc3298e6")


@dataclass(frozen=True, slots=True)
class LifeEventFact:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    version: int
    start_at: datetime | None
    end_at: datetime | None
    local_date: date | None
    timezone_name: str
    importance: float
    evidence: tuple[TypedEvidence, ...]
    status: str = "active"
    followup_allowed: bool = False


@dataclass(frozen=True, slots=True)
class IntentionFact:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    version: int
    expected_at: datetime | None
    timezone_name: str
    importance: float
    evidence: tuple[TypedEvidence, ...]
    owner: str = "self"
    status: str = "active"


@dataclass(frozen=True, slots=True)
class ExplicitFollowupFact:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    version: int
    expected_at: datetime | None
    timezone_name: str
    importance: float
    evidence: tuple[TypedEvidence, ...]
    status: str = "active"


@dataclass(frozen=True, slots=True)
class RelationshipFact:
    id: UUID
    account_id: UUID
    contact_id: UUID
    conversation_id: UUID
    version: int
    relationship: RelationshipLevel
    last_meaningful_at: datetime | None
    timezone_name: str
    evidence: tuple[TypedEvidence, ...]


@dataclass(frozen=True, slots=True)
class FilterResult:
    eligible: tuple[RuleOccurrence, ...]
    suppressed: tuple[tuple[RuleOccurrence, SuppressionReason], ...]


def _aware(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("typed occurrence time must be timezone-aware")
    return value.astimezone(UTC)


def _occurrence(  # noqa: PLR0913 - immutable occurrence requires all source snapshots
    *,
    account_id: UUID,
    contact_id: UUID,
    conversation_id: UUID,
    source_id: UUID,
    source_version: int,
    reason: ReasonCode,
    start: datetime,
    end: datetime,
    timezone_name: str,
    importance: float,
    evidence: tuple[TypedEvidence, ...],
    secret: bytes,
    quiet_bypass_possible: bool = False,
    policy: ProactivePolicy | None = None,
) -> RuleOccurrence:
    if not evidence or any(not item.valid for item in evidence):
        raise ValueError("occurrence requires current evidence")
    key = derive_key(
        secret,
        str(account_id),
        str(contact_id),
        reason.value,
        str(source_id),
        str(source_version),
        start.isoformat(),
        end.isoformat(),
        str(policy.version_id if policy is not None else "none"),
    )
    occurrence_id = uuid5(OCCURRENCE_NAMESPACE, key.hex())
    return RuleOccurrence(
        id=occurrence_id,
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        occurrence_key=key,
        generation=1,
        reason=reason,
        state=OccurrenceState.SCHEDULED,
        window_start_at=_aware(start),
        window_end_at=_aware(end),
        hard_deadline_at=_aware(end),
        timezone_name=timezone_name,
        local_date=_aware(start).astimezone(load_timezone(timezone_name)).date(),
        importance=importance,
        evidence=evidence,
        source_type=evidence[0].source_type,
        source_id=source_id,
        source_version=str(source_version),
        quiet_bypass_possible=quiet_bypass_possible,
        policy_version_id=policy.version_id if policy is not None else None,
    )


def materialize_life_event(
    fact: LifeEventFact,
    *,
    now: datetime,
    policy: ProactivePolicy,
    secret: bytes = b"synthetic-proactive-key",
) -> tuple[RuleOccurrence, ...]:
    if fact.status != "active" or fact.importance < 0 or fact.importance > 1:
        return ()
    results: list[RuleOccurrence] = []
    if fact.start_at is None and fact.local_date is not None:
        window_start, window_end = local_interval_to_utc(
            fact.local_date, wall_time(9), wall_time(12), fact.timezone_name
        )
        if window_end > now.astimezone(UTC):
            results.append(
                _occurrence(
                    account_id=fact.account_id,
                    contact_id=fact.contact_id,
                    conversation_id=fact.conversation_id,
                    source_id=fact.id,
                    source_version=fact.version,
                    reason=ReasonCode.EVENT_UPCOMING,
                    start=window_start,
                    end=window_end,
                    timezone_name=fact.timezone_name,
                    importance=fact.importance,
                    evidence=fact.evidence,
                    secret=secret,
                    quiet_bypass_possible=fact.importance >= policy.bypass_importance_threshold,
                    policy=policy,
                )
            )
        if fact.followup_allowed:
            follow_start = window_end
            follow_end = window_end + timedelta(hours=24)
            if follow_end > now.astimezone(UTC):
                results.append(
                    _occurrence(
                        account_id=fact.account_id,
                        contact_id=fact.contact_id,
                        conversation_id=fact.conversation_id,
                        source_id=fact.id,
                        source_version=fact.version,
                        reason=ReasonCode.EVENT_FOLLOWUP,
                        start=follow_start,
                        end=follow_end,
                        timezone_name=fact.timezone_name,
                        importance=fact.importance,
                        evidence=fact.evidence,
                        secret=secret,
                        policy=policy,
                    )
                )
    elif fact.start_at is not None:
        start = _aware(fact.start_at)
        if fact.local_date is not None and fact.end_at is None:
            window_start, window_end = local_interval_to_utc(
                fact.local_date, wall_time(9), wall_time(12), fact.timezone_name
            )
        else:
            window_start, window_end = start - timedelta(hours=24), start - timedelta(hours=2)
        if window_end > now.astimezone(UTC):
            results.append(
                _occurrence(
                    account_id=fact.account_id,
                    contact_id=fact.contact_id,
                    conversation_id=fact.conversation_id,
                    source_id=fact.id,
                    source_version=fact.version,
                    reason=ReasonCode.EVENT_UPCOMING,
                    start=window_start,
                    end=window_end,
                    timezone_name=fact.timezone_name,
                    importance=fact.importance,
                    evidence=fact.evidence,
                    secret=secret,
                    quiet_bypass_possible=fact.importance >= policy.bypass_importance_threshold,
                    policy=policy,
                )
            )
        if fact.followup_allowed:
            follow_start = (
                _aware(fact.end_at) + timedelta(hours=1)
                if fact.end_at
                else start + timedelta(hours=2)
            )
            follow_end = (
                _aware(fact.end_at) + timedelta(hours=24)
                if fact.end_at
                else start + timedelta(hours=24)
            )
            if follow_end > now.astimezone(UTC):
                results.append(
                    _occurrence(
                        account_id=fact.account_id,
                        contact_id=fact.contact_id,
                        conversation_id=fact.conversation_id,
                        source_id=fact.id,
                        source_version=fact.version,
                        reason=ReasonCode.EVENT_FOLLOWUP,
                        start=follow_start,
                        end=follow_end,
                        timezone_name=fact.timezone_name,
                        importance=fact.importance,
                        evidence=fact.evidence,
                        secret=secret,
                        policy=policy,
                    )
                )
    return tuple(results)


def materialize_intention(
    fact: IntentionFact,
    *,
    now: datetime,
    policy: ProactivePolicy,
    secret: bytes = b"synthetic-proactive-key",
) -> tuple[RuleOccurrence, ...]:
    if (
        fact.status != "active"
        or fact.owner != "self"
        or fact.expected_at is None
        or fact.importance < 0.60
    ):
        return ()
    expected = _aware(fact.expected_at)
    end = expected + timedelta(hours=2)
    if end <= now.astimezone(UTC):
        return ()
    return (
        _occurrence(
            account_id=fact.account_id,
            contact_id=fact.contact_id,
            conversation_id=fact.conversation_id,
            source_id=fact.id,
            source_version=fact.version,
            reason=ReasonCode.PROMISE_DUE,
            start=expected - timedelta(minutes=15),
            end=end,
            timezone_name=fact.timezone_name,
            importance=fact.importance,
            evidence=fact.evidence,
            secret=secret,
            quiet_bypass_possible=fact.importance >= policy.bypass_importance_threshold,
            policy=policy,
        ),
    )


def materialize_explicit_followup(
    fact: ExplicitFollowupFact,
    *,
    now: datetime,
    policy: ProactivePolicy,
    secret: bytes = b"synthetic-proactive-key",
) -> tuple[RuleOccurrence, ...]:
    if fact.status != "active" or fact.expected_at is None:
        return ()
    start = _aware(fact.expected_at)
    end = start + timedelta(hours=6)
    if end <= now.astimezone(UTC):
        return ()
    return (
        _occurrence(
            account_id=fact.account_id,
            contact_id=fact.contact_id,
            conversation_id=fact.conversation_id,
            source_id=fact.id,
            source_version=fact.version,
            reason=ReasonCode.EXPLICIT_FOLLOWUP,
            start=start,
            end=end,
            timezone_name=fact.timezone_name,
            importance=fact.importance,
            evidence=fact.evidence,
            secret=secret,
            policy=policy,
        ),
    )


def materialize_reconnect(
    fact: RelationshipFact,
    *,
    now: datetime,
    policy: ProactivePolicy,
    secret: bytes = b"synthetic-proactive-key",
) -> tuple[RuleOccurrence, ...]:
    if (
        fact.relationship not in {RelationshipLevel.CLOSE, RelationshipLevel.FRIEND}
        or fact.last_meaningful_at is None
    ):
        return ()
    threshold = (
        policy.close_reconnect_after
        if fact.relationship is RelationshipLevel.CLOSE
        else policy.friend_reconnect_after
    )
    last = _aware(fact.last_meaningful_at)
    if now.astimezone(UTC) - last < threshold:
        return ()
    local_date = now.astimezone(load_timezone(fact.timezone_name)).date()
    start, end = local_interval_to_utc(local_date, wall_time(8), wall_time(22), fact.timezone_name)
    if end <= now.astimezone(UTC):
        return ()
    return (
        _occurrence(
            account_id=fact.account_id,
            contact_id=fact.contact_id,
            conversation_id=fact.conversation_id,
            source_id=fact.id,
            source_version=fact.version,
            reason=ReasonCode.RELATIONSHIP_RECONNECT,
            start=max(start, now.astimezone(UTC)),
            end=end,
            timezone_name=fact.timezone_name,
            importance=0.50,
            evidence=fact.evidence,
            secret=secret,
            policy=policy,
        ),
    )


def filter_occurrences(  # noqa: PLR0912, PLR0913 - each suppression gate is explicit
    occurrences: tuple[RuleOccurrence, ...],
    *,
    now: datetime,
    policy: ProactivePolicy,
    settings: ContactSettings,
    account_enabled: bool,
    mode_permits: bool,
    meaningful_activity_at: datetime | None = None,
    conflicting_work: bool = False,
    last_proactive_at: datetime | None = None,
) -> FilterResult:
    eligible: list[RuleOccurrence] = []
    suppressed: list[tuple[RuleOccurrence, SuppressionReason]] = []
    now_utc = now.astimezone(UTC)
    for occurrence in occurrences:
        reason: SuppressionReason | None = None
        if not account_enabled or not settings.enabled:
            reason = SuppressionReason.DISABLED
        elif occurrence.reason not in policy.allowed_reasons:
            reason = SuppressionReason.POLICY_CHANGED
        elif not occurrence.evidence or any(not item.valid for item in occurrence.evidence):
            reason = SuppressionReason.EVIDENCE_INVALID
        elif now_utc < occurrence.window_start_at:
            reason = SuppressionReason.WINDOW_NOT_OPEN
        elif now_utc >= occurrence.window_end_at:
            reason = SuppressionReason.WINDOW_EXPIRED
        elif not mode_permits:
            reason = SuppressionReason.MODE_SUPPRESSED
        elif meaningful_activity_at is not None and now_utc - meaningful_activity_at.astimezone(
            UTC
        ) < timedelta(seconds=policy.activity_suppression_seconds):
            reason = SuppressionReason.CONVERSATION_ACTIVE
        elif conflicting_work:
            reason = SuppressionReason.CONFLICTING_WORK
        elif (
            settings.minimum_interval is not None
            and last_proactive_at is not None
            and now_utc - last_proactive_at.astimezone(UTC) < settings.minimum_interval
        ):
            reason = SuppressionReason.MINIMUM_INTERVAL
        if reason is None:
            quiet = quiet_decision(
                now, timezone_name=occurrence.timezone_name, policy=policy, occurrence=occurrence
            )
            if quiet.blocked:
                reason = SuppressionReason.QUIET_HOURS
        if reason is not None:
            suppressed.append((replace(occurrence, state=OccurrenceState.SUPPRESSED), reason))
        else:
            eligible.append(replace(occurrence, state=OccurrenceState.ELIGIBLE))
    return FilterResult(tuple(eligible), tuple(suppressed))


def aggregate_candidates(  # noqa: PLR0913 - candidate snapshots are sealed at materialization
    occurrences: tuple[RuleOccurrence, ...],
    *,
    now: datetime,
    policy: ProactivePolicy,
    secret: bytes = b"synthetic-proactive-key",
    mode_versions: dict[UUID, int] | None = None,
    content_revisions: dict[UUID, int] | None = None,
    activity_revisions: dict[UUID, int] | None = None,
) -> tuple[Candidate, ...]:
    """Aggregate only overlapping windows; never merge unrelated time windows."""

    grouped: dict[UUID, list[RuleOccurrence]] = {}
    for occurrence in occurrences:
        grouped.setdefault(occurrence.contact_id, []).append(occurrence)
    candidates: list[Candidate] = []
    for contact_id, values in grouped.items():
        ordered = sorted(
            values,
            key=lambda item: (
                item.window_end_at,
                REASON_PRIORITY[item.reason],
                -item.importance,
                str(item.id),
            ),
        )
        while ordered:
            first = ordered.pop(0)
            cluster = [first]
            start, end = first.window_start_at, first.window_end_at
            for candidate in tuple(ordered):
                new_start, new_end = (
                    max(start, candidate.window_start_at),
                    min(end, candidate.window_end_at),
                )
                if new_start < new_end:
                    cluster.append(candidate)
                    start, end = new_start, new_end
                    ordered.remove(candidate)
            cluster = sorted(
                cluster,
                key=lambda item: (
                    REASON_PRIORITY[item.reason],
                    item.window_end_at,
                    -item.importance,
                    str(item.id),
                ),
            )
            member_hash = membership_digest(tuple(cluster))
            key = derive_key(
                secret,
                str(cluster[0].account_id),
                str(contact_id),
                member_hash.hex(),
                str(policy.version_id),
                start.isoformat(),
                end.isoformat(),
            )
            candidate_id = uuid5(OCCURRENCE_NAMESPACE, "candidate:" + key.hex())
            candidates.append(
                Candidate(
                    id=candidate_id,
                    account_id=cluster[0].account_id,
                    contact_id=contact_id,
                    conversation_id=cluster[0].conversation_id,
                    candidate_key=key,
                    generation=1,
                    membership_hash=member_hash,
                    occurrences=tuple(cluster),
                    window_start_at=max(start, now.astimezone(UTC)),
                    window_end_at=end,
                    policy_version_id=policy.version_id,
                    timezone_name=cluster[0].timezone_name,
                    mode_version=(mode_versions or {}).get(contact_id, 1),
                    content_revision=(content_revisions or {}).get(contact_id, 0),
                    activity_revision=(activity_revisions or {}).get(contact_id, 0),
                    state=CandidateState.OPEN,
                )
            )
    return tuple(sorted(candidates, key=lambda item: (item.window_end_at, str(item.id))))
