"""IANA timezone, DST, quiet-hour, and bypass calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_userbot.domain.proactive.models import ProactivePolicy, ReasonCode, RuleOccurrence


class TimePolicyError(ValueError):
    """Raised when an unvalidated or impossible timezone value is supplied."""


def load_timezone(name: str) -> ZoneInfo:
    if not name or ("/" not in name and name != "UTC"):
        raise TimePolicyError("IANA timezone is required")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise TimePolicyError("unknown IANA timezone") from exc


def _valid_instants(local_value: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    values: list[datetime] = []
    for fold in (0, 1):
        candidate = local_value.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if roundtrip == local_value and all(
            existing.astimezone(UTC) != candidate.astimezone(UTC) for existing in values
        ):
            values.append(candidate)
    return tuple(values)


def local_to_utc(
    local_value: datetime,
    timezone_name: str,
    *,
    ambiguous: str = "later",
    nonexistent: str = "forward",
) -> datetime:
    """Convert a wall-clock value with explicit safe handling for DST gaps/folds."""

    if local_value.tzinfo is not None:
        raise TimePolicyError("local wall-clock input must be naive")
    if ambiguous not in {"earlier", "later"} or nonexistent not in {"backward", "forward"}:
        raise TimePolicyError("unknown DST resolution policy")
    zone = load_timezone(timezone_name)
    valid = _valid_instants(local_value, zone)
    if valid:
        selected = (
            min(valid, key=lambda value: value.astimezone(UTC))
            if ambiguous == "earlier"
            else max(valid, key=lambda value: value.astimezone(UTC))
        )
        return selected.astimezone(UTC)
    # A quiet boundary is minute precision in V1.  Search in the safe direction
    # until the first representable wall-clock value, including unusual 24-hour gaps.
    step = timedelta(minutes=1) if nonexistent == "forward" else -timedelta(minutes=1)
    probe = local_value
    for _ in range(24 * 60 + 1):
        probe += step
        valid = _valid_instants(probe, zone)
        if valid:
            selected = (
                min(valid, key=lambda value: value.astimezone(UTC))
                if ambiguous == "earlier"
                else max(valid, key=lambda value: value.astimezone(UTC))
            )
            return selected.astimezone(UTC)
    raise TimePolicyError("could not resolve DST transition")


def local_interval_to_utc(
    local_date: date,
    start: time,
    end: time,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(local_date, start)
    end_date = local_date + timedelta(days=1) if end <= start else local_date
    end_local = datetime.combine(end_date, end)
    start_utc = local_to_utc(
        start_local, timezone_name, ambiguous="earlier", nonexistent="backward"
    )
    end_utc = local_to_utc(end_local, timezone_name, ambiguous="later", nonexistent="forward")
    if start_utc >= end_utc:
        raise TimePolicyError("local interval collapsed after DST conversion")
    return start_utc, end_utc


def _contains(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


@dataclass(frozen=True, slots=True)
class QuietDecision:
    in_absolute_quiet: bool
    in_quiet_hours: bool
    bypass_allowed: bool
    code: str

    @property
    def blocked(self) -> bool:
        return self.in_absolute_quiet or (self.in_quiet_hours and not self.bypass_allowed)


def quiet_decision(
    now: datetime,
    *,
    timezone_name: str,
    policy: ProactivePolicy,
    occurrence: RuleOccurrence | None = None,
) -> QuietDecision:
    """Evaluate absolute quiet first; no model may override that result."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise TimePolicyError("now must be timezone-aware")
    zone = load_timezone(timezone_name)
    local = now.astimezone(zone)
    local_date = local.date()
    absolute_start, absolute_end = local_interval_to_utc(
        local_date,
        policy.absolute_no_send_start_local,
        policy.absolute_no_send_end_local,
        timezone_name,
    )
    if _contains(now.astimezone(UTC), absolute_start, absolute_end):
        return QuietDecision(True, True, False, "absolute_no_send")
    current_utc = now.astimezone(UTC)
    quiet_intervals = tuple(
        local_interval_to_utc(
            interval_date,
            policy.quiet_start_local,
            policy.quiet_end_local,
            timezone_name,
        )
        for interval_date in (local_date - timedelta(days=1), local_date)
    )
    if not any(_contains(current_utc, start, end) for start, end in quiet_intervals):
        return QuietDecision(False, False, False, "normal_window")
    bypass = qualifies_quiet_bypass(
        now, timezone_name=timezone_name, policy=policy, occurrence=occurrence
    )
    return QuietDecision(False, True, bypass, "quiet_bypass" if bypass else "quiet_hours")


def qualifies_quiet_bypass(
    now: datetime,
    *,
    timezone_name: str,
    policy: ProactivePolicy,
    occurrence: RuleOccurrence | None,
) -> bool:
    if occurrence is None or occurrence.reason not in {
        ReasonCode.EVENT_UPCOMING,
        ReasonCode.PROMISE_DUE,
    }:
        return False
    if (
        occurrence.importance < policy.bypass_importance_threshold
        or not occurrence.quiet_bypass_possible
    ):
        return False
    if not all(item.valid and item.explicit for item in occurrence.evidence):
        return False
    zone = load_timezone(timezone_name)
    local = now.astimezone(zone)
    if local.hour == 0 or 1 <= local.hour < 7:
        return False
    return (local.hour == 7) or (local.hour >= 22)


def next_quiet_end(now: datetime, *, timezone_name: str, policy: ProactivePolicy) -> datetime:
    """Return the next ordinary quiet end; callers still re-run absolute gate."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise TimePolicyError("now must be timezone-aware")
    local = now.astimezone(load_timezone(timezone_name))
    for offset in range(-1, 3):
        candidate_date = local.date() + timedelta(days=offset)
        _start, end = local_interval_to_utc(
            candidate_date, policy.quiet_start_local, policy.quiet_end_local, timezone_name
        )
        if end > now.astimezone(UTC):
            return end
    raise TimePolicyError("could not find next quiet end")
