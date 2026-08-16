from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from telegram_userbot.domain.shared.time import MonotonicDeadline, MonotonicInstant, UtcTimestamp


@pytest.mark.property
@given(st.datetimes(timezones=st.timezones()))
def test_utc_timestamp_round_trip(value: datetime) -> None:
    timestamp = UtcTimestamp(value)
    rebuilt = UtcTimestamp.from_iso(timestamp.to_iso())
    assert rebuilt == timestamp
    assert timestamp.value.tzinfo is UTC


@pytest.mark.unit
def test_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UtcTimestamp(datetime(2030, 1, 1))  # noqa: DTZ001 - deliberately naive input


class _NoOffset(tzinfo):
    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        return None

    def dst(self, _value: datetime | None) -> timedelta | None:
        return None

    def tzname(self, _value: datetime | None) -> str | None:
        return "no-offset"


@pytest.mark.unit
def test_timestamp_rejects_timezone_without_offset() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UtcTimestamp(datetime(2030, 1, 1, tzinfo=_NoOffset()))


@pytest.mark.unit
def test_dst_fold_round_trips_through_utc() -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2030, 11, 3, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2030, 11, 3, 1, 30, tzinfo=zone, fold=1)
    assert UtcTimestamp(first) != UtcTimestamp(second)
    assert UtcTimestamp(first).value.astimezone(zone).fold == 0
    assert UtcTimestamp(second).value.astimezone(zone).fold == 1


@pytest.mark.unit
def test_monotonic_deadline_boundaries() -> None:
    deadline = MonotonicDeadline(MonotonicInstant(10.0))
    assert not deadline.expired(MonotonicInstant(9.999))
    assert deadline.expired(MonotonicInstant(10.0))
    assert deadline.remaining_seconds(MonotonicInstant(20.0)) == 0.0


@pytest.mark.unit
@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_monotonic_instant_rejects_invalid_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        MonotonicInstant(value)
