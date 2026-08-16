"""UTC and monotonic time value objects."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self


def require_aware(value: datetime, name: str = "timestamp") -> datetime:
    """Reject naive and pathological timezone objects before UTC conversion."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, order=True)
class UtcTimestamp:
    value: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", require_aware(self.value))

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
