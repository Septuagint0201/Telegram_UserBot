"""M7 proactive domain contracts use only synthetic facts and deterministic time."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
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
    Candidate,
    ContactSettings,
    ProactiveAction,
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
    assert store.complete(idempotency_key=key, owner=uuid4(), now=NOW) is False
    assert store.compensate(now=NOW + timedelta(seconds=1))
    lease2 = store.claim(now=NOW + timedelta(seconds=1), owner=owner)
    assert lease2 is not None
    assert lease2.attempt_count == 2
    assert store.complete(idempotency_key=key, owner=owner, now=NOW)
    assert store.claim(now=NOW + timedelta(days=1), owner=uuid4()) is None
    expired = store.enqueue(
        account_id=account_id, idempotency_key=sha256(b"late").digest(), available_at=NOW
    )
    assert store.expire(now=NOW + timedelta(days=1), window_end=NOW) == 1
    assert expired.state is DueJobState.PENDING


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
