"""Fake-first tests for proactive persistence boundary validation."""

from datetime import UTC, date, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.proactive_repository import (
    ProactiveRepository,
    _due_job,
    _reservation,
)
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    BudgetLimits,
    Candidate,
    CandidateState,
    OccurrenceState,
    ProactiveAction,
    ReasonCode,
    ReservationState,
    membership_digest,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _NoOffset(tzinfo):
    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        return None

    def dst(self, _value: datetime | None) -> timedelta | None:
        return None

    def tzname(self, _value: datetime | None) -> str | None:
        return "no-offset"


class _Result:
    def __init__(
        self,
        row: dict[str, Any] | None = None,
        rowcount: int = 0,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row

    def one(self) -> dict[str, Any]:
        assert self._row is not None
        return self._row

    def all(self) -> list[dict[str, Any]]:
        return self._rows


def repository() -> ProactiveRepository:
    return ProactiveRepository(cast(AsyncSession, object()))


@pytest.mark.unit
def test_proactive_persistence_row_converters_preserve_durable_state() -> None:
    account_id, owner = uuid4(), uuid4()
    key = b"k" * 32
    due = _due_job(
        {
            "id": uuid4(),
            "account_id": account_id,
            "idempotency_key": key,
            "available_at": NOW,
            "state": "leased",
            "lease_owner": owner,
            "lease_expires_at": NOW + timedelta(minutes=1),
            "attempt_count": 2,
            "fencing_token": 3,
        }
    )
    assert due.account_id == account_id
    assert due.lease_owner == owner
    assert due.attempt_count == 2
    assert due.fencing_token == 3
    reservation = _reservation(
        {
            "id": uuid4(),
            "reservation_key": key,
            "account_id": account_id,
            "contact_id": uuid4(),
            "local_date": date(2026, 8, 16),
            "bypass": True,
            "state": "send_unknown",
            "expires_at": NOW,
        }
    )
    assert reservation.state is ReservationState.SEND_UNKNOWN
    assert reservation.bypass


@pytest.mark.asyncio
async def test_proactive_persistence_rejects_invalid_inputs_before_database_access() -> None:
    repo = repository()
    account_id, contact_id = uuid4(), uuid4()
    with pytest.raises(ValueError, match="job identity"):
        await repo.enqueue_job(
            account_id=account_id,
            idempotency_key=b"short",
            available_at=NOW,
        )
    with pytest.raises(ValueError, match="lease"):
        await repo.claim_next(now=NOW, owner=uuid4(), lease=timedelta(0))
    with pytest.raises(ValueError, match="reservation identity"):
        await repo.reserve_budget(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=NOW,
            reservation_key=b"short",
        )
    with pytest.raises(ValueError, match="invalid budget settlement"):
        await repo.settle_budget(
            account_id=account_id,
            reservation_key=b"k" * 32,
            target=cast(ReservationState, "invalid"),
            now=NOW,
        )

    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.enqueue_job(
            account_id=account_id,
            idempotency_key=b"n" * 32,
            available_at=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.enqueue_job(
            account_id=account_id,
            idempotency_key=b"o" * 32,
            available_at=NOW,
            now=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.claim_next(now=naive, owner=uuid4())
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.recover_expired(now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.reserve_budget(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=naive,
            reservation_key=b"p" * 32,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.settle_budget(
            account_id=account_id,
            reservation_key=b"k" * 32,
            target=ReservationState.COMMITTED,
            now=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.reap_budget(now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.reserve_budget(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=datetime(2026, 8, 16, 12, tzinfo=_NoOffset()),
            reservation_key=b"q" * 32,
        )


@pytest.mark.asyncio
async def test_proactive_persistence_rejects_unbound_decisions_without_database_access() -> None:
    repo = repository()
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=uuid4(),
            account_id=uuid4(),
            generation=1,
            membership_hash=b"m" * 32,
            occurrences=(),
        ),
    )
    decision = AgentDecision(
        uuid4(),
        ProactiveAction.NONE,
        "not_natural_now",
        (),
        None,
        0,
    )
    with pytest.raises(ValueError, match="does not bind"):
        await repo.record_decision(
            candidate=candidate,
            decision=decision,
            output_hash=b"o" * 32,
            now=NOW,
        )
    selected = uuid4()
    bound = AgentDecision(
        candidate.id,
        ProactiveAction.SEND_NOW,
        "timely_support",
        (selected,),
        "topic",
        0.5,
    )
    with pytest.raises(ValueError, match="outside the candidate"):
        await repo.record_decision(
            candidate=candidate,
            decision=bound,
            output_hash=b"o" * 32,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_proactive_persistence_success_paths_are_transaction_shaped() -> None:
    session = AsyncMock()
    session.execute.return_value = _Result(rowcount=1)
    session.scalars.return_value = []
    repo = ProactiveRepository(cast(AsyncSession, session))
    account_id = uuid4()
    key = b"j" * 32
    job_id = await repo.enqueue_job(
        account_id=account_id,
        idempotency_key=key,
        available_at=NOW,
    )
    assert job_id
    assert await repo.enqueue(
        account_id=account_id,
        idempotency_key=b"a" * 32,
        available_at=NOW,
    )
    assert await repo.complete_job(idempotency_key=key, owner=uuid4(), fencing_token=1, now=NOW)
    assert await repo.recover_expired(now=NOW) == 1
    assert await repo.reap_budget(now=NOW) == 0
    repo.settle_budget = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await repo.commit_budget(account_id=account_id, reservation_key=key, now=NOW) is None
    assert (
        await repo.release_budget(account_id=account_id, reservation_key=key, now=NOW, expired=True)
        is None
    )

    candidate_id = uuid4()
    empty_membership = membership_digest(())
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=candidate_id,
            account_id=account_id,
            generation=1,
            membership_hash=empty_membership,
            occurrences=(),
        ),
    )
    decision = AgentDecision(
        candidate_id,
        ProactiveAction.NONE,
        "not_natural_now",
        (),
        None,
        0,
    )
    session.execute.side_effect = [
        _Result(
            {
                "id": candidate_id,
                "generation": 1,
                "membership_hash": empty_membership,
                "state": "open",
            }
        ),
        _Result(),
        _Result(),
        _Result(rowcount=1),
        _Result(rowcount=1),
        _Result(rowcount=1),
    ]
    output_hash = b"o" * 32
    assert await repo.record_decision(
        candidate=candidate,
        decision=decision,
        output_hash=output_hash,
        now=NOW,
    ) == uuid5(candidate_id, f"proactive-decision:{output_hash.hex()}")


@pytest.mark.asyncio
async def test_proactive_persistence_claim_updates_a_pending_job_with_a_lease() -> None:
    account_id, job_id, owner = uuid4(), uuid4(), uuid4()
    key = b"q" * 32
    row = {
        "id": job_id,
        "account_id": account_id,
        "idempotency_key": key,
        "available_at": NOW,
        "state": "leased",
        "lease_owner": owner,
        "lease_expires_at": NOW + timedelta(minutes=2),
        "attempt_count": 1,
        "fencing_token": 1,
    }
    session = AsyncMock()
    session.execute.side_effect = [
        _Result(rowcount=0),
        _Result(rowcount=0),
        _Result(row),
        _Result(row),
    ]
    claimed = await ProactiveRepository(cast(AsyncSession, session)).claim_next(
        now=NOW,
        owner=owner,
    )
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.lease_owner == owner
    assert claimed.attempt_count == 1
    assert claimed.fencing_token == 1


@pytest.mark.asyncio
async def test_proactive_job_replay_rejects_a_different_durable_identity() -> None:
    account_id = uuid4()
    key = b"j" * 32
    expected_id = uuid5(account_id, f"proactive-job:candidate_due:{key.hex()}")
    session = AsyncMock()
    session.execute.return_value = _Result(
        {
            "id": expected_id,
            "account_id": uuid4(),
            "candidate_id": None,
            "job_kind": "candidate_due",
            "available_at": NOW,
        }
    )
    repo = ProactiveRepository(cast(AsyncSession, session))

    with pytest.raises(ValueError, match="job replay"):
        await repo.enqueue_job(
            account_id=account_id,
            idempotency_key=key,
            available_at=NOW,
        )


@pytest.mark.asyncio
async def test_proactive_candidate_enqueue_validates_scope_membership_and_persists_snapshot() -> (
    None
):
    account_id, contact_id, conversation_id = uuid4(), uuid4(), uuid4()
    occurrence_id = uuid4()
    occurrence = SimpleNamespace(
        id=occurrence_id,
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        occurrence_key=b"o" * 32,
        generation=1,
        reason=ReasonCode.PROMISE_DUE,
        state=OccurrenceState.SCHEDULED,
        window_start_at=NOW,
        window_end_at=NOW + timedelta(hours=1),
        hard_deadline_at=NOW + timedelta(hours=1),
        timezone_name="UTC",
        local_date=NOW.date(),
        importance=0.8,
        source_type="rule",
        source_id=uuid4(),
        source_version="v1",
        policy_version_id=None,
        quiet_bypass_possible=False,
        evidence=(
            SimpleNamespace(
                source_type="rule",
                source_id=uuid4(),
                source_version="v1",
                source_hash=b"h" * 32,
                summary="synthetic",
                current=True,
                explicit=True,
            ),
        ),
    )
    occurrences = cast(tuple[Any, ...], (occurrence,))
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=uuid4(),
            account_id=account_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            candidate_key=b"c" * 32,
            generation=1,
            membership_hash=membership_digest(occurrences),
            occurrences=occurrences,
            window_start_at=NOW,
            window_end_at=NOW + timedelta(hours=1),
            policy_version_id=None,
            timezone_name="UTC",
            mode_version=1,
            content_revision=0,
            activity_revision=0,
            state=CandidateState.OPEN,
        ),
    )
    session = AsyncMock()
    occurrence_row = {
        "id": occurrence.id,
        "account_id": occurrence.account_id,
        "contact_id": occurrence.contact_id,
        "conversation_id": occurrence.conversation_id,
        "occurrence_key": occurrence.occurrence_key,
        "generation": occurrence.generation,
        "reason": occurrence.reason.value,
        "window_start_at": occurrence.window_start_at,
        "window_end_at": occurrence.window_end_at,
        "hard_deadline_at": occurrence.hard_deadline_at,
        "timezone_name": occurrence.timezone_name,
        "local_date": occurrence.local_date,
        "importance": occurrence.importance,
        "source_type": occurrence.source_type,
        "source_id": occurrence.source_id,
        "source_version": occurrence.source_version,
        "policy_version_id": occurrence.policy_version_id,
        "quiet_bypass_possible": occurrence.quiet_bypass_possible,
    }
    candidate_row = {
        "id": candidate.id,
        "account_id": candidate.account_id,
        "contact_id": candidate.contact_id,
        "conversation_id": candidate.conversation_id,
        "candidate_key": candidate.candidate_key,
        "generation": candidate.generation,
        "membership_hash": candidate.membership_hash,
        "window_start_at": candidate.window_start_at,
        "window_end_at": candidate.window_end_at,
        "policy_version_id": candidate.policy_version_id,
        "mode_version": candidate.mode_version,
        "content_revision": candidate.content_revision,
        "activity_revision": candidate.activity_revision,
    }
    evidence = occurrence.evidence[0]
    evidence_row = {
        "occurrence_id": occurrence.id,
        "account_id": occurrence.account_id,
        "ordinal": 1,
        "source_type": evidence.source_type,
        "source_id": evidence.source_id,
        "source_version": evidence.source_version,
        "source_hash": evidence.source_hash,
        "summary": evidence.summary,
        "current": evidence.current,
        "explicit": evidence.explicit,
    }
    membership_row = {
        "candidate_id": candidate.id,
        "account_id": candidate.account_id,
        "ordinal": 1,
        "occurrence_id": occurrence.id,
        "occurrence_generation": occurrence.generation,
        "occurrence_key": occurrence.occurrence_key,
    }
    session.execute.side_effect = [
        _Result(),
        _Result(occurrence_row),
        _Result(),
        _Result(rows=[evidence_row]),
        _Result(),
        _Result(candidate_row),
        _Result(),
        _Result(rows=[membership_row]),
    ]
    repo = ProactiveRepository(cast(AsyncSession, session))
    repo.enqueue_job = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    assert await repo.enqueue_candidate(candidate, now=NOW) == candidate.id
    assert repo.enqueue_job.await_count == 1

    session.execute.side_effect = [
        _Result(),
        _Result(occurrence_row | {"account_id": uuid4()}),
    ]
    with pytest.raises(ValueError, match="occurrence replay"):
        await repo.enqueue_candidate(candidate, now=NOW)

    session.execute.side_effect = [
        _Result(),
        _Result(occurrence_row),
        _Result(),
        _Result(rows=[evidence_row | {"summary": "different"}]),
    ]
    with pytest.raises(ValueError, match="evidence replay"):
        await repo.enqueue_candidate(candidate, now=NOW)

    session.execute.side_effect = [
        _Result(),
        _Result(occurrence_row),
        _Result(),
        _Result(rows=[evidence_row]),
        _Result(),
        _Result(candidate_row),
        _Result(),
        _Result(rows=[membership_row | {"occurrence_generation": 2}]),
    ]
    with pytest.raises(ValueError, match="membership replay"):
        await repo.enqueue_candidate(candidate, now=NOW)

    bad_scope = cast(Candidate, SimpleNamespace(**{**candidate.__dict__, "contact_id": uuid4()}))
    with pytest.raises(ValueError, match="scope"):
        await repo.enqueue_candidate(bad_scope, now=NOW)
    duplicate = cast(
        Candidate,
        SimpleNamespace(**{**candidate.__dict__, "occurrences": (occurrence, occurrence)}),
    )
    with pytest.raises(ValueError, match="scope"):
        await repo.enqueue_candidate(duplicate, now=NOW)
    bad_hash = cast(
        Candidate, SimpleNamespace(**{**candidate.__dict__, "membership_hash": b"x" * 32})
    )
    with pytest.raises(ValueError, match="scope"):
        await repo.enqueue_candidate(bad_hash, now=NOW)


@pytest.mark.asyncio
async def test_proactive_budget_replay_is_scope_bound_and_settlement_is_terminal() -> None:
    account_id, contact_id = uuid4(), uuid4()
    key = b"r" * 32
    row = {
        "id": uuid4(),
        "reservation_key": key,
        "account_id": account_id,
        "contact_id": contact_id,
        "local_date": NOW.date(),
        "bypass": False,
        "candidate_id": None,
        "state": "held",
        "expires_at": NOW + timedelta(hours=1),
    }
    session = AsyncMock()
    session.execute.return_value = _Result(row)
    repo = ProactiveRepository(cast(AsyncSession, session))
    with pytest.raises(ValueError, match="another scope"):
        await repo.reserve_budget(
            account_id=uuid4(),
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=NOW + timedelta(hours=1),
            reservation_key=key,
        )
    replay = await repo.reserve_budget(
        account_id=account_id,
        contact_id=contact_id,
        local_date=NOW.date(),
        limits=BudgetLimits(1, 1),
        expires_at=NOW + timedelta(hours=1),
        reservation_key=key,
    )
    assert replay is not None
    assert replay.state is ReservationState.HELD
    session.execute.return_value = _Result(dict(row, state="released"))
    assert (
        await repo.reserve_budget(
            account_id=account_id,
            contact_id=contact_id,
            local_date=NOW.date(),
            limits=BudgetLimits(1, 1),
            expires_at=NOW + timedelta(hours=1),
            reservation_key=key,
        )
        is None
    )
    session.execute.return_value = _Result(None)
    assert (
        await repo.settle_budget(
            account_id=account_id,
            reservation_key=key,
            target=ReservationState.COMMITTED,
            now=NOW,
        )
        is None
    )
    terminal = dict(row, state="committed")
    session.execute.return_value = _Result(terminal)
    result = await repo.settle_budget(
        account_id=account_id,
        reservation_key=key,
        target=ReservationState.RELEASED,
        now=NOW,
    )
    assert result is not None
    assert result.state is ReservationState.COMMITTED

    session.execute.side_effect = [
        _Result(row),
        _Result(rowcount=1),
        _Result(rowcount=1),
        _Result(rowcount=1),
    ]
    committed_after_expiry = await repo.settle_budget(
        account_id=account_id,
        reservation_key=key,
        target=ReservationState.COMMITTED,
        now=NOW + timedelta(hours=2),
    )
    assert committed_after_expiry is not None
    assert committed_after_expiry.state is ReservationState.COMMITTED

    session.execute.side_effect = [
        _Result(row),
        _Result(rowcount=1),
        _Result(rowcount=1),
        _Result(rowcount=1),
    ]
    released_after_expiry = await repo.settle_budget(
        account_id=account_id,
        reservation_key=key,
        target=ReservationState.RELEASED,
        now=NOW + timedelta(hours=2),
    )
    assert released_after_expiry is not None
    assert released_after_expiry.state is ReservationState.EXPIRED


@pytest.mark.asyncio
async def test_proactive_decision_replay_rejects_a_different_durable_identity() -> None:
    account_id, candidate_id = uuid4(), uuid4()
    membership_hash = membership_digest(())
    output_hash = b"d" * 32
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=candidate_id,
            account_id=account_id,
            generation=1,
            membership_hash=membership_hash,
            occurrences=(),
        ),
    )
    decision = AgentDecision(
        candidate_id,
        ProactiveAction.NONE,
        "not_natural_now",
        (),
        None,
        0,
    )
    decision_id = uuid5(candidate_id, f"proactive-decision:{output_hash.hex()}")
    session = AsyncMock()
    session.execute.side_effect = [
        _Result(
            {
                "id": candidate_id,
                "generation": 1,
                "membership_hash": membership_hash,
                "state": "open",
            }
        ),
        _Result(
            {
                "id": decision_id,
                "account_id": account_id,
                "candidate_id": candidate_id,
                "generation": 1,
                "action": ProactiveAction.SEND_NOW.value,
                "decision_code": decision.decision_code,
                "topic": decision.topic,
                "priority": decision.priority,
                "defer_until": decision.defer_until,
                "output_hash": output_hash,
            }
        ),
    ]
    repo = ProactiveRepository(cast(AsyncSession, session))

    with pytest.raises(ValueError, match="decision replay"):
        await repo.record_decision(
            candidate=candidate,
            decision=decision,
            output_hash=output_hash,
            now=NOW,
        )

    matching_decision_row = {
        "id": decision_id,
        "account_id": account_id,
        "candidate_id": candidate_id,
        "generation": 1,
        "action": decision.action.value,
        "decision_code": decision.decision_code,
        "topic": decision.topic,
        "priority": decision.priority,
        "defer_until": decision.defer_until,
        "output_hash": output_hash,
    }
    session.execute.side_effect = [
        _Result(
            {
                "id": candidate_id,
                "generation": 1,
                "membership_hash": membership_hash,
                "state": "open",
            }
        ),
        _Result(matching_decision_row),
        _Result(
            rows=[
                {
                    "decision_id": decision_id,
                    "account_id": account_id,
                    "ordinal": 1,
                    "occurrence_id": uuid4(),
                }
            ]
        ),
    ]
    with pytest.raises(ValueError, match="decision membership replay"):
        await repo.record_decision(
            candidate=candidate,
            decision=decision,
            output_hash=output_hash,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_proactive_decision_rejects_a_second_decision_for_terminal_candidate() -> None:
    account_id, candidate_id = uuid4(), uuid4()
    membership_hash = membership_digest(())
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=candidate_id,
            account_id=account_id,
            generation=1,
            membership_hash=membership_hash,
            occurrences=(),
        ),
    )
    decision = AgentDecision(
        candidate_id,
        ProactiveAction.NONE,
        "not_natural_now",
        (),
        None,
        0,
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _Result(
            {
                "id": candidate_id,
                "generation": 1,
                "membership_hash": membership_hash,
                "state": "evaluated_none",
            }
        ),
        _Result(None),
    ]
    repo = ProactiveRepository(cast(AsyncSession, session))

    with pytest.raises(ValueError, match="already terminal"):
        await repo.record_decision(
            candidate=candidate,
            decision=decision,
            output_hash=b"n" * 32,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_budget_reaper_only_selects_candidate_states_that_prove_no_side_effect() -> None:
    account_id = uuid4()
    reservation_key = b"z" * 32
    session = AsyncMock()
    session.execute.return_value = _Result(
        rows=[{"account_id": account_id, "reservation_key": reservation_key}]
    )
    repo = ProactiveRepository(cast(AsyncSession, session))
    repo.release_budget = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]

    assert await repo.reap_budget(now=NOW) == 1
    statement = session.execute.await_args.args[0]
    rendered = str(statement)
    assert "JOIN proactive_candidates" in rendered
    assert "proactive_candidates.state" in rendered
    repo.release_budget.assert_awaited_once_with(
        account_id=account_id,
        reservation_key=reservation_key,
        now=NOW,
        expired=True,
    )
