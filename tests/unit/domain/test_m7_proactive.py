"""M7 proactive domain contracts use only synthetic facts and deterministic time."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from hashlib import sha256
from threading import Thread
from typing import Any, cast
from uuid import uuid4

import pytest

from telegram_userbot.domain.conversation.mode import EffectiveMode
from telegram_userbot.domain.proactive.budget import BudgetLedger
from telegram_userbot.domain.proactive.jobs import DueJobState, DurableDueJobStore
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    BudgetLimits,
    BudgetReservation,
    Candidate,
    CandidateState,
    ContactSettings,
    ProactiveAction,
    ProactiveContext,
    ProactivePolicy,
    ReasonCode,
    RelationshipLevel,
    ReservationState,
    TypedEvidence,
    derive_key,
    membership_digest,
)
from telegram_userbot.domain.proactive.pipeline import (
    AuthorizationInput,
    FinalGateInput,
    build_text_only_context,
    defer_due_at,
    final_gate,
    map_mode,
    preliminary_gate,
)
from telegram_userbot.domain.proactive.rules import (
    ExplicitFollowupFact,
    IntentionFact,
    LifeEventFact,
    RelationshipFact,
    aggregate_candidates,
    filter_occurrences,
    materialize_explicit_followup,
    materialize_intention,
    materialize_life_event,
    materialize_reconnect,
)
from telegram_userbot.domain.proactive.time import (
    TimePolicyError,
    load_timezone,
    local_interval_to_utc,
    local_to_utc,
    next_quiet_end,
    quiet_decision,
)
from telegram_userbot.domain.proactive.validation import (
    ProactiveValidationError,
    parse_agent_decision,
    parse_agent_response_json,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def evidence(
    source_type: str = "rule", *, current: bool = True, explicit: bool = True
) -> TypedEvidence:
    source_id = uuid4()
    return TypedEvidence(
        source_type=source_type,
        source_id=source_id,
        source_version="v1",
        source_hash=sha256(str(source_id).encode()).digest(),
        summary="synthetic evidence summary",
        current=current,
        explicit=explicit,
    )


def policy(**changes: object) -> ProactivePolicy:
    values: dict[str, object] = {"version_id": uuid4(), "enabled": True}
    values.update(changes)
    return ProactivePolicy(**values)  # type: ignore[arg-type]


def intention(*, expected_at: datetime | None = None, importance: float = 0.95) -> IntentionFact:
    account_id, contact_id, conversation_id = uuid4(), uuid4(), uuid4()
    return IntentionFact(
        id=uuid4(),
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        version=1,
        expected_at=expected_at or NOW + timedelta(hours=1),
        timezone_name="Europe/Berlin",
        importance=importance,
        evidence=(evidence("intention"),),
    )


def candidate_for(*, now: datetime = NOW, timezone_name: str = "UTC") -> Candidate:
    fact = intention(expected_at=now + timedelta(minutes=5))
    fact = replace(fact, timezone_name=timezone_name)
    active_policy = policy()
    occurrences = materialize_intention(fact, now=now, policy=active_policy)
    return aggregate_candidates(occurrences, now=now, policy=active_policy)[0]


@pytest.mark.unit
def test_m7_rules_materialize_allowlisted_facts_and_group_only_overlaps() -> None:
    p = policy()
    fact = intention()
    occurrence = materialize_intention(fact, now=NOW, policy=p)[0]
    assert occurrence.reason is ReasonCode.PROMISE_DUE
    assert materialize_intention(replace(fact, owner="other"), now=NOW, policy=p) == ()
    assert materialize_intention(replace(fact, importance=0.5), now=NOW, policy=p) == ()
    explicit = ExplicitFollowupFact(
        id=uuid4(),
        account_id=fact.account_id,
        contact_id=fact.contact_id,
        conversation_id=fact.conversation_id,
        version=1,
        expected_at=NOW + timedelta(hours=2),
        timezone_name="Europe/Berlin",
        importance=0.7,
        evidence=(evidence("rule"),),
    )
    assert materialize_explicit_followup(explicit, now=NOW, policy=p)
    life = LifeEventFact(
        id=uuid4(),
        account_id=fact.account_id,
        contact_id=fact.contact_id,
        conversation_id=fact.conversation_id,
        version=1,
        start_at=None,
        end_at=None,
        local_date=date(2026, 8, 17),
        timezone_name="Europe/Berlin",
        importance=0.95,
        evidence=(evidence("life_event"),),
        followup_allowed=True,
    )
    events = materialize_life_event(life, now=NOW, policy=p)
    assert {item.reason for item in events} == {
        ReasonCode.EVENT_UPCOMING,
        ReasonCode.EVENT_FOLLOWUP,
    }
    candidates = aggregate_candidates((occurrence, *events), now=NOW, policy=p)
    assert candidates
    assert all(item.occurrences for item in candidates)
    assert membership_digest(candidates[0].occurrences) == candidates[0].membership_hash
    assert derive_key(b"secret", "a", "bc") != derive_key(b"secret", "ab", "c")


@pytest.mark.unit
def test_m7_rules_cover_event_start_end_reconnect_and_filter_suppressions() -> None:
    p = policy()
    account_id, contact_id, conversation_id = uuid4(), uuid4(), uuid4()
    event = LifeEventFact(
        id=uuid4(),
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        version=2,
        start_at=NOW + timedelta(days=1),
        end_at=NOW + timedelta(days=1, hours=2),
        local_date=None,
        timezone_name="UTC",
        importance=0.95,
        evidence=(evidence("life_event"),),
        followup_allowed=True,
    )
    event_occurrences = materialize_life_event(event, now=NOW, policy=p)
    assert len(event_occurrences) == 2
    relationship = RelationshipFact(
        id=uuid4(),
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        version=1,
        relationship=RelationshipLevel.CLOSE,
        last_meaningful_at=NOW - timedelta(days=10),
        timezone_name="UTC",
        evidence=(evidence("relationship"),),
    )
    assert materialize_reconnect(relationship, now=NOW, policy=p)
    assert (
        materialize_reconnect(
            replace(relationship, relationship=RelationshipLevel.UNKNOWN),
            now=NOW,
            policy=p,
        )
        == ()
    )
    occurrence = event_occurrences[0]
    settings = ContactSettings(contact_id=contact_id, minimum_interval=timedelta(hours=1))
    assert (
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=False,
            mode_permits=True,
        )
        .suppressed[0][1]
        .value
        == "disabled"
    )
    assert (
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=False,
        )
        .suppressed[0][1]
        .value
        == "mode_suppressed"
    )
    assert (
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
            meaningful_activity_at=NOW - timedelta(seconds=1),
        )
        .suppressed[0][1]
        .value
        == "conversation_active"
    )
    assert (
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
            conflicting_work=True,
        )
        .suppressed[0][1]
        .value
        == "conflicting_work"
    )
    assert (
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
            last_proactive_at=NOW - timedelta(seconds=1),
        )
        .suppressed[0][1]
        .value
        == "minimum_interval"
    )


@pytest.mark.unit
def test_m7_dst_gap_fold_and_overnight_quiet_boundaries_are_explicit() -> None:
    with pytest.raises(TimePolicyError):
        local_to_utc(
            datetime.fromisoformat("2026-08-16T12:00:00"), "Europe/Berlin", ambiguous="bad"
        )
    assert local_to_utc(datetime.fromisoformat("2026-03-29T02:30:00"), "Europe/Berlin") == datetime(
        2026, 3, 29, 1, 0, tzinfo=UTC
    )
    earlier = local_to_utc(
        datetime.fromisoformat("2026-10-25T02:30:00"), "Europe/Berlin", ambiguous="earlier"
    )
    later = local_to_utc(
        datetime.fromisoformat("2026-10-25T02:30:00"), "Europe/Berlin", ambiguous="later"
    )
    assert later - earlier == timedelta(hours=1)
    start, end = local_interval_to_utc(date(2026, 8, 16), time(22), time(8), "Europe/Berlin")
    assert end > start
    p = policy()
    assert quiet_decision(
        datetime(2026, 8, 16, 23, tzinfo=UTC), timezone_name="UTC", policy=p
    ).in_quiet_hours
    assert quiet_decision(
        datetime(2026, 8, 17, 6, tzinfo=UTC), timezone_name="UTC", policy=p
    ).in_quiet_hours
    assert quiet_decision(
        datetime(2026, 8, 17, 3, tzinfo=UTC), timezone_name="UTC", policy=p
    ).in_absolute_quiet
    assert next_quiet_end(
        datetime(2026, 8, 17, 2, tzinfo=UTC), timezone_name="UTC", policy=p
    ) == datetime(2026, 8, 17, 8, tzinfo=UTC)


@pytest.mark.unit
def test_m7_budget_is_atomic_idempotent_reaped_and_unknown_is_charged() -> None:
    ledger = BudgetLedger()
    account_id, contact_id = uuid4(), uuid4()
    limits = BudgetLimits(account_daily=2, contact_daily=1, bypass_daily=1)
    key = sha256(b"reservation").digest()
    held = ledger.reserve(
        account_id=account_id,
        contact_id=contact_id,
        local_date=NOW.date(),
        limits=limits,
        expires_at=NOW + timedelta(minutes=1),
        reservation_key=key,
    )
    assert held is not None
    assert (
        ledger.reserve(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=limits,
            expires_at=NOW + timedelta(minutes=1),
            reservation_key=key,
        )
        == held
    )
    assert ledger.commit(key, unknown=True).state is ReservationState.SEND_UNKNOWN
    assert ledger.commit(key).state is ReservationState.SEND_UNKNOWN
    with pytest.raises(ValueError, match="committed/unknown reservation cannot be released"):
        ledger.release(key)
    bypass_key = sha256(b"bypass").digest()
    bypass = ledger.reserve(
        account_id=account_id,
        contact_id=uuid4(),
        local_date=NOW.date(),
        limits=limits,
        expires_at=NOW,
        reservation_key=bypass_key,
        bypass=True,
    )
    assert bypass is not None
    expired = ledger.reap(now=NOW)
    assert len(expired) == 1
    assert expired[0].state is ReservationState.EXPIRED
    assert ledger.snapshot(account_id=account_id, contact_id=contact_id, local_date=NOW.date())
    assert (
        ledger.reserve(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(0, 0),
            expires_at=NOW,
            reservation_key=sha256(b"none").digest(),
        )
        is None
    )

    concurrent = BudgetLedger()
    outcomes: list[object] = []

    def reserve_once() -> None:
        outcomes.append(
            concurrent.reserve(
                account_id=account_id,
                contact_id=contact_id,
                local_date=NOW.date(),
                limits=BudgetLimits(1, 1),
                expires_at=NOW,
                reservation_key=sha256(str(uuid4()).encode()).digest(),
            )
        )

    threads = [Thread(target=reserve_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item is not None for item in outcomes) == 1

    shrinking = BudgetLedger()
    shrinking_key = sha256(b"shrinking-first").digest()
    assert (
        shrinking.reserve(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(3, 3),
            expires_at=NOW + timedelta(minutes=1),
            reservation_key=shrinking_key,
        )
        is not None
    )
    assert (
        shrinking.reserve(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=NOW + timedelta(minutes=1),
            reservation_key=sha256(b"shrinking-second").digest(),
        )
        is None
    )
    assert shrinking.snapshot(account_id=account_id, contact_id=contact_id, local_date=NOW.date())[
        "account_daily"
    ] == (1, 1, 0)
    shrinking.release(shrinking_key)
    assert (
        shrinking.reserve(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=NOW + timedelta(minutes=1),
            reservation_key=sha256(b"shrinking-third").digest(),
        )
        is not None
    )


@pytest.mark.unit
def test_m7_due_jobs_are_idempotent_and_expired_leases_requeue() -> None:
    store = DurableDueJobStore()
    account_id, key = uuid4(), sha256(b"job").digest()
    first = store.enqueue(account_id=account_id, idempotency_key=key, available_at=NOW)
    assert store.enqueue(account_id=account_id, idempotency_key=key, available_at=NOW) == first
    owner = uuid4()
    lease = store.claim(now=NOW, owner=owner, lease=timedelta(seconds=1))
    assert lease is not None
    assert lease.state is DueJobState.LEASED
    assert lease.fencing_token == 1
    assert (
        store.complete(
            idempotency_key=key,
            owner=uuid4(),
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        is False
    )
    assert store.compensate(now=NOW + timedelta(seconds=1)) == ()
    assert store.claim(now=NOW + timedelta(seconds=1), owner=owner) is None
    lease2 = store.claim(now=NOW + timedelta(seconds=6), owner=owner)
    assert lease2 is not None
    assert lease2.attempt_count == 2
    assert lease2.fencing_token == 2
    assert (
        store.complete(
            idempotency_key=key,
            owner=owner,
            fencing_token=lease.fencing_token,
            now=NOW + timedelta(seconds=6),
        )
        is False
    )
    assert (
        store.complete(
            idempotency_key=key,
            owner=owner,
            fencing_token=lease2.fencing_token,
            now=NOW + timedelta(seconds=6),
        )
        is True
    )
    late_key = sha256(b"late-lease").digest()
    store.enqueue(account_id=account_id, idempotency_key=late_key, available_at=NOW)
    late = store.claim(now=NOW, owner=owner, lease=timedelta(seconds=1))
    assert late is not None
    assert (
        store.complete(
            idempotency_key=late_key,
            owner=owner,
            fencing_token=late.fencing_token,
            now=NOW + timedelta(seconds=2),
        )
        is False
    )
    assert store.claim(now=NOW + timedelta(days=1), owner=uuid4()) is None
    assert store.compensate(now=NOW + timedelta(days=1), max_attempts=1) == ()
    assert store.claim(now=NOW + timedelta(days=1, seconds=5), owner=uuid4()) is None
    expired = store.enqueue(
        account_id=account_id, idempotency_key=sha256(b"late").digest(), available_at=NOW
    )
    assert store.expire(now=NOW + timedelta(days=1), window_end=NOW) == 1
    assert expired.state is DueJobState.PENDING


@pytest.mark.unit
def test_m7_quiet_gate_uses_only_agent_selected_occurrences() -> None:
    candidate = candidate_for(now=datetime(2026, 8, 16, 23, tzinfo=UTC))
    first = candidate.occurrences[0]
    second = replace(
        first,
        id=uuid4(),
        occurrence_key=sha256(b"second-occurrence").digest(),
        importance=0.5,
        quiet_bypass_possible=False,
    )
    updated = replace(
        candidate,
        occurrences=(first, second),
        membership_hash=membership_digest((first, second)),
    )
    decision = AgentDecision(
        updated.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (second.id,),
        "check in",
        0.5,
    )
    assert (
        preliminary_gate(
            AuthorizationInput(
                updated,
                decision,
                datetime(2026, 8, 16, 23, tzinfo=UTC),
                policy(),
                EffectiveMode.AUTO,
            )
        ).reason
        == "QUIET_HOURS"
    )

    bypass = replace(first, evidence=(evidence("intention"),))
    non_explicit = replace(
        first,
        id=uuid4(),
        occurrence_key=sha256(b"non-explicit-occurrence").digest(),
        evidence=(evidence("intention", explicit=False),),
    )
    mixed = replace(
        candidate,
        occurrences=(bypass, non_explicit),
        membership_hash=membership_digest((bypass, non_explicit)),
    )
    mixed_decision = replace(
        decision,
        candidate_id=mixed.id,
        selected_occurrence_ids=(bypass.id, non_explicit.id),
    )
    assert (
        preliminary_gate(
            AuthorizationInput(
                mixed,
                mixed_decision,
                datetime(2026, 8, 16, 23, tzinfo=UTC),
                policy(),
                EffectiveMode.AUTO,
            )
        ).reason
        == "QUIET_HOURS"
    )


@pytest.mark.unit
def test_m7_final_gate_binds_activity_snapshot_to_candidate() -> None:
    candidate = candidate_for()
    decision = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (candidate.occurrences[0].id,),
        "check in",
        0.5,
    )
    authorization = AuthorizationInput(candidate, decision, NOW, policy(), EffectiveMode.AUTO)
    final = FinalGateInput(authorization, 1, 1, EffectiveMode.AUTO, 1, 1, 0, 0, 1, 1)
    assert final_gate(final).reason == "ACTIVITY_REVISION_SNAPSHOT_INVALID"


@pytest.mark.unit
def test_m7_agent_parser_is_strict_and_defer_stays_in_window() -> None:
    candidate = candidate_for()
    p = policy()
    selected = str(candidate.occurrences[0].id)
    base = {
        "schema_version": 1,
        "action": "send_now",
        "decision_code": "timely_support",
        "selected_occurrence_ids": [selected],
        "topic": "check in",
        "priority": 0.5,
        "defer_until": None,
    }
    assert (
        parse_agent_decision(base, candidate=candidate, now=NOW, policy=p).action
        is ProactiveAction.SEND_NOW
    )
    assert (
        parse_agent_response_json(json.dumps(base), candidate=candidate, now=NOW, policy=p).topic
        == "check in"
    )
    for invalid in (
        {**base, "extra": 1},
        {key: value for key, value in base.items() if key != "topic"},
        {**base, "action": "delete"},
        {**base, "selected_occurrence_ids": [str(uuid4())]},
        {**base, "priority": float("nan")},
        {**base, "topic": "bad\nline"},
        {**base, "defer_until": "2026-08-16T12:30:00Z"},
    ):
        with pytest.raises(ProactiveValidationError):
            parse_agent_decision(invalid, candidate=candidate, now=NOW, policy=p)
    none = {**base, "action": "none", "selected_occurrence_ids": [], "topic": None, "priority": 0}
    assert (
        parse_agent_decision(none, candidate=candidate, now=NOW, policy=p).action
        is ProactiveAction.NONE
    )
    defer = {
        **base,
        "action": "defer_once",
        "defer_until": (NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
    }
    deferred = parse_agent_decision(defer, candidate=candidate, now=NOW, policy=p)
    assert deferred.defer_count == 1
    assert defer_due_at(deferred) == NOW + timedelta(minutes=10)
    with pytest.raises(ProactiveValidationError):
        parse_agent_response_json("not-json", candidate=candidate, now=NOW, policy=p)


@pytest.mark.unit
def test_m7_final_gate_maps_modes_and_rechecks_all_snapshots() -> None:
    candidate = candidate_for()
    p = policy()
    decision = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (candidate.occurrences[0].id,),
        "check in",
        0.5,
    )
    authorization = AuthorizationInput(candidate, decision, NOW, p, EffectiveMode.AUTO)
    assert map_mode(EffectiveMode.AUTO).value == "auto_send"
    assert map_mode(EffectiveMode.COPILOT).value == "copilot_draft"
    assert map_mode(EffectiveMode.HUMAN).value == "skip"
    assert preliminary_gate(authorization).allowed
    final = FinalGateInput(authorization, 1, 1, EffectiveMode.AUTO, 1, 1, 0, 0, 0, 0)
    assert final_gate(final).reason == "AUTHORIZED_FINAL"
    for field, expected in (
        ("account_control_version", "CONTROL_VERSION_STALE"),
        ("current_mode", "EFFECTIVE_MODE_STALE"),
        ("snapshot_mode", "MODE_VERSION_STALE"),
        ("snapshot_content_revision", "CONTENT_REVISION_SNAPSHOT_INVALID"),
        ("current_content_revision", "CONTENT_REVISION_STALE"),
        ("snapshot_activity_revision", "ACTIVITY_REVISION_STALE"),
        ("reservation_held", "RESERVATION_NOT_HELD"),
        ("main_output_valid", "MAIN_OUTPUT_INVALID"),
        ("delivery_already_created", "DUPLICATE_DELIVERY"),
    ):
        values = {
            field: 2
            if field
            not in {
                "current_mode",
                "reservation_held",
                "main_output_valid",
                "delivery_already_created",
            }
            else {
                "current_mode": EffectiveMode.HUMAN,
                "reservation_held": False,
                "main_output_valid": False,
                "delivery_already_created": True,
            }[field]
        }
        assert final_gate(replace(final, **cast(Any, values))).reason == expected
    assert (
        preliminary_gate(
            AuthorizationInput(candidate, decision, NOW, policy(enabled=False), EffectiveMode.AUTO)
        ).reason
        == "POLICY_DISABLED"
    )
    assert (
        preliminary_gate(
            AuthorizationInput(candidate, decision, NOW, p, EffectiveMode.HUMAN)
        ).reason
        == "MODE_SUPPRESSED"
    )
    assert (
        preliminary_gate(
            AuthorizationInput(
                candidate, decision, NOW, p, EffectiveMode.AUTO, budget_available=False
            )
        ).reason
        == "BUDGET_EXHAUSTED"
    )


@pytest.mark.unit
def test_m7_context_is_text_only_and_candidate_local() -> None:
    candidate = candidate_for(timezone_name="Asia/Tokyo")
    decision = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (candidate.occurrences[0].id,),
        "synthetic topic",
        0.1,
    )
    context = build_text_only_context(
        candidate, decision, now=NOW, relationship=RelationshipLevel.FRIEND
    )
    assert context.timezone_name == "Asia/Tokyo"
    assert context.relationship is RelationshipLevel.FRIEND
    assert context.reasons[0][0] == "promise_due"
    with pytest.raises(ValueError, match="decision does not bind"):
        build_text_only_context(
            candidate,
            AgentDecision(candidate.id, ProactiveAction.NONE, "not_natural_now", (), None, 0),
            now=NOW,
            relationship=RelationshipLevel.UNKNOWN,
        )


@pytest.mark.unit
def test_m7_model_value_objects_fail_closed_on_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="absolute no-send"):
        ProactivePolicy(version_id=uuid4(), absolute_no_send_end_local=time(8))
    with pytest.raises(ValueError, match="contact minimum interval"):
        ContactSettings(contact_id=uuid4(), minimum_interval=timedelta(0))
    with pytest.raises(ValueError, match="budget limits cannot be negative"):
        BudgetLimits(-1, 1)
    with pytest.raises(ValueError, match="idempotency secret is required"):
        derive_key(b"")


@pytest.mark.unit
def test_m7_time_boundaries_reject_naive_values_and_normalize_offsets() -> None:  # noqa: PLR0915
    p = policy()
    fact = intention(expected_at=NOW + timedelta(minutes=5))
    occurrence = materialize_intention(fact, now=NOW, policy=p)[0]
    candidate = aggregate_candidates((occurrence,), now=NOW, policy=p)[0]
    decision = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (occurrence.id,),
        "check in",
        0.5,
    )
    naive = NOW.replace(tzinfo=None)

    life = LifeEventFact(
        uuid4(),
        fact.account_id,
        fact.contact_id,
        fact.conversation_id,
        1,
        None,
        None,
        NOW.date(),
        "UTC",
        0.9,
        (evidence("life_event"),),
    )
    explicit = ExplicitFollowupFact(
        uuid4(),
        fact.account_id,
        fact.contact_id,
        fact.conversation_id,
        1,
        NOW + timedelta(hours=1),
        "UTC",
        0.9,
        (evidence(),),
    )
    relationship = RelationshipFact(
        uuid4(),
        fact.account_id,
        fact.contact_id,
        fact.conversation_id,
        1,
        RelationshipLevel.FRIEND,
        NOW - timedelta(days=60),
        "UTC",
        (evidence("relationship"),),
    )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        materialize_life_event(life, now=naive, policy=p)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        materialize_intention(fact, now=naive, policy=p)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        materialize_explicit_followup(explicit, now=naive, policy=p)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        materialize_reconnect(relationship, now=naive, policy=p)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        aggregate_candidates((occurrence,), now=naive, policy=p)

    settings = ContactSettings(contact_id=fact.contact_id, minimum_interval=timedelta(hours=1))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        filter_occurrences(
            (occurrence,),
            now=naive,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
        )
    with pytest.raises(ValueError, match="meaningful_activity_at must be timezone-aware"):
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
            meaningful_activity_at=naive,
        )
    with pytest.raises(ValueError, match="last_proactive_at must be timezone-aware"):
        filter_occurrences(
            (occurrence,),
            now=NOW,
            policy=p,
            settings=settings,
            account_enabled=True,
            mode_permits=True,
            last_proactive_at=naive,
        )

    authorization = AuthorizationInput(candidate, decision, naive, p, EffectiveMode.AUTO)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        preliminary_gate(authorization)
    with pytest.raises(ValueError, match="meaningful_activity_at must be timezone-aware"):
        preliminary_gate(replace(authorization, now=NOW, meaningful_activity_at=naive))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        build_text_only_context(
            candidate, decision, now=naive, relationship=RelationshipLevel.FRIEND
        )
    payload = {
        "schema_version": 1,
        "action": "send_now",
        "decision_code": "timely_support",
        "selected_occurrence_ids": [str(occurrence.id)],
        "topic": "check in",
        "priority": 0.5,
        "defer_until": None,
    }
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        parse_agent_decision(payload, candidate=candidate, now=naive, policy=p)

    offset = timezone(timedelta(hours=8))
    normalized_occurrence = replace(
        occurrence,
        window_start_at=occurrence.window_start_at.astimezone(offset),
        window_end_at=occurrence.window_end_at.astimezone(offset),
        hard_deadline_at=occurrence.hard_deadline_at.astimezone(offset),
    )
    assert normalized_occurrence.window_start_at.tzinfo is UTC
    normalized_candidate = replace(
        candidate,
        window_start_at=candidate.window_start_at.astimezone(offset),
        window_end_at=candidate.window_end_at.astimezone(offset),
    )
    assert normalized_candidate.window_end_at.tzinfo is UTC
    with pytest.raises(ValueError, match="window_start_at must be timezone-aware"):
        replace(candidate, window_start_at=naive)
    deferred = AgentDecision(
        candidate.id,
        ProactiveAction.DEFER_ONCE,
        "better_later_in_window",
        (occurrence.id,),
        "check in",
        0.5,
        (NOW + timedelta(minutes=1)).astimezone(offset),
        1,
    )
    assert deferred.defer_until is not None
    assert deferred.defer_until.tzinfo is UTC
    with pytest.raises(ValueError, match="defer_until must be timezone-aware"):
        replace(deferred, defer_until=naive)
    reservation = BudgetReservation(
        uuid4(),
        sha256(b"reservation").digest(),
        candidate.account_id,
        candidate.contact_id,
        NOW.date(),
        False,
        ReservationState.HELD,
        NOW.astimezone(offset),
    )
    assert reservation.expires_at.tzinfo is UTC
    with pytest.raises(ValueError, match="expires_at must be timezone-aware"):
        replace(reservation, expires_at=naive)


@pytest.mark.unit
def test_m7_value_objects_cover_validation_boundaries() -> None:
    source_id = uuid4()
    valid_hash = sha256(b"evidence").digest()

    for evidence_changes, message in (
        ({"source_type": "model"}, "unsupported evidence"),
        ({"source_version": ""}, "evidence source version"),
        ({"source_hash": b"short"}, "evidence hash"),
        ({"summary": ""}, "evidence summary"),
        ({"summary": "x" * 501}, "evidence summary"),
    ):
        values: dict[str, object] = {
            "source_type": "rule",
            "source_id": source_id,
            "source_version": "v1",
            "source_hash": valid_hash,
            "summary": "summary",
        }
        values.update(evidence_changes)
        with pytest.raises(ValueError, match=message):
            TypedEvidence(**values)  # type: ignore[arg-type]
    assert not replace(evidence(), current=False).valid
    assert not replace(evidence(), active=False).valid

    with pytest.raises(TypeError, match="version_id"):
        ProactivePolicy(version_id=cast(Any, "not-a-uuid"))
    invalid_policy_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"version_no": 0}, "version and scan"),
        ({"scheduler_scan_seconds": 0}, "version and scan"),
        ({"activity_suppression_seconds": 0}, "activity suppression"),
        ({"absolute_no_send_start_local": time(1)}, "absolute no-send"),
        ({"bypass_importance_threshold": float("nan")}, "bypass threshold"),
        ({"contact_bypass_daily_limit": 2}, "bypass daily"),
        ({"account_daily_limit": -1}, "daily limits"),
        ({"close_min_interval": timedelta(0)}, "minimum intervals"),
        ({"context_contract_version": ""}, "context contract"),
    )
    for policy_changes, message in invalid_policy_cases:
        with pytest.raises(ValueError, match=message):
            policy(**policy_changes)
    p = policy(close_daily_limit=2, friend_daily_limit=3, acquaintance_daily_limit=4)
    assert p.daily_limit(RelationshipLevel.CLOSE) == 2
    assert p.daily_limit(RelationshipLevel.FRIEND) == 3
    assert p.daily_limit(RelationshipLevel.UNKNOWN) == 4
    assert p.minimum_interval(RelationshipLevel.CLOSE) == p.close_min_interval
    assert p.minimum_interval(RelationshipLevel.FRIEND) == p.friend_min_interval
    assert p.minimum_interval(RelationshipLevel.UNKNOWN) == p.acquaintance_min_interval

    invalid_contact_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"version": 0}, "settings version"),
        ({"daily_limit": -1}, "contact daily"),
        ({"minimum_interval": timedelta(0)}, "contact minimum"),
    )
    for contact_changes, message in invalid_contact_cases:
        with pytest.raises(ValueError, match=message):
            ContactSettings(contact_id=uuid4(), **contact_changes)  # type: ignore[arg-type]

    occurrence = candidate_for().occurrences[0]
    invalid_occurrences: tuple[tuple[dict[str, object], str], ...] = (
        ({"occurrence_key": b"short"}, "occurrence identity"),
        ({"generation": 0}, "occurrence identity"),
        ({"window_start_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"window_end_at": occurrence.window_start_at}, "occurrence window"),
        (
            {"hard_deadline_at": occurrence.window_end_at + timedelta(seconds=1)},
            "occurrence window",
        ),
        ({"importance": 2.0}, "importance"),
        ({"evidence": ()}, "occurrence evidence"),
        ({"evidence": (replace(occurrence.evidence[0], current=False),)}, "occurrence evidence"),
        (
            {"quiet_bypass_possible": True, "reason": ReasonCode.RELATIONSHIP_RECONNECT},
            "quiet bypass",
        ),
    )
    for occurrence_changes, message in invalid_occurrences:
        with pytest.raises(ValueError, match=message):
            replace(occurrence, **occurrence_changes)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="candidate identity"):
        replace(candidate_for(), candidate_key=b"short")
    with pytest.raises(ValueError, match="same-contact"):
        replace(candidate_for(), occurrences=(replace(occurrence, contact_id=uuid4()),))
    with pytest.raises(ValueError, match="candidate window"):
        replace(candidate_for(), window_end_at=candidate_for().window_start_at)
    with pytest.raises(ValueError, match="snapshots"):
        replace(candidate_for(), mode_version=0)

    valid_id = occurrence.id
    invalid_decision_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"decision_code": "other"}, "allowlisted"),
        ({"priority": 2.0}, "priority"),
        ({"topic": ""}, "topic"),
        ({"topic": "x" * 121}, "topic"),
        ({"defer_count": 2}, "defer count"),
        ({"selected_occurrence_ids": (valid_id,), "topic": "x", "priority": 1}, "none decision"),
        (
            {"action": ProactiveAction.SEND_NOW, "selected_occurrence_ids": (), "topic": None},
            "requires",
        ),
        (
            {
                "action": ProactiveAction.DEFER_ONCE,
                "selected_occurrence_ids": (valid_id,),
                "topic": "x",
            },
            "defer_once",
        ),
        (
            {
                "action": ProactiveAction.SEND_NOW,
                "selected_occurrence_ids": (valid_id,),
                "topic": "x",
                "defer_until": NOW,
            },
            "only defer_once",
        ),
    )
    for decision_changes, message in invalid_decision_cases:
        decision_values: dict[str, object] = {
            "candidate_id": candidate_for().id,
            "action": ProactiveAction.NONE,
            "decision_code": "not_natural_now",
            "selected_occurrence_ids": (),
            "topic": None,
            "priority": 0,
        }
        decision_values.update(decision_changes)
        with pytest.raises(ValueError, match=message):
            AgentDecision(**decision_values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reservation identity"):
        BudgetReservation(
            uuid4(), b"short", uuid4(), uuid4(), NOW.date(), False, ReservationState.HELD, NOW
        )
    with pytest.raises(ValueError, match="proactive context"):
        ProactiveContext(uuid4(), "", (), "UTC", "", RelationshipLevel.UNKNOWN)
    with pytest.raises(ValueError, match="unknown context"):
        ProactiveContext(
            uuid4(),
            "topic",
            (("reason", "evidence"),),
            "UTC",
            "",
            RelationshipLevel.UNKNOWN,
            "unknown",
        )


@pytest.mark.unit
def test_m7_preliminary_gate_covers_each_fail_closed_boundary() -> None:
    candidate = candidate_for()
    decision = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (candidate.occurrences[0].id,),
        "topic",
        0.5,
    )
    base = AuthorizationInput(candidate, decision, NOW, policy(), EffectiveMode.AUTO)
    cases: tuple[tuple[AuthorizationInput, str], ...] = (
        (replace(base, decision=replace(decision, candidate_id=uuid4())), "CANDIDATE_MISMATCH"),
        (
            replace(
                base,
                decision=AgentDecision(
                    candidate.id, ProactiveAction.NONE, "not_natural_now", (), None, 0
                ),
            ),
            "DECISION_NONE",
        ),
        (replace(base, policy=policy(enabled=False)), "POLICY_DISABLED"),
        (
            replace(base, candidate=replace(candidate, state=CandidateState.EVALUATED_NONE)),
            "CANDIDATE_TERMINAL",
        ),
        (replace(base, now=candidate.window_end_at), "WINDOW_CLOSED"),
        (replace(base, operational_ready=False), "CONTROL_BLOCKED"),
        (replace(base, account_enabled=False), "CONTROL_BLOCKED"),
        (replace(base, contact_enabled=False), "CONTROL_BLOCKED"),
        (replace(base, mode=EffectiveMode.HUMAN), "MODE_SUPPRESSED"),
        (replace(base, evidence_current=False), "EVIDENCE_INVALID"),
        (replace(base, meaningful_activity_at=NOW - timedelta(seconds=1)), "CONVERSATION_ACTIVE"),
        (replace(base, conflicting_work=True), "CONFLICTING_WORK"),
        (replace(base, minimum_interval_ok=False), "MINIMUM_INTERVAL"),
        (replace(base, budget_available=False), "BUDGET_EXHAUSTED"),
    )
    for value, reason in cases:
        assert preliminary_gate(value).reason == reason

    quiet_now = datetime(2026, 8, 16, 23, 0, tzinfo=UTC)
    quiet_candidate = candidate_for(now=quiet_now)
    quiet_base = replace(
        base,
        candidate=quiet_candidate,
        decision=replace(
            decision,
            candidate_id=quiet_candidate.id,
            selected_occurrence_ids=(quiet_candidate.occurrences[0].id,),
        ),
        now=quiet_now,
    )
    no_bypass = replace(
        quiet_base,
        candidate=replace(
            quiet_candidate,
            occurrences=(
                replace(
                    quiet_candidate.occurrences[0], importance=0.5, quiet_bypass_possible=False
                ),
            ),
        ),
    )
    assert preliminary_gate(no_bypass).reason == "QUIET_HOURS"
    assert (
        preliminary_gate(
            quiet_base,
        ).reason
        == "AUTHORIZED"
    )
    assert (
        preliminary_gate(replace(quiet_base, bypass_available=False)).reason
        == "BYPASS_BUDGET_EXHAUSTED"
    )

    absolute_now = datetime(2026, 8, 16, 3, 0, tzinfo=UTC)
    absolute_candidate = candidate_for(now=absolute_now)
    absolute = replace(
        base,
        candidate=absolute_candidate,
        decision=replace(
            decision,
            candidate_id=absolute_candidate.id,
            selected_occurrence_ids=(absolute_candidate.occurrences[0].id,),
        ),
        now=absolute_now,
    )
    assert preliminary_gate(absolute).reason == "ABSOLUTE_NO_SEND"


@pytest.mark.unit
def test_m7_time_and_quiet_policy_fail_closed_edges() -> None:
    p = policy()
    with pytest.raises(TimePolicyError, match="IANA timezone"):
        load_timezone("Berlin")
    with pytest.raises(TimePolicyError, match="unknown IANA"):
        load_timezone("Europe/NotAZone")
    with pytest.raises(TimePolicyError, match="wall-clock"):
        local_to_utc(NOW, "UTC")
    with pytest.raises(TimePolicyError, match="unknown DST"):
        local_to_utc(datetime.fromisoformat("2026-08-16T12:00:00"), "UTC", ambiguous="bad")
    assert local_to_utc(
        datetime.fromisoformat("2026-03-29T02:30:00"), "Europe/Berlin", nonexistent="backward"
    ) == datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
    with pytest.raises(TimePolicyError, match="now must"):
        quiet_decision(datetime.fromisoformat("2026-08-16T12:00:00"), timezone_name="UTC", policy=p)
    assert quiet_decision(NOW, timezone_name="UTC", policy=p).code == "normal_window"
    assert next_quiet_end(NOW, timezone_name="UTC", policy=p) == datetime(
        2026, 8, 17, 8, tzinfo=UTC
    )

    occurrence = candidate_for().occurrences[0]
    for candidate_occurrence in (
        None,
        replace(occurrence, reason=ReasonCode.RELATIONSHIP_RECONNECT, quiet_bypass_possible=False),
        replace(occurrence, importance=0.5, quiet_bypass_possible=False),
        replace(occurrence, quiet_bypass_possible=False),
        replace(occurrence, evidence=(replace(occurrence.evidence[0], explicit=False),)),
    ):
        assert not quiet_decision(
            datetime(2026, 8, 16, 23, tzinfo=UTC),
            timezone_name="UTC",
            policy=p,
            occurrence=candidate_occurrence,
        ).bypass_allowed
    assert quiet_decision(
        datetime(2026, 8, 16, 23, tzinfo=UTC),
        timezone_name="UTC",
        policy=p,
        occurrence=occurrence,
    ).bypass_allowed
    assert not quiet_decision(
        datetime(2026, 8, 16, 1, tzinfo=UTC),
        timezone_name="UTC",
        policy=p,
        occurrence=occurrence,
    ).bypass_allowed
    assert quiet_decision(
        datetime(2026, 8, 16, 7, tzinfo=UTC),
        timezone_name="UTC",
        policy=p,
        occurrence=occurrence,
    ).bypass_allowed


@pytest.mark.unit
def test_m7_validation_and_fake_store_edge_cases() -> None:
    candidate = candidate_for()
    p = policy()
    selected = str(candidate.occurrences[0].id)
    payload = {
        "schema_version": 1,
        "action": "send_now",
        "decision_code": "timely_support",
        "selected_occurrence_ids": [selected],
        "topic": "topic",
        "priority": 0.5,
        "defer_until": None,
    }
    invalid_payloads: tuple[dict[str, object], ...] = (
        {**payload, "schema_version": 2},
        {**payload, "decision_code": "bad"},
        {**payload, "selected_occurrence_ids": "not-list"},
        {**payload, "selected_occurrence_ids": [1]},
        {**payload, "selected_occurrence_ids": ["bad-uuid"]},
        {**payload, "selected_occurrence_ids": [selected, selected]},
        {**payload, "topic": 1},
        {**payload, "topic": "   "},
        {**payload, "topic": "x\rline"},
        {**payload, "priority": True},
        {**payload, "priority": "high"},
        {**payload, "priority": 2},
        {
            **payload,
            "action": "none",
            "selected_occurrence_ids": [selected],
            "topic": None,
            "priority": 0,
        },
        {**payload, "selected_occurrence_ids": [], "topic": None},
        {**payload, "defer_until": "2026-08-16T12:30:00Z"},
        {**payload, "action": "defer_once", "defer_until": None},
        {**payload, "action": "defer_once", "defer_until": "2026-08-16T15:00:00Z"},
    )
    for invalid in invalid_payloads:
        with pytest.raises(ProactiveValidationError):
            parse_agent_decision(invalid, candidate=candidate, now=NOW, policy=p)

    absolute = candidate_for(now=datetime(2026, 8, 16, 3, tzinfo=UTC))
    absolute_payload = {
        **payload,
        "selected_occurrence_ids": [str(absolute.occurrences[0].id)],
        "action": "defer_once",
        "defer_until": "2026-08-16T04:00:00Z",
    }
    with pytest.raises(ProactiveValidationError, match="absolute quiet"):
        parse_agent_decision(
            absolute_payload,
            candidate=absolute,
            now=datetime(2026, 8, 16, 3, tzinfo=UTC),
            policy=p,
        )

    before_quiet = candidate_for(now=datetime(2026, 8, 16, 21, tzinfo=UTC))
    before_quiet = replace(
        before_quiet,
        window_end_at=datetime(2026, 8, 17, 2, tzinfo=UTC),
    )
    future_absolute_payload = {
        **payload,
        "selected_occurrence_ids": [str(before_quiet.occurrences[0].id)],
        "action": "defer_once",
        "defer_until": "2026-08-17T01:00:00Z",
    }
    with pytest.raises(ProactiveValidationError, match="absolute quiet"):
        parse_agent_decision(
            future_absolute_payload,
            candidate=before_quiet,
            now=datetime(2026, 8, 16, 21, tzinfo=UTC),
            policy=p,
        )

    store = DurableDueJobStore()
    account_id = uuid4()
    key = sha256(b"edge-job").digest()
    with pytest.raises(ValueError, match="32 bytes"):
        store.enqueue(account_id=account_id, idempotency_key=b"short", available_at=NOW)
    store.enqueue(account_id=account_id, idempotency_key=key, available_at=NOW)
    with pytest.raises(ValueError, match="lease"):
        store.claim(now=NOW, owner=uuid4(), lease=timedelta(0))
    assert (
        store.complete(
            idempotency_key=sha256(b"missing").digest(),
            owner=uuid4(),
            fencing_token=1,
            now=NOW,
        )
        is False
    )
    with pytest.raises(KeyError, match="unknown budget"):
        BudgetLedger().commit(sha256(b"missing-budget").digest())
