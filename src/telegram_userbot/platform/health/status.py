"""Typed, content-free service status and restore-gate contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from telegram_userbot.domain.shared.time import require_aware
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION, RESOURCE_PROFILE
from telegram_userbot.platform.health.model import ServiceName

_DEPLOYMENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_HEX_PREFIX = re.compile(r"[0-9a-f]{12}\Z")
_BANDS = frozenset({"unknown", "normal", "warning", "critical"})
_METADATA_FIELD_ORDER = (
    "deployment_id",
    "source_commit_prefix",
    "image_digest_prefix",
    "resource_profile",
    "restart_count",
    "queue_lag_band",
    "disk_band",
    "backup_age_band",
)
_METADATA_KEYS = frozenset(_METADATA_FIELD_ORDER)


class ServiceReadiness(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"
    DRAINING = "draining"
    STOPPED = "stopped"


class ServiceStatusCode(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    PROCESS_LOOP_FAILED = "PROCESS_LOOP_FAILED"
    HEARTBEAT_STALE = "HEARTBEAT_STALE"
    HEARTBEAT_INVALID = "HEARTBEAT_INVALID"
    MAINTENANCE_ACTIVE = "MAINTENANCE_ACTIVE"
    DRAINING = "DRAINING"
    REQUIRED_CONFIG_NOT_READY = "REQUIRED_CONFIG_NOT_READY"
    DISK_SAFETY_NOT_READY = "DISK_SAFETY_NOT_READY"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    SCHEMA_NOT_READY = "SCHEMA_NOT_READY"
    RESTORE_GATE_CLOSED = "RESTORE_GATE_CLOSED"
    ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
    SESSION_NOT_OWNED = "SESSION_NOT_OWNED"
    TELEGRAM_NOT_READY = "TELEGRAM_NOT_READY"
    CONTROL_BOT_NOT_READY = "CONTROL_BOT_NOT_READY"
    WEB_API_NOT_READY = "WEB_API_NOT_READY"
    CONSUMER_NOT_READY = "CONSUMER_NOT_READY"


class RestoreGateState(StrEnum):
    CLOSED = "closed"
    VALIDATING = "validating"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class ServiceStatusMetadata:
    """A bounded metadata allowlist suitable for PostgreSQL, Redis, and logs."""

    deployment_id: str
    source_commit_prefix: str | None = None
    image_digest_prefix: str | None = None
    resource_profile: str | None = None
    restart_count: int | None = None
    queue_lag_band: str | None = None
    disk_band: str | None = None
    backup_age_band: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.deployment_id, str)
            or _DEPLOYMENT_ID.fullmatch(self.deployment_id) is None
        ):
            raise ValueError("service metadata deployment id is invalid")
        for value in (self.source_commit_prefix, self.image_digest_prefix):
            if value is not None and (
                not isinstance(value, str) or _HEX_PREFIX.fullmatch(value) is None
            ):
                raise ValueError("service metadata digest prefix is invalid")
        if self.resource_profile is not None and (
            not isinstance(self.resource_profile, str) or self.resource_profile != RESOURCE_PROFILE
        ):
            raise ValueError("service metadata resource profile is invalid")
        if self.restart_count is not None and (
            type(self.restart_count) is not int or not 0 <= self.restart_count <= 2**31 - 1
        ):
            raise ValueError("service metadata restart count is invalid")
        for value in (self.queue_lag_band, self.disk_band, self.backup_age_band):
            if value is not None and (not isinstance(value, str) or value not in _BANDS):
                raise ValueError("service metadata band is invalid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ServiceStatusMetadata:
        if not isinstance(value, Mapping) or not set(value) <= _METADATA_KEYS:
            raise ValueError("service metadata contains an unknown field")
        try:
            return cls(**value)  # type: ignore[arg-type]
        except TypeError:
            raise ValueError("service metadata has invalid fields") from None

    def as_mapping(self) -> Mapping[str, object]:
        values: dict[str, object] = {"deployment_id": self.deployment_id}
        # Keep a stable field order for JSONB snapshots and any future
        # canonical serialization/evidence hash consumers.
        for name in _METADATA_FIELD_ORDER[1:]:
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        return MappingProxyType(values)


@dataclass(frozen=True, slots=True)
class ServiceHeartbeat:
    instance_id: UUID
    service_name: ServiceName
    started_at: datetime
    heartbeat_at: datetime
    readiness: ServiceReadiness
    status_code: ServiceStatusCode
    metadata: ServiceStatusMetadata
    schema_revision: str = EXPECTED_SCHEMA_REVISION
    last_successful_operation_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, UUID) or self.instance_id.int == 0:
            raise ValueError("service instance id cannot be nil")
        if not isinstance(self.service_name, ServiceName):
            raise TypeError("service name must use the stable vocabulary")
        if not isinstance(self.readiness, ServiceReadiness) or not isinstance(
            self.status_code, ServiceStatusCode
        ):
            raise TypeError("service status must use the stable vocabulary")
        started = require_aware(self.started_at, "started_at")
        heartbeat = require_aware(self.heartbeat_at, "heartbeat_at")
        successful = (
            None
            if self.last_successful_operation_at is None
            else require_aware(self.last_successful_operation_at, "last_successful_operation_at")
        )
        if started > heartbeat or (
            successful is not None and not started <= successful <= heartbeat
        ):
            raise ValueError("service status timestamps are out of order")
        if self.schema_revision != EXPECTED_SCHEMA_REVISION:
            raise ValueError("service schema revision is incompatible")
        exact_codes = {
            ServiceReadiness.STARTING: ServiceStatusCode.STARTING,
            ServiceReadiness.READY: ServiceStatusCode.READY,
            ServiceReadiness.DRAINING: ServiceStatusCode.DRAINING,
            ServiceReadiness.STOPPED: ServiceStatusCode.STOPPED,
        }
        expected = exact_codes.get(self.readiness)
        if expected is not None and self.status_code is not expected:
            raise ValueError("service readiness and status code do not match")
        non_failure_codes = frozenset(
            {
                ServiceStatusCode.STARTING,
                ServiceStatusCode.READY,
                ServiceStatusCode.DRAINING,
                ServiceStatusCode.STOPPED,
            }
        )
        if self.readiness is ServiceReadiness.DEGRADED and self.status_code in non_failure_codes:
            raise ValueError("degraded status requires a stable degraded or failure code")
        if self.readiness is ServiceReadiness.NOT_READY and self.status_code in (
            non_failure_codes | {ServiceStatusCode.DEGRADED}
        ):
            raise ValueError("not-ready status requires a stable failure code")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "heartbeat_at", heartbeat)
        object.__setattr__(self, "last_successful_operation_at", successful)


@dataclass(frozen=True, slots=True)
class ServiceStatusUpdate:
    transition_recorded: bool
    version: int


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    erasure_replay_verified: bool
    unknown_send_reconciled: bool
    credentials_verified: bool
    session_verified: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.erasure_replay_verified,
                self.unknown_send_reconciled,
                self.credentials_verified,
                self.session_verified,
            )
        ):
            raise TypeError("restore verification values must be booleans")

    @property
    def complete(self) -> bool:
        return all(
            (
                self.erasure_replay_verified,
                self.unknown_send_reconciled,
                self.credentials_verified,
                self.session_verified,
            )
        )


@dataclass(frozen=True, slots=True)
class RestoreGateRecord:
    deployment_id: str
    account_id: UUID
    state: RestoreGateState
    restore_generation: int
    verification: RestoreVerification
    verified_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.deployment_id, str)
            or _DEPLOYMENT_ID.fullmatch(self.deployment_id) is None
            or not isinstance(self.account_id, UUID)
            or self.account_id.int == 0
        ):
            raise ValueError("restore gate identity is invalid")
        if not isinstance(self.state, RestoreGateState):
            raise TypeError("restore gate state must use the stable vocabulary")
        if self.restore_generation <= 0 or self.version <= 0:
            raise ValueError("restore gate versions must be positive")
        verified_at = (
            None if self.verified_at is None else require_aware(self.verified_at, "verified_at")
        )
        if self.state is RestoreGateState.OPEN and (
            not self.verification.complete or verified_at is None
        ):
            raise ValueError("open restore gate requires complete verification")
        object.__setattr__(self, "verified_at", verified_at)


__all__ = [
    "RestoreGateRecord",
    "RestoreGateState",
    "RestoreVerification",
    "ServiceHeartbeat",
    "ServiceReadiness",
    "ServiceStatusCode",
    "ServiceStatusMetadata",
    "ServiceStatusUpdate",
]
