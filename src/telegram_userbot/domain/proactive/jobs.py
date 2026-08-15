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
    SUCCEEDED = "succeeded"
    EXPIRED = "expired"


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

    def __post_init__(self) -> None:
        if len(self.idempotency_key) != 32 or self.available_at.tzinfo is None:
            raise ValueError("due job identity/time is invalid")
        if self.attempt_count < 0:
            raise ValueError("attempt count cannot be negative")


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
        self, *, now: datetime, owner: UUID, lease: timedelta = timedelta(minutes=2)
    ) -> DueJob | None:
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        with self._lock:
            candidates = [
                item
                for item in self._jobs.values()
                if item.state is DueJobState.PENDING and item.available_at <= now.astimezone(UTC)
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
            )
            self._jobs[current.idempotency_key] = claimed
            return claimed

    def complete(self, *, idempotency_key: bytes, owner: UUID, now: datetime) -> bool:
        with self._lock:
            current = self._jobs.get(idempotency_key)
            if (
                current is None
                or current.state is not DueJobState.LEASED
                or current.lease_owner != owner
            ):
                return False
            self._jobs[idempotency_key] = replace(
                current, state=DueJobState.SUCCEEDED, lease_owner=None, lease_expires_at=None
            )
            return True

    def compensate(self, *, now: datetime) -> tuple[DueJob, ...]:
        """Requeue expired leases and return jobs still due; no model is called here."""
        with self._lock:
            recovered: list[DueJob] = []
            for key, current in tuple(self._jobs.items()):
                if (
                    current.state is DueJobState.LEASED
                    and current.lease_expires_at is not None
                    and current.lease_expires_at <= now.astimezone(UTC)
                ):
                    recovered.append(
                        replace(
                            current,
                            state=DueJobState.PENDING,
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                    )
                    self._jobs[key] = recovered[-1]
            return tuple(
                sorted(
                    (
                        item
                        for item in self._jobs.values()
                        if item.state is DueJobState.PENDING
                        and item.available_at <= now.astimezone(UTC)
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
