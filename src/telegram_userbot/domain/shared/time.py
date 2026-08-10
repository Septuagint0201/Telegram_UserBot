"""UTC and monotonic time value objects."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self


@dataclass(frozen=True, slots=True, order=True)
class UtcTimestamp:
    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "value", self.value.astimezone(UTC))

    @classmethod
    def from_iso(cls, raw: str) -> Self:
        return cls(datetime.fromisoformat(raw))

    def to_iso(self) -> str:
        return self.value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def add(self, delta: timedelta) -> Self:
        return type(self)(self.value + delta)


@dataclass(frozen=True, slots=True, order=True)
class MonotonicInstant:
    value: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or self.value < 0:
            raise ValueError("monotonic time must be finite and non-negative")


@dataclass(frozen=True, slots=True, order=True)
class MonotonicDeadline:
    at: MonotonicInstant

    def expired(self, now: MonotonicInstant) -> bool:
        return now >= self.at

    def remaining_seconds(self, now: MonotonicInstant) -> float:
        return max(0.0, self.at.value - now.value)
