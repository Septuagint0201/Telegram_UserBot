"""Idempotent drain and termination-deadline state without signal registration."""

import math
from collections.abc import Callable
from threading import RLock

from telegram_userbot.domain.shared.time import MonotonicDeadline, MonotonicInstant
from telegram_userbot.platform.time.system import SystemClock


class TerminationDeadline:
    """A monotonic shutdown budget suitable for a future SIGTERM adapter."""

    __slots__ = ("_deadline", "_started_at")

    def __init__(self, *, started_at: MonotonicInstant, grace_seconds: float) -> None:
        if not math.isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError("termination grace must be finite and positive")
        self._started_at = started_at
        self._deadline = MonotonicDeadline(MonotonicInstant(started_at.value + grace_seconds))

    @property
    def started_at(self) -> MonotonicInstant:
        return self._started_at

    @property
    def expires_at(self) -> MonotonicInstant:
        return self._deadline.at

    def expired(self, now: MonotonicInstant) -> bool:
        return self._deadline.expired(now)

    def remaining_seconds(self, now: MonotonicInstant) -> float:
        return self._deadline.remaining_seconds(now)


class DrainController:
    """One-way, idempotent drain state; caller owns all signal integration."""

    def __init__(
        self,
        *,
        grace_seconds: float,
        clock: Callable[[], MonotonicInstant] | None = None,
    ) -> None:
        if not math.isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError("drain grace must be finite and positive")
        self._grace_seconds = grace_seconds
        self._clock = clock or SystemClock().monotonic_now
        self._lock = RLock()
        self._deadline: TerminationDeadline | None = None

    def begin(self) -> TerminationDeadline:
        """Begin draining once and preserve the first caller's deadline."""

        with self._lock:
            if self._deadline is None:
                self._deadline = TerminationDeadline(
                    started_at=self._clock(), grace_seconds=self._grace_seconds
                )
            return self._deadline

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._deadline is not None

    @property
    def accepting_new_work(self) -> bool:
        return not self.draining

    @property
    def deadline(self) -> TerminationDeadline | None:
        with self._lock:
            return self._deadline

    def deadline_reached(self) -> bool:
        with self._lock:
            return self._deadline is not None and self._deadline.expired(self._clock())

    def remaining_seconds(self) -> float | None:
        with self._lock:
            if self._deadline is None:
                return None
            return self._deadline.remaining_seconds(self._clock())
