"""Preliminary authorization, text-only context, and final proactive gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from telegram_userbot.domain.conversation.mode import EffectiveMode
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    Candidate,
    CandidateState,
    GateResult,
    ProactiveAction,
    ProactiveContext,
    ProactivePolicy,
    RelationshipLevel,
)
from telegram_userbot.domain.proactive.time import load_timezone, quiet_decision
from telegram_userbot.domain.shared.time import require_aware


class ProactiveTarget(StrEnum):
    AUTO_SEND = "auto_send"
    COPILOT_DRAFT = "copilot_draft"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class AuthorizationInput:
    candidate: Candidate
    decision: AgentDecision
    now: datetime
    policy: ProactivePolicy
    mode: EffectiveMode
    operational_ready: bool = True
    account_enabled: bool = True
    contact_enabled: bool = True
    evidence_current: bool = True
    meaningful_activity_at: datetime | None = None
    conflicting_work: bool = False
    minimum_interval_ok: bool = True
    budget_available: bool = True
    bypass_available: bool = True


@dataclass(frozen=True, slots=True)
class FinalGateInput:
    authorization: AuthorizationInput
    account_control_version: int
    current_account_control_version: int
    current_mode: EffectiveMode
    snapshot_mode: int
    current_mode_version: int
    snapshot_content_revision: int
    current_content_revision: int
    snapshot_activity_revision: int
    current_activity_revision: int
    current_now: datetime
    delivery_already_created: bool = False
    reservation_held: bool = True
    main_output_valid: bool = True


def map_mode(mode: EffectiveMode) -> ProactiveTarget:
    if mode is EffectiveMode.AUTO:
        return ProactiveTarget.AUTO_SEND
    if mode is EffectiveMode.COPILOT:
        return ProactiveTarget.COPILOT_DRAFT
    return ProactiveTarget.SKIP


def preliminary_gate(value: AuthorizationInput) -> GateResult:  # noqa: PLR0911, PLR0912 - ordered fail-closed gates
    candidate, decision = value.candidate, value.decision
    now = require_aware(value.now, "now")
    meaningful_activity_at = (
        require_aware(value.meaningful_activity_at, "meaningful_activity_at")
        if value.meaningful_activity_at is not None
        else None
    )
    if decision.candidate_id != candidate.id:
        return GateResult(False, "CANDIDATE_MISMATCH")
    if decision.action is ProactiveAction.NONE:
        return GateResult(False, "DECISION_NONE")
    if not value.policy.enabled:
        return GateResult(False, "POLICY_DISABLED")
    if candidate.state not in {
        CandidateState.OPEN,
        CandidateState.EVALUATING,
        CandidateState.DEFERRED_ONCE,
    }:
        return GateResult(False, "CANDIDATE_TERMINAL")
    if now < candidate.window_start_at or now >= candidate.window_end_at:
        return GateResult(False, "WINDOW_CLOSED")
    if not value.operational_ready or not value.account_enabled or not value.contact_enabled:
        return GateResult(False, "CONTROL_BLOCKED")
    if value.mode not in {EffectiveMode.AUTO, EffectiveMode.COPILOT}:
        return GateResult(False, "MODE_SUPPRESSED")
    if not value.evidence_current:
        return GateResult(False, "EVIDENCE_INVALID")
    if meaningful_activity_at is not None and now - meaningful_activity_at < timedelta(
        seconds=value.policy.activity_suppression_seconds
    ):
        return GateResult(False, "CONVERSATION_ACTIVE")
    if value.conflicting_work:
        return GateResult(False, "CONFLICTING_WORK")
    if not value.minimum_interval_ok:
        return GateResult(False, "MINIMUM_INTERVAL")
    selected_occurrences = tuple(
        occurrence
        for occurrence in candidate.occurrences
        if occurrence.id in set(decision.selected_occurrence_ids)
    )
    if not selected_occurrences:
        return GateResult(False, "SELECTED_OCCURRENCE_INVALID")
    quiet_results = tuple(
        quiet_decision(
            now,
            timezone_name=candidate.timezone_name,
            policy=value.policy,
            occurrence=occurrence,
        )
        for occurrence in selected_occurrences
    )
    if any(quiet.in_absolute_quiet for quiet in quiet_results):
        return GateResult(False, "ABSOLUTE_NO_SEND")
    if any(quiet.in_quiet_hours for quiet in quiet_results):
        if not all(quiet.bypass_allowed for quiet in quiet_results):
            return GateResult(False, "QUIET_HOURS")
        if not value.bypass_available:
            return GateResult(False, "BYPASS_BUDGET_EXHAUSTED")
    if not value.budget_available:
        return GateResult(False, "BUDGET_EXHAUSTED")
    return GateResult(True, "AUTHORIZED", map_mode(value.mode).value)


def final_gate(value: FinalGateInput) -> GateResult:  # noqa: PLR0911 - every snapshot is an independent gate
    """Re-run all preliminary checks and then compare every version snapshot."""

    preliminary = preliminary_gate(
        replace(value.authorization, now=require_aware(value.current_now, "current_now"))
    )
    if not preliminary.allowed:
        return preliminary
    if value.account_control_version != value.current_account_control_version:
        return GateResult(False, "CONTROL_VERSION_STALE")
    if (
        value.current_mode is not value.authorization.mode
        or value.current_mode is EffectiveMode.HUMAN
    ):
        return GateResult(False, "EFFECTIVE_MODE_STALE")
    if value.snapshot_mode != value.current_mode_version:
        return GateResult(False, "MODE_VERSION_STALE")
    if value.snapshot_content_revision != value.authorization.candidate.content_revision:
        return GateResult(False, "CONTENT_REVISION_SNAPSHOT_INVALID")
    if value.snapshot_content_revision != value.current_content_revision:
        return GateResult(False, "CONTENT_REVISION_STALE")
    if value.current_content_revision < 0:
        return GateResult(False, "CONTENT_REVISION_INVALID")
    if value.snapshot_activity_revision != value.current_activity_revision:
        return GateResult(False, "ACTIVITY_REVISION_STALE")
    if value.snapshot_activity_revision != value.authorization.candidate.activity_revision:
        return GateResult(False, "ACTIVITY_REVISION_SNAPSHOT_INVALID")
    if not value.reservation_held:
        return GateResult(False, "RESERVATION_NOT_HELD")
    if not value.main_output_valid:
        return GateResult(False, "MAIN_OUTPUT_INVALID")
    if value.delivery_already_created:
        return GateResult(False, "DUPLICATE_DELIVERY")
    return GateResult(True, "AUTHORIZED_FINAL", map_mode(value.authorization.mode).value)


def build_text_only_context(
    candidate: Candidate,
    decision: AgentDecision,
    *,
    now: datetime,
    relationship: RelationshipLevel,
    freshness: str = "fresh",
) -> ProactiveContext:
    current_time = require_aware(now, "now")
    if decision.candidate_id != candidate.id or decision.topic is None:
        raise ValueError("decision does not bind candidate topic")
    selected = set(decision.selected_occurrence_ids)
    reasons = tuple(
        (occurrence.reason.value, occurrence.evidence[0].summary)
        for occurrence in candidate.occurrences
        if occurrence.id in selected
    )
    if not reasons:
        raise ValueError("context cannot be built without selected evidence")
    return ProactiveContext(
        candidate_id=candidate.id,
        topic=decision.topic,
        reasons=reasons,
        timezone_name=candidate.timezone_name,
        local_time=current_time.astimezone(load_timezone(candidate.timezone_name)).isoformat(),
        relationship=relationship,
        freshness=freshness,
    )


def defer_due_at(decision: AgentDecision) -> datetime:
    if decision.action is not ProactiveAction.DEFER_ONCE or decision.defer_until is None:
        raise ValueError("decision is not deferred")
    return decision.defer_until
