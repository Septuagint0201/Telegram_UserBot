"""PostgreSQL persistence for the fake-first proactive worker pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import case, insert, select, text, update
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
    membership_digest,
)


class ProactiveRepository:
    """Durable worker state; callers decide transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue_candidate(self, candidate: Candidate, *, now: datetime) -> UUID:
        current_time = now.astimezone(UTC)
        occurrence_keys = [occurrence.occurrence_key for occurrence in candidate.occurrences]
        if (
            any(
                occurrence.account_id != candidate.account_id
                or occurrence.contact_id != candidate.contact_id
                or occurrence.conversation_id != candidate.conversation_id
                for occurrence in candidate.occurrences
            )
            or len({occurrence.id for occurrence in candidate.occurrences})
            != len(candidate.occurrences)
            or len(set(occurrence_keys)) != len(occurrence_keys)
            or candidate.membership_hash != membership_digest(candidate.occurrences)
        ):
            raise ValueError("candidate occurrence scope or membership snapshot is invalid")
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
            persisted_occurrence = (
                (
                    await self._session.execute(
                        select(proactive_occurrences)
                        .where(proactive_occurrences.c.occurrence_key == occurrence.occurrence_key)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if persisted_occurrence is None or not _occurrence_matches(
                persisted_occurrence, occurrence
            ):
                raise ValueError("occurrence replay does not match durable identity")
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
        persisted_candidate = (
            (
                await self._session.execute(
                    select(proactive_candidates)
                    .where(proactive_candidates.c.candidate_key == candidate.candidate_key)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if persisted_candidate is None or not _candidate_matches(persisted_candidate, candidate):
            raise ValueError("candidate replay does not match durable identity")
        persisted_candidate_id = cast(UUID, persisted_candidate["id"])
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
        available_time = available_at.astimezone(UTC)
        job_id = uuid5(account_id, f"proactive-job:{job_kind}:{idempotency_key.hex()}")
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"proactive_job:{idempotency_key.hex()}"},
        )
        existing = (
            (
                await self._session.execute(
                    select(proactive_jobs)
                    .where(proactive_jobs.c.idempotency_key == idempotency_key)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing["id"] != job_id
                or existing["account_id"] != account_id
                or existing["candidate_id"] != candidate_id
                or existing["job_kind"] != job_kind
                or existing["available_at"] != available_time
            ):
                raise ValueError("proactive job replay does not match durable identity")
            return job_id
        if candidate_id is not None:
            candidate_exists = await self._session.scalar(
                select(proactive_candidates.c.id).where(
                    proactive_candidates.c.id == candidate_id,
                    proactive_candidates.c.account_id == account_id,
                )
            )
            if candidate_exists is None:
                raise ValueError("proactive job candidate is outside the requested scope")
        await self._session.execute(
            postgresql_insert(proactive_jobs)
            .values(
                id=job_id,
                account_id=account_id,
                candidate_id=candidate_id,
                job_kind=job_kind,
                idempotency_key=idempotency_key,
                available_at=available_time,
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
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> DueJob | None:
        if lease <= timedelta(0) or max_attempts <= 0 or retry_delay <= timedelta(0):
            raise ValueError("proactive lease policy is invalid")
        current_time = now.astimezone(UTC)
        await self.recover_expired(
            now=current_time,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )
        await self._session.execute(
            update(proactive_jobs)
            .where(
                proactive_jobs.c.state.in_(("pending", "retry_wait")),
                proactive_jobs.c.attempt_count >= max_attempts,
            )
            .values(state="dead_letter")
        )
        row = (
            (
                await self._session.execute(
                    select(proactive_jobs)
                    .where(
                        proactive_jobs.c.state.in_(("pending", "retry_wait")),
                        proactive_jobs.c.available_at <= current_time,
                        proactive_jobs.c.attempt_count < max_attempts,
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
                    .where(
                        proactive_jobs.c.id == row["id"],
                        proactive_jobs.c.state == row["state"],
                    )
                    .values(
                        state="leased",
                        lease_owner=owner,
                        lease_expires_at=current_time + lease,
                        attempt_count=proactive_jobs.c.attempt_count + 1,
                        fencing_token=proactive_jobs.c.fencing_token + 1,
                    )
                    .returning(proactive_jobs)
                )
            )
            .mappings()
            .one()
        )
        return _due_job(updated)

    async def complete(  # noqa: PLR0913 - lease and retry policy are explicit
        self,
        *,
        idempotency_key: bytes,
        owner: UUID,
        fencing_token: int,
        now: datetime,
        succeeded: bool = True,
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> bool:
        if fencing_token <= 0 or max_attempts <= 0 or retry_delay <= timedelta(0):
            raise ValueError("proactive completion policy is invalid")
        next_state = (
            "succeeded"
            if succeeded
            else case(
                (proactive_jobs.c.attempt_count >= max_attempts, "dead_letter"),
                else_="retry_wait",
            )
        )
        next_available_at: Any = proactive_jobs.c.available_at
        if not succeeded:
            next_available_at = case(
                (
                    proactive_jobs.c.attempt_count >= max_attempts,
                    proactive_jobs.c.available_at,
                ),
                else_=now.astimezone(UTC) + retry_delay,
            )
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_jobs)
                .where(
                    proactive_jobs.c.idempotency_key == idempotency_key,
                    proactive_jobs.c.state == "leased",
                    proactive_jobs.c.lease_owner == owner,
                    proactive_jobs.c.fencing_token == fencing_token,
                    proactive_jobs.c.lease_expires_at > now.astimezone(UTC),
                )
                .values(
                    state=next_state,
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now.astimezone(UTC) if succeeded else None,
                    available_at=next_available_at,
                )
            ),
        )
        return result.rowcount == 1

    async def complete_job(  # noqa: PLR0913 - compatibility alias preserves policy
        self,
        *,
        idempotency_key: bytes,
        owner: UUID,
        fencing_token: int,
        now: datetime,
        succeeded: bool = True,
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> bool:
        return await self.complete(
            idempotency_key=idempotency_key,
            owner=owner,
            fencing_token=fencing_token,
            now=now,
            succeeded=succeeded,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )

    async def recover_expired(
        self,
        *,
        now: datetime,
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> int:
        if max_attempts <= 0 or retry_delay <= timedelta(0):
            raise ValueError("proactive recovery policy is invalid")
        current_time = now.astimezone(UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_jobs)
                .where(
                    proactive_jobs.c.state == "leased",
                    proactive_jobs.c.lease_expires_at <= current_time,
                )
                .values(
                    state=case(
                        (proactive_jobs.c.attempt_count >= max_attempts, "dead_letter"),
                        else_="retry_wait",
                    ),
                    available_at=case(
                        (
                            proactive_jobs.c.attempt_count >= max_attempts,
                            proactive_jobs.c.available_at,
                        ),
                        else_=current_time + retry_delay,
                    ),
                    lease_owner=None,
                    lease_expires_at=None,
                )
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
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"proactive_budget:{reservation_key.hex()}"},
        )
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
            if (
                existing["account_id"] != account_id
                or existing["contact_id"] != contact_id
                or existing["local_date"] != local_date
                or existing["bypass"] != bypass
                or existing["candidate_id"] != candidate_id
                or existing["expires_at"] != expires_at.astimezone(UTC)
            ):
                raise ValueError("budget reservation key belongs to another scope")
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
        account_id: UUID,
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
                    .where(
                        proactive_budget_reservations.c.account_id == account_id,
                        proactive_budget_reservations.c.reservation_key == reservation_key,
                    )
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
        if cast(datetime, row["expires_at"]) <= now.astimezone(UTC):
            target = ReservationState.EXPIRED
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
        self, *, account_id: UUID, reservation_key: bytes, now: datetime, unknown: bool = False
    ) -> BudgetReservation | None:
        return await self.settle_budget(
            account_id=account_id,
            reservation_key=reservation_key,
            target=ReservationState.SEND_UNKNOWN if unknown else ReservationState.COMMITTED,
            now=now,
        )

    async def release_budget(
        self, *, account_id: UUID, reservation_key: bytes, now: datetime, expired: bool = False
    ) -> BudgetReservation | None:
        return await self.settle_budget(
            account_id=account_id,
            reservation_key=reservation_key,
            target=ReservationState.EXPIRED if expired else ReservationState.RELEASED,
            now=now,
        )

    async def reap_budget(self, *, now: datetime) -> int:
        reservations = tuple(
            (
                await self._session.execute(
                    select(
                        proactive_budget_reservations.c.account_id,
                        proactive_budget_reservations.c.reservation_key,
                    ).where(
                        proactive_budget_reservations.c.state == "held",
                        proactive_budget_reservations.c.expires_at <= now.astimezone(UTC),
                    )
                )
            )
            .mappings()
            .all()
        )
        changed = 0
        for reservation in reservations:
            if (
                await self.release_budget(
                    account_id=reservation["account_id"],
                    reservation_key=reservation["reservation_key"],
                    now=now,
                    expired=True,
                )
                is not None
            ):
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
        if len(candidate.occurrences) != len(
            {occurrence.id for occurrence in candidate.occurrences}
        ) or (candidate.membership_hash != membership_digest(candidate.occurrences)):
            raise ValueError("candidate membership snapshot is invalid")
        decision_id = uuid5(candidate.id, f"proactive-decision:{output_hash.hex()}")
        current_candidate = (
            (
                await self._session.execute(
                    select(proactive_candidates)
                    .where(
                        proactive_candidates.c.id == candidate.id,
                        proactive_candidates.c.account_id == candidate.account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if current_candidate is None or (
            current_candidate["generation"] != candidate.generation
            or current_candidate["membership_hash"] != candidate.membership_hash
        ):
            raise ValueError("candidate snapshot is stale")
        existing = (
            (
                await self._session.execute(
                    select(proactive_decisions)
                    .where(
                        proactive_decisions.c.id == decision_id,
                        proactive_decisions.c.account_id == candidate.account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if not _decision_matches(existing, candidate, decision, output_hash):
                raise ValueError("decision replay does not match durable identity")
            return decision_id
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
            .where(
                proactive_candidates.c.id == candidate.id,
                proactive_candidates.c.account_id == candidate.account_id,
            )
            .values(state=target_state)
        )
        await self._session.execute(
            insert(proactive_state_transitions).values(
                id=uuid4(),
                account_id=candidate.account_id,
                candidate_id=candidate.id,
                from_state=cast(str, current_candidate["state"]),
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
        fencing_token=cast(int, row["fencing_token"]),
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


def _occurrence_matches(row: Any, occurrence: Any) -> bool:
    return all(
        row[column] == value
        for column, value in {
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
        }.items()
    )


def _candidate_matches(row: Any, candidate: Candidate) -> bool:
    return all(
        row[column] == value
        for column, value in {
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
        }.items()
    )


def _decision_matches(
    row: Any, candidate: Candidate, decision: AgentDecision, output_hash: bytes
) -> bool:
    return all(
        row[column] == value
        for column, value in {
            "id": uuid5(candidate.id, f"proactive-decision:{output_hash.hex()}"),
            "account_id": candidate.account_id,
            "candidate_id": candidate.id,
            "generation": candidate.generation,
            "action": decision.action.value,
            "decision_code": decision.decision_code,
            "topic": decision.topic,
            "priority": decision.priority,
            "defer_until": decision.defer_until,
            "output_hash": output_hash,
        }.items()
    )
