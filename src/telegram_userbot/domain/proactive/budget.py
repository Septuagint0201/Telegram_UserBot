"""Atomic-in-process budget reservation model used by fake-first workers.

The PostgreSQL repository mirrors these transitions with row locks/CAS.  Keeping
the state machine here makes concurrency and unknown-send behavior testable
without opening a database or Telegram connection.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from threading import Lock
from uuid import UUID, uuid4

from telegram_userbot.domain.proactive.models import (
    BudgetLimits,
    BudgetReservation,
    ReservationState,
)


@dataclass(slots=True)
class _Bucket:
    limit: int
    held: int = 0
    committed: int = 0

    @property
    def available(self) -> int:
        return self.limit - self.held - self.committed


class BudgetLedger:
    """CAS-like reservation ledger with account/contact/bypass lock ordering."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._buckets: dict[tuple[UUID, UUID | None, str, date], _Bucket] = {}
        self._reservations: dict[bytes, BudgetReservation] = {}

    def reserve(  # noqa: PLR0913 - reservation identity spans all budget scopes
        self,
        *,
        account_id: UUID,
        contact_id: UUID,
        local_date: date,
        limits: BudgetLimits,
        expires_at: datetime,
        reservation_key: bytes,
        bypass: bool = False,
    ) -> BudgetReservation | None:
        if expires_at.tzinfo is None or len(reservation_key) != 32:
            raise ValueError("reservation expiry and key are required")
        if limits.bypass_daily == 0 and bypass:
            return None
        with self._lock:
            existing = self._reservations.get(reservation_key)
            if existing is not None:
                return (
                    existing
                    if existing.state
                    in {
                        ReservationState.HELD,
                        ReservationState.COMMITTED,
                        ReservationState.SEND_UNKNOWN,
                    }
                    else None
                )
            account = self._bucket(
                account_id, None, "account_daily", local_date, limits.account_daily
            )
            contact = self._bucket(
                account_id, contact_id, "contact_daily", local_date, limits.contact_daily
            )
            bypass_bucket = (
                self._bucket(
                    account_id, contact_id, "contact_bypass", local_date, limits.bypass_daily
                )
                if bypass
                else None
            )
            if (
                account.available < 1
                or contact.available < 1
                or (bypass_bucket is not None and bypass_bucket.available < 1)
            ):
                return None
            account.held += 1
            contact.held += 1
            if bypass_bucket is not None:
                bypass_bucket.held += 1
            reservation = BudgetReservation(
                id=uuid4(),
                reservation_key=reservation_key,
                account_id=account_id,
                contact_id=contact_id,
                local_date=local_date,
                bypass=bypass,
                state=ReservationState.HELD,
                expires_at=expires_at,
            )
            self._reservations[reservation_key] = reservation
            return reservation

    def commit(self, reservation_key: bytes, *, unknown: bool = False) -> BudgetReservation:
        with self._lock:
            reservation = self._require(reservation_key)
            if (
                reservation.state is ReservationState.COMMITTED
                or reservation.state is ReservationState.SEND_UNKNOWN
            ):
                return reservation
            if reservation.state is not ReservationState.HELD:
                raise ValueError("only held reservations can be committed")
            self._move_counts(reservation, held_delta=-1, committed_delta=1)
            target = ReservationState.SEND_UNKNOWN if unknown else ReservationState.COMMITTED
            updated = replace(reservation, state=target)
            self._reservations[reservation_key] = updated
            return updated

    def release(self, reservation_key: bytes, *, expired: bool = False) -> BudgetReservation:
        with self._lock:
            reservation = self._require(reservation_key)
            if reservation.state in {ReservationState.RELEASED, ReservationState.EXPIRED}:
                return reservation
            if reservation.state is not ReservationState.HELD:
                raise ValueError("a committed/unknown reservation cannot be released")
            self._move_counts(reservation, held_delta=-1, committed_delta=0)
            target = ReservationState.EXPIRED if expired else ReservationState.RELEASED
            updated = replace(reservation, state=target)
            self._reservations[reservation_key] = updated
            return updated

    def reap(self, *, now: datetime) -> tuple[BudgetReservation, ...]:
        if now.tzinfo is None:
            raise ValueError("reaper time must be aware")
        with self._lock:
            expired: list[BudgetReservation] = []
            for key, reservation in tuple(self._reservations.items()):
                if reservation.state is ReservationState.HELD and reservation.expires_at <= now:
                    self._move_counts(reservation, held_delta=-1, committed_delta=0)
                    updated = replace(reservation, state=ReservationState.EXPIRED)
                    self._reservations[key] = updated
                    expired.append(updated)
            return tuple(expired)

    def snapshot(
        self, *, account_id: UUID, contact_id: UUID, local_date: date
    ) -> dict[str, tuple[int, int, int]]:
        with self._lock:
            result: dict[str, tuple[int, int, int]] = {}
            for scope, key in (
                ("account_daily", (account_id, None, "account_daily", local_date)),
                ("contact_daily", (account_id, contact_id, "contact_daily", local_date)),
                ("contact_bypass", (account_id, contact_id, "contact_bypass", local_date)),
            ):
                bucket = self._buckets.get(key)
                if bucket is not None:
                    result[scope] = (bucket.limit, bucket.held, bucket.committed)
            return result

    def _bucket(
        self, account_id: UUID, contact_id: UUID | None, scope: str, local_date: date, limit: int
    ) -> _Bucket:
        key = (account_id, contact_id, scope, local_date)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(limit)
            self._buckets[key] = bucket
        elif bucket.limit != limit:
            bucket.limit = min(bucket.limit, limit)
        return bucket

    def _move_counts(
        self, reservation: BudgetReservation, *, held_delta: int, committed_delta: int
    ) -> None:
        keys = [
            (reservation.account_id, None, "account_daily", reservation.local_date),
            (
                reservation.account_id,
                reservation.contact_id,
                "contact_daily",
                reservation.local_date,
            ),
        ]
        if reservation.bypass:
            keys.append(
                (
                    reservation.account_id,
                    reservation.contact_id,
                    "contact_bypass",
                    reservation.local_date,
                )
            )
        for key in keys:
            bucket = self._buckets[key]
            bucket.held += held_delta
            bucket.committed += committed_delta
            if (
                min(bucket.held, bucket.committed) < 0
                or bucket.held + bucket.committed > bucket.limit
            ):
                raise RuntimeError("budget bucket invariant violated")

    def _require(self, reservation_key: bytes) -> BudgetReservation:
        try:
            return self._reservations[reservation_key]
        except KeyError as exc:
            raise KeyError("unknown budget reservation") from exc
