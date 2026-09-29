"""Content-free health vocabulary shared by runtime processes and probes."""

from dataclasses import dataclass
from enum import StrEnum

from telegram_userbot.domain.shared.time import UtcTimestamp

HEALTH_SNAPSHOT_VERSION = 1


class ServiceName(StrEnum):
    APP = "app"
    CONTROL = "control"
    WORKER = "worker"


class HealthKind(StrEnum):
    LIVE = "live"
    READY = "ready"


class HealthReason(StrEnum):
    """Stable, content-free probe result codes."""

    LIVE = "LIVE"
    READY = "READY"
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
    SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
    SNAPSHOT_UNREADABLE = "SNAPSHOT_UNREADABLE"
    SNAPSHOT_NOT_REGULAR = "SNAPSHOT_NOT_REGULAR"
    SNAPSHOT_PERMISSIONS = "SNAPSHOT_PERMISSIONS"
    SNAPSHOT_OVERSIZED = "SNAPSHOT_OVERSIZED"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
    SNAPSHOT_VERSION_MISMATCH = "SNAPSHOT_VERSION_MISMATCH"
    SNAPSHOT_SERVICE_MISMATCH = "SNAPSHOT_SERVICE_MISMATCH"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"


@dataclass(frozen=True, slots=True)
class HealthState:
    """One bounded status snapshot with no identifiers, content, or error text."""

    version: int
    service: ServiceName
    observed_at: UtcTimestamp
    heartbeat_at: UtcTimestamp
    process_loop_ok: bool
    maintenance: bool
    draining: bool
    required_config_ok: bool
    disk_safety_ok: bool
    database_ok: bool
    redis_ok: bool
    schema_ok: bool
    restore_gate_open: bool
    account_ready: bool | None = None
    session_owned: bool | None = None
    telegram_ready: bool | None = None
    control_bot_ready: bool | None = None
    web_api_ready: bool | None = None
    consumer_ready: bool | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != HEALTH_SNAPSHOT_VERSION:
            raise ValueError("unsupported health snapshot version")
        if not isinstance(self.service, ServiceName):
            raise TypeError("health service must use the stable service vocabulary")
        if self.heartbeat_at > self.observed_at:
            raise ValueError("heartbeat cannot be newer than its observation")
        common = (
            self.process_loop_ok,
            self.maintenance,
            self.draining,
            self.required_config_ok,
            self.disk_safety_ok,
            self.database_ok,
            self.redis_ok,
            self.schema_ok,
            self.restore_gate_open,
        )
        if any(type(value) is not bool for value in common):
            raise TypeError("common health states must be booleans")

        service_fields = {
            ServiceName.APP: (
                (self.account_ready, self.session_owned, self.telegram_ready),
                (self.control_bot_ready, self.web_api_ready, self.consumer_ready),
            ),
            ServiceName.CONTROL: (
                (self.control_bot_ready, self.web_api_ready),
                (
                    self.account_ready,
                    self.session_owned,
                    self.telegram_ready,
                    self.consumer_ready,
                ),
            ),
            ServiceName.WORKER: (
                (self.consumer_ready,),
                (
                    self.account_ready,
                    self.session_owned,
                    self.telegram_ready,
                    self.control_bot_ready,
                    self.web_api_ready,
                ),
            ),
        }
        required, not_applicable = service_fields[self.service]
        if any(type(value) is not bool for value in required):
            raise TypeError("required service health states must be booleans")
        if any(value is not None for value in not_applicable):
            raise ValueError("non-applicable service health states must be null")

    @classmethod
    def fail_closed(
        cls,
        service: ServiceName,
        *,
        observed_at: UtcTimestamp,
        draining: bool,
        process_loop_ok: bool,
    ) -> HealthState:
        """Create a not-ready baseline without claiming dependency observations."""

        service_fields: dict[str, bool | None] = {
            "account_ready": None,
            "session_owned": None,
            "telegram_ready": None,
            "control_bot_ready": None,
            "web_api_ready": None,
            "consumer_ready": None,
        }
        if service is ServiceName.APP:
            service_fields.update(
                account_ready=False,
                session_owned=False,
                telegram_ready=False,
            )
        elif service is ServiceName.CONTROL:
            service_fields.update(control_bot_ready=False, web_api_ready=False)
        else:
            service_fields["consumer_ready"] = False
        return cls(
            version=HEALTH_SNAPSHOT_VERSION,
            service=service,
            observed_at=observed_at,
            heartbeat_at=observed_at,
            process_loop_ok=process_loop_ok,
            maintenance=False,
            draining=draining,
            required_config_ok=False,
            disk_safety_ok=False,
            database_ok=False,
            redis_ok=False,
            schema_ok=False,
            restore_gate_open=False,
            **service_fields,
        )
