import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health import (
    HEALTH_SNAPSHOT_VERSION,
    MAX_HEALTH_SNAPSHOT_BYTES,
    HealthReason,
    HealthSnapshotError,
    HealthState,
    ReadinessPolicy,
    ServiceName,
    disk_safety_ok,
    read_health_snapshot,
    write_health_snapshot,
)

NOW = UtcTimestamp(datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC))


def health_state(service: ServiceName = ServiceName.APP, **overrides: object) -> HealthState:
    service_fields: dict[str, bool | None] = {
        "account_ready": None,
        "session_owned": None,
        "telegram_ready": None,
        "control_bot_ready": None,
        "web_api_ready": None,
        "consumer_ready": None,
    }
    if service is ServiceName.APP:
        service_fields.update(account_ready=True, session_owned=True, telegram_ready=True)
    elif service is ServiceName.CONTROL:
        service_fields.update(control_bot_ready=True, web_api_ready=True)
    else:
        service_fields["consumer_ready"] = True
    values: dict[str, object] = {
        "version": HEALTH_SNAPSHOT_VERSION,
        "service": service,
        "observed_at": NOW,
        "heartbeat_at": NOW,
        "process_loop_ok": True,
        "maintenance": False,
        "draining": False,
        "required_config_ok": True,
        "disk_safety_ok": True,
        "database_ok": True,
        "redis_ok": True,
        "schema_ok": True,
        "restore_gate_open": True,
        **service_fields,
        **overrides,
    }
    return HealthState(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_liveness_only_uses_process_loop_and_heartbeat_freshness() -> None:
    policy = ReadinessPolicy()
    state = health_state(
        maintenance=True,
        draining=True,
        required_config_ok=False,
        disk_safety_ok=False,
        database_ok=False,
        redis_ok=False,
        schema_ok=False,
        restore_gate_open=False,
        account_ready=False,
        session_owned=False,
        telegram_ready=False,
    )

    live = policy.liveness(state, now=NOW)
    ready = policy.readiness(state, now=NOW)

    assert live.healthy
    assert live.reason is HealthReason.LIVE
    assert not ready.healthy
    assert ready.reason is HealthReason.MAINTENANCE_ACTIVE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (health_state(process_loop_ok=False), HealthReason.PROCESS_LOOP_FAILED),
        (health_state(maintenance=True), HealthReason.MAINTENANCE_ACTIVE),
        (health_state(draining=True), HealthReason.DRAINING),
        (health_state(required_config_ok=False), HealthReason.REQUIRED_CONFIG_NOT_READY),
        (health_state(disk_safety_ok=False), HealthReason.DISK_SAFETY_NOT_READY),
        (health_state(database_ok=False), HealthReason.DATABASE_UNAVAILABLE),
        (health_state(redis_ok=False), HealthReason.REDIS_UNAVAILABLE),
        (health_state(schema_ok=False), HealthReason.SCHEMA_NOT_READY),
        (health_state(restore_gate_open=False), HealthReason.RESTORE_GATE_CLOSED),
        (health_state(account_ready=False), HealthReason.ACCOUNT_NOT_READY),
        (health_state(session_owned=False), HealthReason.SESSION_NOT_OWNED),
        (health_state(telegram_ready=False), HealthReason.TELEGRAM_NOT_READY),
    ],
)
def test_app_readiness_is_a_strict_conjunction(state: HealthState, reason: HealthReason) -> None:
    decision = ReadinessPolicy().readiness(state, now=NOW)
    assert not decision.healthy
    assert decision.reason is reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (
            health_state(ServiceName.CONTROL, control_bot_ready=False),
            HealthReason.CONTROL_BOT_NOT_READY,
        ),
        (
            health_state(ServiceName.CONTROL, web_api_ready=False),
            HealthReason.WEB_API_NOT_READY,
        ),
        (
            health_state(ServiceName.WORKER, consumer_ready=False),
            HealthReason.CONSUMER_NOT_READY,
        ),
    ],
)
def test_service_specific_readiness(state: HealthState, reason: HealthReason) -> None:
    decision = ReadinessPolicy().readiness(state, now=NOW)
    assert not decision.healthy
    assert decision.reason is reason


@pytest.mark.unit
@pytest.mark.parametrize("service", tuple(ServiceName))
def test_each_service_can_be_ready(service: ServiceName) -> None:
    decision = ReadinessPolicy().readiness(health_state(service), now=NOW)
    assert decision.healthy
    assert decision.reason is HealthReason.READY


@pytest.mark.unit
@pytest.mark.parametrize(
    ("total_bytes", "available_bytes", "expected"),
    [
        (100 << 30, 6 << 30, True),
        (100 << 30, 5 << 30, False),
        (100 << 30, (1 << 30) - 1, False),
        (100 << 30, 1 << 30, False),
        (10 << 30, 1 << 30, True),
    ],
)
def test_disk_safety_closes_at_either_exact_boundary(
    total_bytes: int, available_bytes: int, expected: bool
) -> None:
    assert disk_safety_ok(total_bytes=total_bytes, available_bytes=available_bytes) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("total_bytes", "available_bytes"), [(0, 0), (10, -1), (10, 11), (True, 1)]
)
def test_disk_safety_rejects_invalid_capacity_values(
    total_bytes: int, available_bytes: int
) -> None:
    with pytest.raises(ValueError, match="capacity"):
        disk_safety_ok(total_bytes=total_bytes, available_bytes=available_bytes)


@pytest.mark.unit
def test_heartbeat_staleness_and_future_clock_skew_fail_liveness() -> None:
    policy = ReadinessPolicy()
    stale = health_state(heartbeat_at=NOW.add(-timedelta(seconds=31)))
    future_observation = NOW.add(timedelta(seconds=6))
    future = health_state(observed_at=future_observation, heartbeat_at=future_observation)
    assert policy.liveness(stale, now=NOW).reason is HealthReason.HEARTBEAT_STALE
    assert policy.liveness(future, now=NOW).reason is HealthReason.HEARTBEAT_INVALID


@pytest.mark.unit
def test_state_rejects_missing_or_non_applicable_service_status() -> None:
    with pytest.raises(TypeError, match="required service"):
        health_state(account_ready=None)
    with pytest.raises(ValueError, match="non-applicable"):
        health_state(ServiceName.CONTROL, telegram_ready=True)
    with pytest.raises(ValueError, match="newer"):
        health_state(heartbeat_at=NOW.add(timedelta(microseconds=1)))


@pytest.mark.unit
def test_snapshot_round_trip_is_atomic_bounded_and_content_free(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    state = health_state()
    write_health_snapshot(path, state)
    write_health_snapshot(path, replace(state, maintenance=True))

    restored = read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert restored.maintenance is True
    assert path.stat().st_size <= MAX_HEALTH_SNAPSHOT_BYTES
    assert tuple(tmp_path.iterdir()) == (path,)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
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
    assert all(value is None or type(value) in {bool, int, str} for value in payload.values())


@pytest.mark.unit
def test_snapshot_service_and_freshness_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    with pytest.raises(HealthSnapshotError) as mismatch:
        read_health_snapshot(path, expected_service=ServiceName.WORKER, now=NOW)
    assert mismatch.value.reason is HealthReason.SNAPSHOT_SERVICE_MISMATCH

    stale_time = NOW.add(-timedelta(seconds=31))
    write_health_snapshot(path, health_state(observed_at=stale_time, heartbeat_at=stale_time))
    with pytest.raises(HealthSnapshotError) as stale:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert stale.value.reason is HealthReason.SNAPSHOT_STALE


@pytest.mark.unit
def test_snapshot_rejects_missing_non_regular_and_oversized_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(HealthSnapshotError) as missing_error:
        read_health_snapshot(missing, expected_service=ServiceName.APP, now=NOW)
    assert missing_error.value.reason is HealthReason.SNAPSHOT_MISSING

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(HealthSnapshotError) as regular_error:
        read_health_snapshot(directory, expected_service=ServiceName.APP, now=NOW)
    assert regular_error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_HEALTH_SNAPSHOT_BYTES + 1))
    oversized.chmod(0o600)
    with pytest.raises(HealthSnapshotError) as size_error:
        read_health_snapshot(oversized, expected_service=ServiceName.APP, now=NOW)
    assert size_error.value.reason is HealthReason.SNAPSHOT_OVERSIZED


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation",
    [
        {"version": 2},
        {"process_loop_ok": 1},
        {"unexpected": "not-allowed"},
        {"service": "unknown"},
    ],
)
def test_snapshot_rejects_version_type_unknown_fields_and_service(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(mutation)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    expected = (
        HealthReason.SNAPSHOT_VERSION_MISMATCH
        if mutation == {"version": 2}
        else HealthReason.SNAPSHOT_INVALID
    )
    assert error.value.reason is expected


@pytest.mark.unit
def test_snapshot_rejects_symlink_when_platform_allows_creation(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "health.json"
    write_health_snapshot(target, health_state())
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("file symlink creation is unavailable on this Windows host")
    with pytest.raises(HealthSnapshotError) as write_error:
        write_health_snapshot(link, health_state(maintenance=True))
    assert write_error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR
    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(link, expected_service=ServiceName.APP, now=NOW)
    assert error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR
    assert (
        read_health_snapshot(target, expected_service=ServiceName.APP, now=NOW).maintenance is False
    )


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="Unix permission evidence requires a POSIX host")
def test_snapshot_rejects_group_or_world_permissions_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert error.value.reason is HealthReason.SNAPSHOT_PERMISSIONS


@pytest.mark.unit
def test_snapshot_write_rejects_unsafe_parent_and_existing_directory(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    with pytest.raises(HealthSnapshotError) as parent_error:
        write_health_snapshot(parent_file / "health.json", health_state())
    assert parent_error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR

    target_directory = tmp_path / "health.json"
    target_directory.mkdir()
    with pytest.raises(HealthSnapshotError) as target_error:
        write_health_snapshot(target_directory, health_state())
    assert target_error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR


@pytest.mark.unit
@pytest.mark.parametrize("content", [b"", b"{", b"\xff"])
def test_snapshot_read_rejects_empty_truncated_or_non_utf8_json(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "health.json"
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert error.value.reason is HealthReason.SNAPSHOT_INVALID


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"observed_at": 123}, HealthReason.SNAPSHOT_INVALID),
        ({"observed_at": "not-a-timestamp"}, HealthReason.SNAPSHOT_INVALID),
        ({"account_ready": 1}, HealthReason.SNAPSHOT_INVALID),
    ],
)
def test_snapshot_read_rejects_timestamp_and_optional_status_type_drift(
    tmp_path: Path, mutation: dict[str, object], reason: HealthReason
) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(mutation)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert error.value.reason is reason


@pytest.mark.unit
def test_snapshot_read_rejects_future_observation_and_nonpositive_age(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    future = NOW.add(timedelta(seconds=6))
    write_health_snapshot(path, health_state(observed_at=future, heartbeat_at=future))

    with pytest.raises(HealthSnapshotError) as future_error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert future_error.value.reason is HealthReason.SNAPSHOT_INVALID

    with pytest.raises(ValueError, match="max age"):
        read_health_snapshot(
            path,
            expected_service=ServiceName.APP,
            now=NOW,
            max_age=timedelta(0),
        )


@pytest.mark.unit
def test_snapshot_read_fails_closed_if_target_changes_between_path_and_fd_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    real_fstat = os.fstat

    def changed_inode(descriptor: int) -> SimpleNamespace:
        opened = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=opened.st_mode,
            st_size=opened.st_size,
            st_dev=opened.st_dev,
            st_ino=opened.st_ino + 1,
        )

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.fstat", changed_inode)

    with pytest.raises(HealthSnapshotError) as error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert error.value.reason is HealthReason.SNAPSHOT_NOT_REGULAR


@pytest.mark.unit
def test_snapshot_read_fails_closed_when_open_or_bounded_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())

    def fail_open(path_value: Path, flags: int) -> int:
        del path_value, flags
        raise OSError("synthetic open failure")

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.open", fail_open)
    with pytest.raises(HealthSnapshotError) as open_error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert open_error.value.reason is HealthReason.SNAPSHOT_UNREADABLE

    monkeypatch.undo()

    def grow_after_stat(descriptor: int, size: int) -> bytes:
        del descriptor, size
        return b"x" * (MAX_HEALTH_SNAPSHOT_BYTES + 1)

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.read", grow_after_stat)
    with pytest.raises(HealthSnapshotError) as size_error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert size_error.value.reason is HealthReason.SNAPSHOT_OVERSIZED


@pytest.mark.unit
def test_snapshot_read_maps_fstat_and_close_failures_to_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())

    real_fstat = os.fstat

    def fail_fstat(descriptor: int) -> os.stat_result:
        del descriptor
        raise OSError("synthetic fstat failure")

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.fstat", fail_fstat)
    with pytest.raises(HealthSnapshotError) as fstat_error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert fstat_error.value.reason is HealthReason.SNAPSHOT_UNREADABLE

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.fstat", real_fstat)
    real_close = os.close

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("synthetic close failure")

    monkeypatch.setattr("telegram_userbot.platform.health.snapshot.os.close", fail_close)
    with pytest.raises(HealthSnapshotError) as close_error:
        read_health_snapshot(path, expected_service=ServiceName.APP, now=NOW)
    assert close_error.value.reason is HealthReason.SNAPSHOT_UNREADABLE


@pytest.mark.unit
def test_snapshot_write_cleans_temporary_file_after_atomic_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "health.json"
    real_replace = Path.replace

    def fail_snapshot_replace(source: Path, target: Path) -> Path:
        if source.name.endswith(".tmp"):
            raise OSError("synthetic replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_snapshot_replace)

    with pytest.raises(HealthSnapshotError) as error:
        write_health_snapshot(path, health_state())
    assert error.value.reason is HealthReason.SNAPSHOT_UNREADABLE
    assert tuple(tmp_path.iterdir()) == ()
