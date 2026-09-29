"""Atomic, bounded, fail-closed health snapshots for the runtime tmpfs."""

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import cast

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health.model import (
    HEALTH_SNAPSHOT_VERSION,
    HealthReason,
    HealthState,
    ServiceName,
)

DEFAULT_HEALTH_SNAPSHOT_PATH = Path("/var/lib/telegram-userbot/runtime/health.json")
MAX_HEALTH_SNAPSHOT_BYTES = 4096
DEFAULT_SNAPSHOT_MAX_AGE = timedelta(seconds=30)
_FUTURE_CLOCK_SKEW = timedelta(seconds=5)
_FIELDS = frozenset(
    {
        "version",
        "service",
        "observed_at",
        "heartbeat_at",
        "process_loop_ok",
        "maintenance",
        "draining",
        "required_config_ok",
        "disk_safety_ok",
        "database_ok",
        "redis_ok",
        "schema_ok",
        "restore_gate_open",
        "account_ready",
        "session_owned",
        "telegram_ready",
        "control_bot_ready",
        "web_api_ready",
        "consumer_ready",
    }
)


class HealthSnapshotError(RuntimeError):
    """A content-free snapshot failure suitable for direct probe output."""

    def __init__(self, reason: HealthReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _payload(state: HealthState) -> dict[str, object]:
    return {
        "version": state.version,
        "service": state.service.value,
        "observed_at": state.observed_at.to_iso(),
        "heartbeat_at": state.heartbeat_at.to_iso(),
        "process_loop_ok": state.process_loop_ok,
        "maintenance": state.maintenance,
        "draining": state.draining,
        "required_config_ok": state.required_config_ok,
        "disk_safety_ok": state.disk_safety_ok,
        "database_ok": state.database_ok,
        "redis_ok": state.redis_ok,
        "schema_ok": state.schema_ok,
        "restore_gate_open": state.restore_gate_open,
        "account_ready": state.account_ready,
        "session_owned": state.session_owned,
        "telegram_ready": state.telegram_ready,
        "control_bot_ready": state.control_bot_ready,
        "web_api_ready": state.web_api_ready,
        "consumer_ready": state.consumer_ready,
    }


def _require_safe_parent(path: Path) -> None:
    try:
        parent_stat = path.parent.lstat()
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_NOT_REGULAR)


def _require_replaceable_target(path: Path) -> None:
    try:
        target_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_NOT_REGULAR)


def write_health_snapshot(path: Path, state: HealthState) -> None:
    """Atomically replace one same-directory snapshot with mode 0600."""

    encoded = json.dumps(_payload(state), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_HEALTH_SNAPSHOT_BYTES:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_OVERSIZED)
    _require_safe_parent(path)
    _require_replaceable_target(path)

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, mode="wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _require_replaceable_target(path)
        Path(temporary_name).replace(path)
        temporary_name = ""
    except HealthSnapshotError:
        raise
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def _secure_regular_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_NOT_REGULAR)
    if file_stat.st_size > MAX_HEALTH_SNAPSHOT_BYTES:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_OVERSIZED)
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_PERMISSIONS)


def _read_opened_regular_file(descriptor: int, path_stat: os.stat_result) -> bytes:
    try:
        opened_stat = os.fstat(descriptor)
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    _secure_regular_stat(opened_stat)
    if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_NOT_REGULAR)
    try:
        return os.read(descriptor, MAX_HEALTH_SNAPSHOT_BYTES + 1)
    except OSError as error:
        # Keep the probe's public failure contract stable when the opened
        # file disappears or an I/O error occurs during validation/read.
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error


def _read_bounded_regular_file(path: Path) -> bytes:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_MISSING) from error
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_NOT_REGULAR)
    _secure_regular_stat(path_stat)

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    read_failed = False
    try:
        content = _read_opened_regular_file(descriptor, path_stat)
    except BaseException:
        read_failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            # Do not replace a more specific validation error while unwinding,
            # but fail closed when close itself is the first I/O failure.
            if not read_failed:
                raise HealthSnapshotError(HealthReason.SNAPSHOT_UNREADABLE) from error
    if len(content) > MAX_HEALTH_SNAPSHOT_BYTES:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_OVERSIZED)
    return content


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    return value


def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = payload[key]
    if value is not None and type(value) is not bool:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    return value


def _timestamp(payload: Mapping[str, object], key: str) -> UtcTimestamp:
    value = payload[key]
    if not isinstance(value, str):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    try:
        return UtcTimestamp.from_iso(value)
    except (TypeError, ValueError) as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID) from error


def _decode_state(content: bytes) -> HealthState:
    try:
        decoded: object = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID) from error
    if not isinstance(decoded, dict) or set(decoded) != _FIELDS:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    payload = cast(dict[str, object], decoded)
    version = payload["version"]
    if type(version) is not int:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    if version != HEALTH_SNAPSHOT_VERSION:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_VERSION_MISMATCH)
    service_value = payload["service"]
    if not isinstance(service_value, str):
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    try:
        service = ServiceName(service_value)
        return HealthState(
            version=version,
            service=service,
            observed_at=_timestamp(payload, "observed_at"),
            heartbeat_at=_timestamp(payload, "heartbeat_at"),
            process_loop_ok=_required_bool(payload, "process_loop_ok"),
            maintenance=_required_bool(payload, "maintenance"),
            draining=_required_bool(payload, "draining"),
            required_config_ok=_required_bool(payload, "required_config_ok"),
            disk_safety_ok=_required_bool(payload, "disk_safety_ok"),
            database_ok=_required_bool(payload, "database_ok"),
            redis_ok=_required_bool(payload, "redis_ok"),
            schema_ok=_required_bool(payload, "schema_ok"),
            restore_gate_open=_required_bool(payload, "restore_gate_open"),
            account_ready=_optional_bool(payload, "account_ready"),
            session_owned=_optional_bool(payload, "session_owned"),
            telegram_ready=_optional_bool(payload, "telegram_ready"),
            control_bot_ready=_optional_bool(payload, "control_bot_ready"),
            web_api_ready=_optional_bool(payload, "web_api_ready"),
            consumer_ready=_optional_bool(payload, "consumer_ready"),
        )
    except (TypeError, ValueError) as error:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID) from error


def read_health_snapshot(
    path: Path,
    *,
    expected_service: ServiceName,
    now: UtcTimestamp,
    max_age: timedelta = DEFAULT_SNAPSHOT_MAX_AGE,
) -> HealthState:
    """Read and validate a same-service, fresh snapshot or fail closed."""

    if max_age <= timedelta(0):
        raise ValueError("snapshot max age must be positive")
    state = _decode_state(_read_bounded_regular_file(path))
    if state.service is not expected_service:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_SERVICE_MISMATCH)
    if state.observed_at.value > now.value + _FUTURE_CLOCK_SKEW:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_INVALID)
    if now.value - state.observed_at.value > max_age:
        raise HealthSnapshotError(HealthReason.SNAPSHOT_STALE)
    return state
