"""Durable due-job semantics mirrored by the PostgreSQL background-job table."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from uuid import UUID, uuid4


class DueJobState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    EXPIRED = "expired"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class DueJob:
    id: UUID
    account_id: UUID
    idempotency_key: bytes
    available_at: datetime
    state: DueJobState = DueJobState.PENDING
    lease_owner: UUID | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    fencing_token: int = 0

    def __post_init__(self) -> None:
        if len(self.idempotency_key) != 32 or self.available_at.tzinfo is None:
            raise ValueError("due job identity/time is invalid")
        if self.attempt_count < 0 or self.fencing_token < 0:
            raise ValueError("attempt count and fencing token cannot be negative")


class DurableDueJobStore:
    """Small deterministic fake for due/compensation recovery tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[bytes, DueJob] = {}

    def enqueue(
        self, *, account_id: UUID, idempotency_key: bytes, available_at: datetime
    ) -> DueJob:
        if len(idempotency_key) != 32:
            raise ValueError("due job key must be 32 bytes")
        with self._lock:
            current = self._jobs.get(idempotency_key)
            if current is not None:
                return current
            job = DueJob(uuid4(), account_id, idempotency_key, available_at.astimezone(UTC))
            self._jobs[idempotency_key] = job
            return job

    def claim(
        self,
        *,
        now: datetime,
        owner: UUID,
        lease: timedelta = timedelta(minutes=2),
        max_attempts: int = 5,
    ) -> DueJob | None:
        if lease <= timedelta(0) or max_attempts <= 0:
            raise ValueError("lease policy is invalid")
        with self._lock:
            for key, current in tuple(self._jobs.items()):
                if (
                    current.state in {DueJobState.PENDING, DueJobState.RETRY_WAIT}
                    and current.attempt_count >= max_attempts
                ):
                    self._jobs[key] = replace(current, state=DueJobState.DEAD_LETTER)
            candidates = [
                item
                for item in self._jobs.values()
                if item.state in {DueJobState.PENDING, DueJobState.RETRY_WAIT}
                and item.available_at <= now.astimezone(UTC)
                and item.attempt_count < max_attempts
            ]
            if not candidates:
                return None
            current = min(candidates, key=lambda item: (item.available_at, str(item.id)))
            claimed = replace(
                current,
                state=DueJobState.LEASED,
                lease_owner=owner,
                lease_expires_at=now.astimezone(UTC) + lease,
                attempt_count=current.attempt_count + 1,
                fencing_token=current.fencing_token + 1,
            )
            self._jobs[current.idempotency_key] = claimed
            return claimed

    def complete(  # noqa: PLR0913 - fake mirrors the durable lease contract
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
            raise ValueError("completion policy is invalid")
        with self._lock:
            current = self._jobs.get(idempotency_key)
            if (
                current is None
                or current.state is not DueJobState.LEASED
                or current.lease_owner != owner
                or current.fencing_token != fencing_token
                or current.lease_expires_at is None
                or current.lease_expires_at <= now.astimezone(UTC)
            ):
                return False
            if succeeded:
                state = DueJobState.SUCCEEDED
                available_at = current.available_at
            elif current.attempt_count >= max_attempts:
                state = DueJobState.DEAD_LETTER
                available_at = current.available_at
            else:
                state = DueJobState.RETRY_WAIT
                available_at = now.astimezone(UTC) + retry_delay
            self._jobs[idempotency_key] = replace(
                current,
                state=state,
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
            )
            return True

    def compensate(
        self,
        *,
        now: datetime,
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> tuple[DueJob, ...]:
        """Recover expired leases with bounded retries; no model is called here."""
        if max_attempts <= 0 or retry_delay <= timedelta(0):
            raise ValueError("compensation policy is invalid")
        current_time = now.astimezone(UTC)
        with self._lock:
            for key, current in tuple(self._jobs.items()):
                if (
                    current.state is DueJobState.LEASED
                    and current.lease_expires_at is not None
                    and current.lease_expires_at <= current_time
                ):
                    terminal = current.attempt_count >= max_attempts
                    self._jobs[key] = replace(
                        current,
                        state=DueJobState.DEAD_LETTER if terminal else DueJobState.RETRY_WAIT,
                        available_at=current.available_at
                        if terminal
                        else current_time + retry_delay,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
            return tuple(
                sorted(
                    (
                        item
                        for item in self._jobs.values()
                        if item.state in {DueJobState.PENDING, DueJobState.RETRY_WAIT}
                        and item.available_at <= current_time
                        and item.attempt_count < max_attempts
                    ),
                    key=lambda item: (item.available_at, str(item.id)),
                )
            )

    def expire(self, *, now: datetime, window_end: datetime) -> int:
        with self._lock:
            changed = 0
            for key, current in tuple(self._jobs.items()):
                if current.state is DueJobState.PENDING and window_end <= now.astimezone(UTC):
                    self._jobs[key] = replace(current, state=DueJobState.EXPIRED)
                    changed += 1
            return changed
