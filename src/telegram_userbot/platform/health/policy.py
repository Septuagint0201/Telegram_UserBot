"""Strict liveness and readiness evaluation without dependency I/O."""

from dataclasses import dataclass
from datetime import timedelta

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health.model import (
    HealthKind,
    HealthReason,
    HealthState,
    ServiceName,
)

DISK_SAFETY_MAX_USED_PERCENT = 95
DISK_SAFETY_MIN_AVAILABLE_BYTES = 1 << 30


def disk_safety_ok(*, total_bytes: int, available_bytes: int) -> bool:
    """Return false at 95% used or below one GiB available."""

    if (
        type(total_bytes) is not int
        or type(available_bytes) is not int
        or total_bytes <= 0
        or available_bytes < 0
        or available_bytes > total_bytes
    ):
        raise ValueError("disk capacity values are invalid")
    below_used_limit = (total_bytes - available_bytes) * 100 < (
        total_bytes * DISK_SAFETY_MAX_USED_PERCENT
    )
    return below_used_limit and available_bytes >= DISK_SAFETY_MIN_AVAILABLE_BYTES


@dataclass(frozen=True, slots=True)
class HealthDecision:
    kind: HealthKind
    healthy: bool
    reason: HealthReason


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    """Evaluate process liveness separately from strict serving readiness."""

    heartbeat_max_age: timedelta = timedelta(seconds=30)
    future_clock_skew: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.heartbeat_max_age <= timedelta(0) or self.future_clock_skew < timedelta(0):
            raise ValueError("health freshness windows are invalid")

    def liveness(self, state: HealthState, *, now: UtcTimestamp) -> HealthDecision:
        if not state.process_loop_ok:
            return HealthDecision(HealthKind.LIVE, False, HealthReason.PROCESS_LOOP_FAILED)
        if state.heartbeat_at.value > now.value + self.future_clock_skew:
            return HealthDecision(HealthKind.LIVE, False, HealthReason.HEARTBEAT_INVALID)
        if now.value - state.heartbeat_at.value > self.heartbeat_max_age:
            return HealthDecision(HealthKind.LIVE, False, HealthReason.HEARTBEAT_STALE)
        return HealthDecision(HealthKind.LIVE, True, HealthReason.LIVE)

    def readiness(self, state: HealthState, *, now: UtcTimestamp) -> HealthDecision:
        live = self.liveness(state, now=now)
        if not live.healthy:
            return HealthDecision(HealthKind.READY, False, live.reason)

        common_checks = (
            (not state.maintenance, HealthReason.MAINTENANCE_ACTIVE),
            (not state.draining, HealthReason.DRAINING),
            (state.required_config_ok, HealthReason.REQUIRED_CONFIG_NOT_READY),
            (state.disk_safety_ok, HealthReason.DISK_SAFETY_NOT_READY),
            (state.database_ok, HealthReason.DATABASE_UNAVAILABLE),
            (state.redis_ok, HealthReason.REDIS_UNAVAILABLE),
            (state.schema_ok, HealthReason.SCHEMA_NOT_READY),
            (state.restore_gate_open, HealthReason.RESTORE_GATE_CLOSED),
        )
        for passed, reason in common_checks:
            if not passed:
                return HealthDecision(HealthKind.READY, False, reason)

        service_checks: tuple[tuple[bool | None, HealthReason], ...]
        if state.service is ServiceName.APP:
            service_checks = (
                (state.account_ready, HealthReason.ACCOUNT_NOT_READY),
                (state.session_owned, HealthReason.SESSION_NOT_OWNED),
                (state.telegram_ready, HealthReason.TELEGRAM_NOT_READY),
            )
        elif state.service is ServiceName.CONTROL:
            service_checks = (
                (state.control_bot_ready, HealthReason.CONTROL_BOT_NOT_READY),
                (state.web_api_ready, HealthReason.WEB_API_NOT_READY),
            )
        else:
            service_checks = ((state.consumer_ready, HealthReason.CONSUMER_NOT_READY),)
        for service_passed, reason in service_checks:
            if service_passed is not True:
                return HealthDecision(HealthKind.READY, False, reason)
        return HealthDecision(HealthKind.READY, True, HealthReason.READY)
