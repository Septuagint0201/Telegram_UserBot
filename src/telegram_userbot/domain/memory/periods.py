"""Calendar boundaries persisted with a scheduled summary, independent of event IDs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_userbot.domain.memory.models import SummaryKind
from telegram_userbot.domain.memory.summary import summary_period


@dataclass(frozen=True, slots=True)
class SummaryPeriod:
    kind: SummaryKind
    key: str
    timezone: str
    start: datetime
    end: datetime
    partition: tuple[int, int] | None = None

    @classmethod
    def at(cls, kind: SummaryKind, occurred_at: datetime, timezone: str) -> SummaryPeriod:
        if kind not in {SummaryKind.DAILY, SummaryKind.WEEKLY}:
            raise ValueError("calendar summary kind is invalid")
        key, start, end = summary_period(kind, occurred_at, timezone_name=timezone)
        return cls(kind, key, timezone, start, end)

    @property
    def identity(self) -> str:
        suffix = f":part:{self.partition[0]}:{self.partition[1]}" if self.partition else ""
        return f"{self.timezone}:{self.key}{suffix}"

    def contains(self, value: datetime) -> bool:
        return self.start <= value <= self.end

    def document(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": self.kind.value,
            "key": self.key,
            "timezone": self.timezone,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }
        if self.partition is not None:
            value["partition"] = list(self.partition)
        return value

    @classmethod
    def parse(cls, value: Any) -> SummaryPeriod:
        if (
            not isinstance(value, dict)
            or set(value) - {"partition"} != {"kind", "key", "timezone", "start", "end"}
            or any(not isinstance(item, str) for key, item in value.items() if key != "partition")
        ):
            raise ValueError("calendar summary snapshot is invalid")
        start, end = datetime.fromisoformat(value["start"]), datetime.fromisoformat(value["end"])
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("calendar summary snapshot must be aware")
        expected = cls.at(SummaryKind(value["kind"]), start, value["timezone"])
        if expected.key != value["key"] or expected.start != start or expected.end != end:
            raise ValueError("calendar summary snapshot boundaries changed")
        partition = value.get("partition")
        if partition is None:
            return expected
        if (
            expected.kind is not SummaryKind.DAILY
            or not isinstance(partition, list)
            or len(partition) != 2
            or any(type(item) is not int for item in partition)
            or not 0 <= partition[0] <= 16
            or partition[1] < 0
        ):
            raise ValueError("calendar partition is invalid")
        return cls(
            expected.kind,
            expected.key,
            expected.timezone,
            expected.start,
            expected.end,
            tuple(partition),
        )


def summary_timezone(contact: str | None, account: str | None, deployment: str) -> str:
    value = contact or account or deployment
    # Do not silently fall back when a stored value is invalid.
    try:
        datetime.now(UTC).astimezone(ZoneInfo(value))
    except ZoneInfoNotFoundError:
        raise ValueError("summary timezone is invalid") from None
    return value
