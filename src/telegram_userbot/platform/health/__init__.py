"""Content-free process health state, policy, and snapshot contracts."""

from telegram_userbot.platform.health.model import (
    HEALTH_SNAPSHOT_VERSION,
    HealthKind,
    HealthReason,
    HealthState,
    ServiceName,
)
from telegram_userbot.platform.health.policy import (
    DISK_SAFETY_MAX_USED_PERCENT,
    DISK_SAFETY_MIN_AVAILABLE_BYTES,
    HealthDecision,
    ReadinessPolicy,
    disk_safety_ok,
)
from telegram_userbot.platform.health.snapshot import (
    DEFAULT_HEALTH_SNAPSHOT_PATH,
    DEFAULT_SNAPSHOT_MAX_AGE,
    MAX_HEALTH_SNAPSHOT_BYTES,
    HealthSnapshotError,
    read_health_snapshot,
    write_health_snapshot,
)
from telegram_userbot.platform.health.status import (
    RestoreGateRecord,
    RestoreGateState,
    RestoreVerification,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
    ServiceStatusUpdate,
)

__all__ = [
    "DEFAULT_HEALTH_SNAPSHOT_PATH",
    "DEFAULT_SNAPSHOT_MAX_AGE",
    "DISK_SAFETY_MAX_USED_PERCENT",
    "DISK_SAFETY_MIN_AVAILABLE_BYTES",
    "HEALTH_SNAPSHOT_VERSION",
    "MAX_HEALTH_SNAPSHOT_BYTES",
    "HealthDecision",
    "HealthKind",
    "HealthReason",
    "HealthSnapshotError",
    "HealthState",
    "ReadinessPolicy",
    "RestoreGateRecord",
    "RestoreGateState",
    "RestoreVerification",
    "ServiceHeartbeat",
    "ServiceName",
    "ServiceReadiness",
    "ServiceStatusCode",
    "ServiceStatusMetadata",
    "ServiceStatusUpdate",
    "disk_safety_ok",
    "read_health_snapshot",
    "write_health_snapshot",
]
