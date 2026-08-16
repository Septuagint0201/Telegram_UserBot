"""Fake-first tests for proactive persistence boundary validation."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

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
    ProactiveAction,
    ReservationState,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, row: dict[str, Any] | None = None, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row

    def one(self) -> dict[str, Any]:
        assert self._row is not None
        return self._row


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
        }
    )
    assert due.account_id == account_id
    assert due.lease_owner == owner
    assert due.attempt_count == 2
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
            reservation_key=b"k" * 32,
            target=cast(ReservationState, "invalid"),
            now=NOW,
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
    session.execute.return_value = SimpleNamespace(rowcount=1)
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
    assert await repo.complete_job(idempotency_key=key, owner=uuid4(), now=NOW)
    assert await repo.recover_expired(now=NOW) == 1
    assert await repo.reap_budget(now=NOW) == 0
    repo.settle_budget = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await repo.commit_budget(reservation_key=key, now=NOW) is None
    assert await repo.release_budget(reservation_key=key, now=NOW, expired=True) is None

    candidate_id = uuid4()
    session.scalar.return_value = candidate_id
    candidate = cast(
        Candidate,
        SimpleNamespace(
            id=candidate_id,
            account_id=account_id,
            generation=1,
            membership_hash=b"m" * 32,
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
    assert (
        await repo.record_decision(
            candidate=candidate,
            decision=decision,
            output_hash=b"o" * 32,
            now=NOW,
        )
        == candidate_id
    )


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
    }
    session = AsyncMock()
    session.execute.side_effect = [_Result(rowcount=0), _Result(row), _Result(row)]
    claimed = await ProactiveRepository(cast(AsyncSession, session)).claim_next(
        now=NOW,
        owner=owner,
    )
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.lease_owner == owner
    assert claimed.attempt_count == 1
