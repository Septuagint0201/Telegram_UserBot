"""PostgreSQL persistence for the fake-first proactive worker pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    proactive_budget_buckets,
    proactive_budget_reservations,
    proactive_candidate_memberships,
    proactive_candidates,
    proactive_decision_memberships,
    proactive_decisions,
    proactive_jobs,
    proactive_occurrence_evidence,
    proactive_occurrences,
    proactive_state_transitions,
)
from telegram_userbot.domain.proactive.jobs import DueJob, DueJobState
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    BudgetLimits,
    BudgetReservation,
    Candidate,
    ProactiveAction,
    ReservationState,
)


class ProactiveRepository:
    """Durable worker state; callers decide transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue_candidate(self, candidate: Candidate, *, now: datetime) -> UUID:
        current_time = now.astimezone(UTC)
        for occurrence in candidate.occurrences:
            await self._session.execute(
                postgresql_insert(proactive_occurrences)
                .values(
                    id=occurrence.id,
                    account_id=occurrence.account_id,
                    contact_id=occurrence.contact_id,
                    conversation_id=occurrence.conversation_id,
                    occurrence_key=occurrence.occurrence_key,
                    generation=occurrence.generation,
                    reason=occurrence.reason.value,
                    state=occurrence.state.value,
                    window_start_at=occurrence.window_start_at,
                    window_end_at=occurrence.window_end_at,
                    hard_deadline_at=occurrence.hard_deadline_at,
                    timezone_name=occurrence.timezone_name,
                    local_date=occurrence.local_date,
                    importance=occurrence.importance,
                    source_type=occurrence.source_type,
                    source_id=occurrence.source_id,
                    source_version=occurrence.source_version,
                    policy_version_id=occurrence.policy_version_id,
                    quiet_bypass_possible=occurrence.quiet_bypass_possible,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(constraint="uq_proactive_occurrences_key")
            )
            for ordinal, evidence in enumerate(occurrence.evidence, 1):
                await self._session.execute(
                    postgresql_insert(proactive_occurrence_evidence)
                    .values(
                        occurrence_id=occurrence.id,
                        account_id=occurrence.account_id,
                        ordinal=ordinal,
                        source_type=evidence.source_type,
                        source_id=evidence.source_id,
                        source_version=evidence.source_version,
                        source_hash=evidence.source_hash,
                        summary=evidence.summary,
                        current=evidence.current,
                        explicit=evidence.explicit,
                    )
                    .on_conflict_do_nothing(constraint="pk_proactive_occurrence_evidence")
                )
        await self._session.execute(
            postgresql_insert(proactive_candidates)
            .values(
                id=candidate.id,
                account_id=candidate.account_id,
                contact_id=candidate.contact_id,
                conversation_id=candidate.conversation_id,
                candidate_key=candidate.candidate_key,
                generation=candidate.generation,
                membership_hash=candidate.membership_hash,
                state=candidate.state.value,
                window_start_at=candidate.window_start_at,
                window_end_at=candidate.window_end_at,
                due_at=candidate.window_start_at,
                policy_version_id=candidate.policy_version_id,
                mode_version=candidate.mode_version,
                content_revision=candidate.content_revision,
                activity_revision=candidate.activity_revision,
                created_at=current_time,
            )
            .on_conflict_do_nothing(constraint="uq_proactive_candidates_key")
        )
        persisted_candidate_id = cast(
            UUID,
            await self._session.scalar(
                select(proactive_candidates.c.id).where(
                    proactive_candidates.c.candidate_key == candidate.candidate_key
                )
            ),
        )
        for ordinal, occurrence in enumerate(candidate.occurrences, 1):
            await self._session.execute(
                postgresql_insert(proactive_candidate_memberships)
                .values(
                    candidate_id=persisted_candidate_id,
                    account_id=candidate.account_id,
                    ordinal=ordinal,
                    occurrence_id=occurrence.id,
                    occurrence_generation=occurrence.generation,
                    occurrence_key=occurrence.occurrence_key,
                )
                .on_conflict_do_nothing(constraint="pk_proactive_candidate_memberships")
            )
        await self.enqueue_job(
            account_id=candidate.account_id,
            idempotency_key=candidate.candidate_key,
            available_at=candidate.window_start_at,
            candidate_id=persisted_candidate_id,
            job_kind="candidate_due",
            now=current_time,
        )
        return persisted_candidate_id

    async def enqueue_job(  # noqa: PLR0913 - durable job identity is explicit
        self,
        *,
        account_id: UUID,
        idempotency_key: bytes,
        available_at: datetime,
        candidate_id: UUID | None = None,
        job_kind: str = "candidate_due",
        now: datetime | None = None,
    ) -> UUID:
        if len(idempotency_key) != 32 or job_kind not in {
            "candidate_due",
            "compensation_scan",
            "budget_reaper",
        }:
            raise ValueError("proactive job identity is invalid")
        job_id = uuid5(account_id, f"proactive-job:{job_kind}:{idempotency_key.hex()}")
        await self._session.execute(
            postgresql_insert(proactive_jobs)
            .values(
                id=job_id,
                account_id=account_id,
                candidate_id=candidate_id,
                job_kind=job_kind,
                idempotency_key=idempotency_key,
                available_at=available_at.astimezone(UTC),
                state="pending",
                attempt_count=0,
                created_at=(now or datetime.now(UTC)).astimezone(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_proactive_jobs_idempotency")
        )
        return job_id

    async def enqueue(self, **kwargs: Any) -> UUID:
        """Compatibility alias used by scheduler adapters."""
        return await self.enqueue_job(**kwargs)

    async def claim_next(
        self,
        *,
        now: datetime,
        owner: UUID,
        lease: timedelta = timedelta(minutes=2),
    ) -> DueJob | None:
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        await self.recover_expired(now=now)
        row = (
            (
                await self._session.execute(
                    select(proactive_jobs)
                    .where(
                        proactive_jobs.c.state == "pending",
                        proactive_jobs.c.available_at <= now.astimezone(UTC),
                    )
                    .order_by(proactive_jobs.c.available_at, proactive_jobs.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        updated = (
            (
                await self._session.execute(
                    update(proactive_jobs)
                    .where(proactive_jobs.c.id == row["id"], proactive_jobs.c.state == "pending")
                    .values(
                        state="leased",
                        lease_owner=owner,
                        lease_expires_at=now.astimezone(UTC) + lease,
                        attempt_count=proactive_jobs.c.attempt_count + 1,
                    )
                    .returning(proactive_jobs)
                )
            )
            .mappings()
            .one()
        )
        return _due_job(updated)

    async def complete(
        self, *, idempotency_key: bytes, owner: UUID, now: datetime, succeeded: bool = True
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_jobs)
                .where(
                    proactive_jobs.c.idempotency_key == idempotency_key,
                    proactive_jobs.c.state == "leased",
                    proactive_jobs.c.lease_owner == owner,
                    proactive_jobs.c.lease_expires_at > now.astimezone(UTC),
                )
                .values(
                    state="succeeded" if succeeded else "pending",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now.astimezone(UTC) if succeeded else None,
                )
            ),
        )
        return result.rowcount == 1

    async def complete_job(
        self, *, idempotency_key: bytes, owner: UUID, now: datetime, succeeded: bool = True
    ) -> bool:
        return await self.complete(
            idempotency_key=idempotency_key,
            owner=owner,
            now=now,
            succeeded=succeeded,
        )

    async def recover_expired(self, *, now: datetime) -> int:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_jobs)
                .where(
                    proactive_jobs.c.state == "leased",
                    proactive_jobs.c.lease_expires_at <= now.astimezone(UTC),
                )
                .values(state="pending", lease_owner=None, lease_expires_at=None)
            ),
        )
        return result.rowcount

    async def reserve_budget(  # noqa: PLR0913 - all budget scopes are part of the reservation key
        self,
        *,
        account_id: UUID,
        contact_id: UUID,
        local_date: date,
        limits: BudgetLimits,
        expires_at: datetime,
        reservation_key: bytes,
        candidate_id: UUID | None = None,
        bypass: bool = False,
    ) -> BudgetReservation | None:
        if len(reservation_key) != 32 or expires_at.tzinfo is None:
            raise ValueError("budget reservation identity is invalid")
        if bypass and limits.bypass_daily == 0:
            return None
        existing = (
            (
                await self._session.execute(
                    select(proactive_budget_reservations)
                    .where(proactive_budget_reservations.c.reservation_key == reservation_key)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return _reservation(existing)
        scopes = [
            (None, "account_daily", limits.account_daily),
            (contact_id, "contact_daily", limits.contact_daily),
        ]
        if bypass:
            scopes.append((contact_id, "contact_bypass", limits.bypass_daily))
        buckets: list[Any] = []
        for scoped_contact, scope, limit in scopes:
            await self._session.execute(
                postgresql_insert(proactive_budget_buckets)
                .values(
                    id=uuid4(),
                    account_id=account_id,
                    contact_id=scoped_contact,
                    scope=scope,
                    local_date=local_date,
                    limit_value=limit,
                    held_count=0,
                    committed_count=0,
                    version=1,
                )
                .on_conflict_do_nothing(constraint="uq_proactive_budget_bucket_identity")
            )
            row = (
                (
                    await self._session.execute(
                        select(proactive_budget_buckets)
                        .where(
                            proactive_budget_buckets.c.account_id == account_id,
                            proactive_budget_buckets.c.contact_id.is_(scoped_contact)
                            if scoped_contact is None
                            else proactive_budget_buckets.c.contact_id == scoped_contact,
                            proactive_budget_buckets.c.scope == scope,
                            proactive_budget_buckets.c.local_date == local_date,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            buckets.append(row)
        if any(row["held_count"] + row["committed_count"] >= row["limit_value"] for row in buckets):
            return None
        reservation_id = uuid5(account_id, f"proactive-reservation:{reservation_key.hex()}")
        await self._session.execute(
            insert(proactive_budget_reservations).values(
                id=reservation_id,
                account_id=account_id,
                contact_id=contact_id,
                candidate_id=candidate_id,
                reservation_key=reservation_key,
                local_date=local_date,
                bypass=bypass,
                state="held",
                expires_at=expires_at.astimezone(UTC),
            )
        )
        for row in buckets:
            await self._session.execute(
                update(proactive_budget_buckets)
                .where(proactive_budget_buckets.c.id == row["id"])
                .values(
                    held_count=proactive_budget_buckets.c.held_count + 1,
                    version=proactive_budget_buckets.c.version + 1,
                )
            )
        return BudgetReservation(
            reservation_id,
            reservation_key,
            account_id,
            contact_id,
            local_date,
            bypass,
            ReservationState.HELD,
            expires_at.astimezone(UTC),
        )

    async def settle_budget(
        self,
        *,
        reservation_key: bytes,
        target: ReservationState,
        now: datetime,
    ) -> BudgetReservation | None:
        if target not in {
            ReservationState.COMMITTED,
            ReservationState.RELEASED,
            ReservationState.EXPIRED,
            ReservationState.SEND_UNKNOWN,
        }:
            raise ValueError("invalid budget settlement")
        row = (
            (
                await self._session.execute(
                    select(proactive_budget_reservations)
                    .where(proactive_budget_reservations.c.reservation_key == reservation_key)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        current = ReservationState(row["state"])
        if current in {
            ReservationState.COMMITTED,
            ReservationState.SEND_UNKNOWN,
            ReservationState.RELEASED,
            ReservationState.EXPIRED,
        }:
            return _reservation(row)
        delta_committed = target in {ReservationState.COMMITTED, ReservationState.SEND_UNKNOWN}
        await self._session.execute(
            update(proactive_budget_reservations)
            .where(
                proactive_budget_reservations.c.id == row["id"],
                proactive_budget_reservations.c.state == "held",
            )
            .values(
                state=target.value, committed_at=now.astimezone(UTC) if delta_committed else None
            )
        )
        scopes = [(None, "account_daily"), (row["contact_id"], "contact_daily")]
        if row["bypass"]:
            scopes.append((row["contact_id"], "contact_bypass"))
        for scoped_contact, scope in scopes:
            await self._session.execute(
                update(proactive_budget_buckets)
                .where(
                    proactive_budget_buckets.c.account_id == row["account_id"],
                    proactive_budget_buckets.c.contact_id.is_(None)
                    if scoped_contact is None
                    else proactive_budget_buckets.c.contact_id == scoped_contact,
                    proactive_budget_buckets.c.scope == scope,
                    proactive_budget_buckets.c.local_date == row["local_date"],
                )
                .values(
                    held_count=proactive_budget_buckets.c.held_count - 1,
                    committed_count=proactive_budget_buckets.c.committed_count
                    + (1 if delta_committed else 0),
                    version=proactive_budget_buckets.c.version + 1,
                )
            )
        return BudgetReservation(
            cast(UUID, row["id"]),
            reservation_key,
            cast(UUID, row["account_id"]),
            cast(UUID, row["contact_id"]),
            cast(date, row["local_date"]),
            cast(bool, row["bypass"]),
            target,
            cast(datetime, row["expires_at"]),
        )

    async def commit_budget(
        self, *, reservation_key: bytes, now: datetime, unknown: bool = False
    ) -> BudgetReservation | None:
        return await self.settle_budget(
            reservation_key=reservation_key,
            target=ReservationState.SEND_UNKNOWN if unknown else ReservationState.COMMITTED,
            now=now,
        )

    async def release_budget(
        self, *, reservation_key: bytes, now: datetime, expired: bool = False
    ) -> BudgetReservation | None:
        return await self.settle_budget(
            reservation_key=reservation_key,
            target=ReservationState.EXPIRED if expired else ReservationState.RELEASED,
            now=now,
        )

    async def reap_budget(self, *, now: datetime) -> int:
        keys = tuple(
            await self._session.scalars(
                select(proactive_budget_reservations.c.reservation_key).where(
                    proactive_budget_reservations.c.state == "held",
                    proactive_budget_reservations.c.expires_at <= now.astimezone(UTC),
                )
            )
        )
        changed = 0
        for key in keys:
            if await self.release_budget(reservation_key=key, now=now, expired=True) is not None:
                changed += 1
        return changed

    async def record_decision(
        self,
        *,
        candidate: Candidate,
        decision: AgentDecision,
        output_hash: bytes,
        now: datetime,
    ) -> UUID:
        if decision.candidate_id != candidate.id or len(output_hash) != 32:
            raise ValueError("decision does not bind candidate")
        candidate_occurrence_ids = {occurrence.id for occurrence in candidate.occurrences}
        if not set(decision.selected_occurrence_ids) <= candidate_occurrence_ids:
            raise ValueError("decision selects an occurrence outside the candidate")
        decision_id = uuid5(candidate.id, f"proactive-decision:{output_hash.hex()}")
        await self._session.execute(
            postgresql_insert(proactive_decisions)
            .values(
                id=decision_id,
                account_id=candidate.account_id,
                candidate_id=candidate.id,
                generation=candidate.generation,
                action=decision.action.value,
                decision_code=decision.decision_code,
                topic=decision.topic,
                priority=decision.priority,
                defer_until=decision.defer_until,
                output_hash=output_hash,
                state="accepted",
                created_at=now.astimezone(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_proactive_decisions_id_account")
        )
        for ordinal, occurrence_id in enumerate(decision.selected_occurrence_ids, 1):
            await self._session.execute(
                postgresql_insert(proactive_decision_memberships)
                .values(
                    decision_id=decision_id,
                    account_id=candidate.account_id,
                    ordinal=ordinal,
                    occurrence_id=occurrence_id,
                )
                .on_conflict_do_nothing(constraint="pk_proactive_decision_memberships")
            )
        target_state = {
            ProactiveAction.SEND_NOW: "send_selected",
            ProactiveAction.DEFER_ONCE: "deferred_once",
            ProactiveAction.NONE: "evaluated_none",
        }[decision.action]
        await self._session.execute(
            update(proactive_candidates)
            .where(proactive_candidates.c.id == candidate.id)
            .values(state=target_state)
        )
        await self._session.execute(
            insert(proactive_state_transitions).values(
                id=uuid4(),
                account_id=candidate.account_id,
                candidate_id=candidate.id,
                from_state=candidate.state.value,
                to_state=target_state,
                event="agent_decision",
                reason=decision.decision_code,
                actor="agent",
                created_at=now.astimezone(UTC),
            )
        )
        return decision_id


def _due_job(row: Any) -> DueJob:
    return DueJob(
        id=cast(UUID, row["id"]),
        account_id=cast(UUID, row["account_id"]),
        idempotency_key=cast(bytes, row["idempotency_key"]),
        available_at=cast(datetime, row["available_at"]),
        state=DueJobState(row["state"]),
        lease_owner=cast(UUID | None, row["lease_owner"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        attempt_count=cast(int, row["attempt_count"]),
    )


def _reservation(row: Any) -> BudgetReservation:
    return BudgetReservation(
        cast(UUID, row["id"]),
        cast(bytes, row["reservation_key"]),
        cast(UUID, row["account_id"]),
        cast(UUID, row["contact_id"]),
        cast(date, row["local_date"]),
        cast(bool, row["bypass"]),
        ReservationState(row["state"]),
        cast(datetime, row["expires_at"]),
    )
