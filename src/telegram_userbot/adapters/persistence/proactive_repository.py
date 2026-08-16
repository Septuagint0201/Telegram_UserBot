"""PostgreSQL persistence for the fake-first proactive worker pipeline."""

from __future__ import annotations

from collections.abc import Sequence
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
from telegram_userbot.domain.proactive.time import local_interval_to_utc
from telegram_userbot.domain.shared.time import require_aware


class ProactiveRepository:
    """Durable worker state; callers decide transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue_candidate(self, candidate: Candidate, *, now: datetime) -> UUID:
        current_time = require_aware(now, "now")
        if candidate.policy_version_id is None or not candidate.timezone_name:
            raise ValueError("candidate policy and timezone snapshots are required")
        occurrence_keys = [occurrence.occurrence_key for occurrence in candidate.occurrences]
        if (
            any(
                occurrence.account_id != candidate.account_id
                or occurrence.contact_id != candidate.contact_id
                or occurrence.conversation_id != candidate.conversation_id
                or occurrence.timezone_name != candidate.timezone_name
                or occurrence.policy_version_id != candidate.policy_version_id
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
            persisted_evidence = (
                (
                    await self._session.execute(
                        select(proactive_occurrence_evidence)
                        .where(
                            proactive_occurrence_evidence.c.occurrence_id == occurrence.id,
                            proactive_occurrence_evidence.c.account_id == occurrence.account_id,
                        )
                        .order_by(proactive_occurrence_evidence.c.ordinal)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if not _occurrence_evidence_matches(persisted_evidence, occurrence):
                raise ValueError("occurrence evidence replay does not match durable identity")
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
                timezone_name=candidate.timezone_name,
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
        persisted_memberships = (
            (
                await self._session.execute(
                    select(proactive_candidate_memberships)
                    .where(
                        proactive_candidate_memberships.c.candidate_id == persisted_candidate_id,
                        proactive_candidate_memberships.c.account_id == candidate.account_id,
                    )
                    .order_by(proactive_candidate_memberships.c.ordinal)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not _candidate_memberships_match(persisted_memberships, candidate):
            raise ValueError("candidate membership replay does not match durable identity")
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
        available_time = require_aware(available_at, "available_at")
        created_time = datetime.now(UTC) if now is None else require_aware(now, "now")
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
                created_at=created_time,
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
        current_time = require_aware(now, "now")
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
        current_time = require_aware(now, "now")
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
                else_=current_time + retry_delay,
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
                    proactive_jobs.c.lease_expires_at > current_time,
                )
                .values(
                    state=next_state,
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=current_time if succeeded else None,
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
        current_time = require_aware(now, "now")
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

    async def reserve_budget(  # noqa: PLR0912,PLR0913 - durable authorization spans every budget scope
        self,
        *,
        account_id: UUID,
        contact_id: UUID,
        account_local_date: date,
        contact_local_date: date,
        account_timezone_name: str,
        contact_timezone_name: str,
        limits: BudgetLimits,
        now: datetime,
        expires_at: datetime,
        reservation_key: bytes,
        candidate_id: UUID,
        decision_id: UUID,
        policy_version_id: UUID,
        authorization_generation: int,
        bypass: bool = False,
    ) -> BudgetReservation | None:
        if len(reservation_key) != 32:
            raise ValueError("budget reservation identity is invalid")
        if authorization_generation < 1:
            raise ValueError("authorization generation must be positive")
        current_time = require_aware(now, "now")
        expiry = require_aware(expires_at, "expires_at")
        account_start, account_end = local_interval_to_utc(
            account_local_date, datetime.min.time(), datetime.min.time(), account_timezone_name
        )
        contact_start, contact_end = local_interval_to_utc(
            contact_local_date, datetime.min.time(), datetime.min.time(), contact_timezone_name
        )
        if bypass and limits.bypass_daily == 0:
            return None
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"proactive_budget:{account_id}:{reservation_key.hex()}"},
        )
        binding = (
            (
                await self._session.execute(
                    select(
                        proactive_candidates.c.id.label("candidate_row_id"),
                        proactive_candidates.c.account_id,
                        proactive_candidates.c.contact_id,
                        proactive_candidates.c.conversation_id,
                        proactive_candidates.c.generation,
                        proactive_candidates.c.policy_version_id.label("candidate_policy_id"),
                        proactive_candidates.c.timezone_name.label("candidate_timezone"),
                        proactive_decisions.c.id.label("decision_row_id"),
                        proactive_decisions.c.candidate_id.label("decision_candidate_id"),
                        proactive_decisions.c.generation.label("decision_generation"),
                        proactive_decisions.c.contact_id.label("decision_contact_id"),
                        proactive_decisions.c.conversation_id.label("decision_conversation_id"),
                        proactive_decisions.c.policy_version_id.label("decision_policy_id"),
                        proactive_decisions.c.timezone_name.label("decision_timezone"),
                    )
                    .select_from(
                        proactive_candidates.join(
                            proactive_decisions,
                            (proactive_decisions.c.candidate_id == proactive_candidates.c.id)
                            & (
                                proactive_decisions.c.account_id
                                == proactive_candidates.c.account_id
                            ),
                        )
                    )
                    .where(
                        proactive_candidates.c.id == candidate_id,
                        proactive_candidates.c.account_id == account_id,
                        proactive_decisions.c.id == decision_id,
                        proactive_decisions.c.account_id == account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if binding is None:
            raise ValueError("budget reservation decision/candidate binding is missing")
        if (
            binding["contact_id"] != contact_id
            or binding["decision_candidate_id"] != candidate_id
            or binding["decision_contact_id"] != contact_id
            or binding["decision_conversation_id"] != binding["conversation_id"]
            or binding["candidate_policy_id"] != policy_version_id
            or binding["decision_policy_id"] != policy_version_id
            or binding["candidate_policy_id"] is None
            or binding["decision_generation"] != binding["generation"]
            or binding["decision_timezone"] != binding["candidate_timezone"]
        ):
            raise ValueError("budget reservation scope does not match decision/candidate")

        existing = (
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
        if existing is not None:
            if any(
                (
                    existing["contact_id"] != contact_id,
                    existing["candidate_id"] != candidate_id,
                    existing["decision_id"] != decision_id,
                    existing["policy_version_id"] != policy_version_id,
                    existing["authorization_generation"] != authorization_generation,
                    existing["account_local_date"] != account_local_date,
                    existing["contact_local_date"] != contact_local_date,
                    existing["bypass"] != bypass,
                    existing["expires_at"] != expiry,
                )
            ):
                raise ValueError("budget reservation key belongs to another scope")
            reservation = _reservation(existing)
            return (
                reservation
                if reservation.state
                in {
                    ReservationState.HELD,
                    ReservationState.COMMITTED,
                    ReservationState.SEND_UNKNOWN,
                }
                else None
            )

        scopes = [
            (
                None,
                "account_daily",
                account_local_date,
                account_timezone_name,
                account_start,
                account_end,
                limits.account_daily,
            ),
            (
                contact_id,
                "contact_daily",
                contact_local_date,
                contact_timezone_name,
                contact_start,
                contact_end,
                limits.contact_daily,
            ),
        ]
        if bypass:
            scopes.append(
                (
                    contact_id,
                    "contact_bypass",
                    contact_local_date,
                    contact_timezone_name,
                    contact_start,
                    contact_end,
                    limits.bypass_daily,
                )
            )
        buckets: list[tuple[Any, int]] = []
        for scoped_contact, scope, bucket_date, timezone_name, starts_at, ends_at, limit in scopes:
            await self._session.execute(
                postgresql_insert(proactive_budget_buckets)
                .values(
                    id=uuid4(),
                    account_id=account_id,
                    contact_id=scoped_contact,
                    scope=scope,
                    local_date=bucket_date,
                    timezone_name_snapshot=timezone_name,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    limit_value=limit,
                    held_count=0,
                    committed_count=0,
                    version=1,
                )
                .on_conflict_do_nothing(constraint="uq_proactive_budget_bucket_identity")
            )
            raw_row = (
                (
                    await self._session.execute(
                        select(proactive_budget_buckets)
                        .where(
                            proactive_budget_buckets.c.account_id == account_id,
                            proactive_budget_buckets.c.contact_id.is_(scoped_contact)
                            if scoped_contact is None
                            else proactive_budget_buckets.c.contact_id == scoped_contact,
                            proactive_budget_buckets.c.scope == scope,
                            proactive_budget_buckets.c.local_date == bucket_date,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if raw_row is None:
                raise RuntimeError("budget bucket disappeared during reservation")
            row = dict(raw_row)
            if (
                row["timezone_name_snapshot"] != timezone_name
                or row["starts_at"] != starts_at
                or row["ends_at"] != ends_at
            ):
                raise ValueError("budget bucket snapshot does not match reservation")
            used = row["held_count"] + row["committed_count"]
            effective_limit = min(row["limit_value"], limit)
            if effective_limit < row["limit_value"] and used <= effective_limit:
                result = cast(
                    CursorResult[Any],
                    await self._session.execute(
                        update(proactive_budget_buckets)
                        .where(
                            proactive_budget_buckets.c.id == row["id"],
                            proactive_budget_buckets.c.version == row["version"],
                        )
                        .values(
                            limit_value=effective_limit,
                            version=proactive_budget_buckets.c.version + 1,
                        )
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError("budget bucket limit CAS failed")
                row = dict(row, limit_value=effective_limit, version=row["version"] + 1)
            buckets.append((row, effective_limit))
        if any(
            row["held_count"] + row["committed_count"] >= effective_limit
            for row, effective_limit in buckets
        ):
            return None
        reservation_id = uuid5(account_id, f"proactive-reservation:{reservation_key.hex()}")
        await self._session.execute(
            insert(proactive_budget_reservations).values(
                id=reservation_id,
                account_id=account_id,
                contact_id=contact_id,
                conversation_id=binding["conversation_id"],
                candidate_id=candidate_id,
                decision_id=decision_id,
                policy_version_id=policy_version_id,
                authorization_generation=authorization_generation,
                account_bucket_id=buckets[0][0]["id"],
                contact_bucket_id=buckets[1][0]["id"],
                bypass_bucket_id=buckets[2][0]["id"] if bypass else None,
                reservation_key=reservation_key,
                account_local_date=account_local_date,
                contact_local_date=contact_local_date,
                local_date=contact_local_date,
                bypass=bypass,
                state="held",
                expires_at=expiry,
                held_at=current_time,
            )
        )
        for row, _effective_limit in buckets:
            result = cast(
                CursorResult[Any],
                await self._session.execute(
                    update(proactive_budget_buckets)
                    .where(
                        proactive_budget_buckets.c.id == row["id"],
                        proactive_budget_buckets.c.version == row["version"],
                    )
                    .values(
                        held_count=proactive_budget_buckets.c.held_count + 1,
                        version=proactive_budget_buckets.c.version + 1,
                    )
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("budget bucket hold CAS failed")
        return BudgetReservation(
            reservation_id,
            reservation_key,
            account_id,
            contact_id,
            contact_local_date,
            bypass,
            ReservationState.HELD,
            expiry,
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
        current_time = require_aware(now, "now")
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
        if (
            cast(datetime, row["expires_at"]) <= current_time
            and target is ReservationState.RELEASED
        ):
            target = ReservationState.EXPIRED
        delta_committed = target in {ReservationState.COMMITTED, ReservationState.SEND_UNKNOWN}
        reservation_update = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_budget_reservations)
                .where(
                    proactive_budget_reservations.c.id == row["id"],
                    proactive_budget_reservations.c.state == "held",
                )
                .values(
                    state=target.value,
                    committed_at=current_time if delta_committed else None,
                    terminal_at=current_time,
                )
            ),
        )
        if reservation_update.rowcount != 1:
            raise RuntimeError("budget reservation settlement CAS failed")
        bucket_ids = [row["account_bucket_id"], row["contact_bucket_id"]]
        if row["bypass"]:
            if row["bypass_bucket_id"] is None:
                raise RuntimeError("bypass reservation has no bypass bucket")
            bucket_ids.append(row["bypass_bucket_id"])
        for bucket_id in bucket_ids:
            bucket_update = cast(
                CursorResult[Any],
                await self._session.execute(
                    update(proactive_budget_buckets)
                    .where(
                        proactive_budget_buckets.c.id == bucket_id,
                        proactive_budget_buckets.c.account_id == row["account_id"],
                        proactive_budget_buckets.c.held_count > 0,
                    )
                    .values(
                        held_count=proactive_budget_buckets.c.held_count - 1,
                        committed_count=proactive_budget_buckets.c.committed_count
                        + (1 if delta_committed else 0),
                        version=proactive_budget_buckets.c.version + 1,
                    )
                ),
            )
            if bucket_update.rowcount != 1:
                raise RuntimeError("budget bucket settlement CAS failed")
        return BudgetReservation(
            cast(UUID, row["id"]),
            reservation_key,
            cast(UUID, row["account_id"]),
            cast(UUID, row["contact_id"]),
            cast(date, row["contact_local_date"]),
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
        current_time = require_aware(now, "now")
        reservations = tuple(
            (
                await self._session.execute(
                    select(
                        proactive_budget_reservations.c.account_id,
                        proactive_budget_reservations.c.reservation_key,
                    )
                    .join(
                        proactive_candidates,
                        (proactive_candidates.c.id == proactive_budget_reservations.c.candidate_id)
                        & (
                            proactive_candidates.c.account_id
                            == proactive_budget_reservations.c.account_id
                        ),
                    )
                    .where(
                        proactive_budget_reservations.c.state == "held",
                        proactive_budget_reservations.c.expires_at <= current_time,
                        proactive_candidates.c.state.in_(
                            ("evaluated_none", "failed_model", "superseded", "expired")
                        ),
                    )
                    .with_for_update(skip_locked=True)
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
                    now=current_time,
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
        if candidate.policy_version_id is None or not candidate.timezone_name:
            raise ValueError("candidate policy and timezone snapshots are required")
        current_time = require_aware(now, "now")
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
        if current_candidate is None or not _candidate_matches(current_candidate, candidate):
            raise ValueError("candidate snapshot is stale")
        existing = (
            (
                await self._session.execute(
                    select(proactive_decisions)
                    .where(
                        proactive_decisions.c.candidate_id == candidate.id,
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
            persisted_memberships = (
                (
                    await self._session.execute(
                        select(proactive_decision_memberships)
                        .where(
                            proactive_decision_memberships.c.decision_id == existing["id"],
                            proactive_decision_memberships.c.account_id == candidate.account_id,
                        )
                        .order_by(proactive_decision_memberships.c.ordinal)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if not _decision_memberships_match(
                persisted_memberships,
                decision,
                decision_id=cast(UUID, existing["id"]),
                account_id=candidate.account_id,
            ):
                raise ValueError("decision membership replay does not match durable identity")
            return cast(UUID, existing["id"])
        if current_candidate["state"] not in {"open", "evaluating"}:
            raise ValueError("candidate is already terminal")
        await self._session.execute(
            postgresql_insert(proactive_decisions)
            .values(
                id=decision_id,
                account_id=candidate.account_id,
                candidate_id=candidate.id,
                generation=candidate.generation,
                action=decision.action.value,
                contact_id=candidate.contact_id,
                conversation_id=candidate.conversation_id,
                decision_code=decision.decision_code,
                policy_version_id=candidate.policy_version_id,
                timezone_name=candidate.timezone_name,
                topic=decision.topic,
                priority=decision.priority,
                defer_until=decision.defer_until,
                output_hash=output_hash,
                state="accepted",
                created_at=current_time,
            )
            .on_conflict_do_nothing(constraint="uq_proactive_decisions_candidate")
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
        persisted_memberships = (
            (
                await self._session.execute(
                    select(proactive_decision_memberships)
                    .where(
                        proactive_decision_memberships.c.decision_id == decision_id,
                        proactive_decision_memberships.c.account_id == candidate.account_id,
                    )
                    .order_by(proactive_decision_memberships.c.ordinal)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not _decision_memberships_match(
            persisted_memberships,
            decision,
            decision_id=decision_id,
            account_id=candidate.account_id,
        ):
            raise ValueError("decision membership replay does not match durable identity")
        target_state = {
            ProactiveAction.SEND_NOW: "send_selected",
            ProactiveAction.DEFER_ONCE: "deferred_once",
            ProactiveAction.NONE: "evaluated_none",
        }[decision.action]
        candidate_update = cast(
            CursorResult[Any],
            await self._session.execute(
                update(proactive_candidates)
                .where(
                    proactive_candidates.c.id == candidate.id,
                    proactive_candidates.c.account_id == candidate.account_id,
                    proactive_candidates.c.state.in_(("open", "evaluating")),
                )
                .values(state=target_state)
            ),
        )
        if candidate_update.rowcount != 1:
            raise RuntimeError("candidate state transition CAS failed")
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
                created_at=current_time,
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
        cast(date, row.get("contact_local_date", row.get("local_date"))),
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
            "timezone_name": candidate.timezone_name,
            "mode_version": candidate.mode_version,
            "content_revision": candidate.content_revision,
            "activity_revision": candidate.activity_revision,
        }.items()
    )


def _occurrence_evidence_matches(rows: Sequence[Any], occurrence: Any) -> bool:
    if len(rows) != len(occurrence.evidence):
        return False
    return all(
        all(
            row[column] == value
            for column, value in {
                "occurrence_id": occurrence.id,
                "account_id": occurrence.account_id,
                "ordinal": ordinal,
                "source_type": evidence.source_type,
                "source_id": evidence.source_id,
                "source_version": evidence.source_version,
                "source_hash": evidence.source_hash,
                "summary": evidence.summary,
                "current": evidence.current,
                "explicit": evidence.explicit,
            }.items()
        )
        for ordinal, (row, evidence) in enumerate(zip(rows, occurrence.evidence, strict=True), 1)
    )


def _candidate_memberships_match(rows: Sequence[Any], candidate: Candidate) -> bool:
    if len(rows) != len(candidate.occurrences):
        return False
    return all(
        all(
            row[column] == value
            for column, value in {
                "candidate_id": candidate.id,
                "account_id": candidate.account_id,
                "ordinal": ordinal,
                "occurrence_id": occurrence.id,
                "occurrence_generation": occurrence.generation,
                "occurrence_key": occurrence.occurrence_key,
            }.items()
        )
        for ordinal, (row, occurrence) in enumerate(
            zip(rows, candidate.occurrences, strict=True), 1
        )
    )


def _decision_memberships_match(
    rows: Sequence[Any],
    decision: AgentDecision,
    *,
    decision_id: UUID,
    account_id: UUID,
) -> bool:
    if len(rows) != len(decision.selected_occurrence_ids):
        return False
    return all(
        row["decision_id"] == decision_id
        and row["account_id"] == account_id
        and row["ordinal"] == ordinal
        and row["occurrence_id"] == occurrence_id
        for ordinal, (row, occurrence_id) in enumerate(
            zip(rows, decision.selected_occurrence_ids, strict=True), 1
        )
    )


def _decision_matches(
    row: Any, candidate: Candidate, decision: AgentDecision, output_hash: bytes
) -> bool:
    return all(
        row[column] == value
        for column, value in {
            "id": uuid5(candidate.id, f"proactive-decision:{output_hash.hex()}"),
            "account_id": candidate.account_id,
            "contact_id": candidate.contact_id,
            "conversation_id": candidate.conversation_id,
            "candidate_id": candidate.id,
            "generation": candidate.generation,
            "policy_version_id": candidate.policy_version_id,
            "timezone_name": candidate.timezone_name,
            "action": decision.action.value,
            "decision_code": decision.decision_code,
            "topic": decision.topic,
            "priority": decision.priority,
            "defer_until": decision.defer_until,
            "output_hash": output_hash,
        }.items()
    )
