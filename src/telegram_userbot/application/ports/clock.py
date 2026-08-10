"""Wall-clock and monotonic-clock ports."""

from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp


@runtime_checkable
class Clock(Protocol):
    def now(self) -> UtcTimestamp: ...


@runtime_checkable
class MonotonicClock(Protocol):
    def monotonic_now(self) -> MonotonicInstant: ...
