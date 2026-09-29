from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    path = ROOT / "deploy" / "ops" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"m8_test_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_export_queries_are_explicit_and_never_reconstruct_erased_or_secret_fields() -> None:
    module = _load_script("data_export")
    queries = {section: query for section, query, _contact_scoped in module._EXPORT_QUERIES}

    assert queries
    for query in queries.values():
        normalized = " ".join(query.lower().split())
        assert re.search(r"select\s+(?:[a-z_]+\.)?\*", normalized) is None
        assert "access_hash" not in normalized
        assert "telegram_file_ref" not in normalized

    assert "mr.redacted_at is null" in queries["message_revisions"].lower()
    assert "mr.redacted_at is null" in queries["message_media"].lower()
    assert "mr.redacted_at is null" in queries["media_metadata"].lower()
    memories = queries["memories"].lower()
    assert "status <> 'forgotten'" in memories
    assert "forgotten_at is null" in memories
    memory_versions = queries["memory_versions"].lower()
    assert "mv.redacted_at is null" in memory_versions
    assert "m.status <> 'forgotten'" in memory_versions
    assert "m.forgotten_at is null" in memory_versions
    assert "sv.redacted_at is null" in queries["summary_versions"].lower()
    assert "metadata" not in queries["account_peers"].lower()
    assert "metadata" not in queries["message_media"].lower()
    assert "metadata" not in queries["audit"].lower()

    protected_views = {
        "account_peers": "export_account_peers_v1",
        "message_revisions": "export_message_revisions_v1",
        "message_media": "export_message_media_v1",
        "memories": "export_memories_v1",
        "memory_versions": "export_memory_versions_v1",
        "summary_versions": "export_summary_versions_v1",
    }
    for section, view in protected_views.items():
        assert view in queries[section].lower()
    assert "export_account_peers_v1" in queries["telegram_peers"].lower()
    assert "export_message_media_v1" in queries["media_metadata"].lower()


@pytest.mark.unit
def test_monitor_disk_stats_matches_canonical_95_percent_and_one_gib_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("monitor")
    gib = 1024**3

    monkeypatch.setattr(
        module.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_blocks=100, f_bavail=5, f_frsize=gib),
        raising=False,
    )
    used_percent, available, at_95 = module._disk_stats(Path("/unused"))
    assert (used_percent, available, at_95) == (95.0, 5 * gib, True)

    monkeypatch.setattr(
        module.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_blocks=10, f_bavail=1, f_frsize=gib - 1),
        raising=False,
    )
    used_percent, available, at_95 = module._disk_stats(Path("/unused"))
    assert used_percent == 90.0
    assert available == gib - 1
    assert not at_95


class _Rows:
    def __init__(self, *, rows: list[tuple[object, ...]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _MonitorConnection:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self._instances = [
            ("app", "stopped", now - timedelta(seconds=40), uuid4()),
            ("app", "ready", now - timedelta(seconds=2), uuid4()),
            ("control", "degraded", now - timedelta(seconds=3), uuid4()),
            ("worker", "not_ready", now - timedelta(seconds=4), uuid4()),
        ]

    def __enter__(self) -> _MonitorConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(  # noqa: PLR0911, PLR0912 - query fake enumerates the monitor contract
        self, query: str, _parameters: object = None
    ) -> _Rows:
        normalized = " ".join(query.split())
        if "FROM service_instances" in normalized:
            assert "DISTINCT ON (service_name)" in normalized
            assert "last_heartbeat_at DESC, instance_id DESC" in normalized
            current: dict[str, tuple[str, datetime, object]] = {}
            for service, readiness, heartbeat, instance_id in self._instances:
                candidate = (readiness, heartbeat, instance_id)
                previous = current.get(service)
                if previous is None or (heartbeat, str(instance_id)) > (
                    previous[1],
                    str(previous[2]),
                ):
                    current[service] = candidate
            return _Rows(
                rows=[
                    (service, value[0], (self._now - value[1]).total_seconds())
                    for service, value in sorted(current.items())
                ]
            )
        if "FROM background_jobs" in normalized:
            if "GROUP BY state" in normalized:
                return _Rows(rows=[("retry_wait", 2), ("cancelled", 1)])
            if "state IN ('pending','retry_wait')" in normalized:
                return _Rows(rows=[(2, 12.0)])
            if "lease_expires_at <=" in normalized:
                return _Rows(rows=[(0,)])
        if "FROM transactional_outbox" in normalized:
            return _Rows(rows=[(0, 0.0)])
        if "FROM outbound_delivery_groups" in normalized:
            return _Rows(rows=[] if "GROUP BY state" in normalized else [(0.0,)])
        if "FROM data_erasure_requests" in normalized:
            return _Rows(rows=[(0, 0.0)])
        if "FROM data_export_requests" in normalized:
            return _Rows(rows=[(0,)])
        if "FROM model_run_attempts" in normalized:
            if "GROUP BY state" in normalized:
                return _Rows(rows=[("succeeded", 3), ("retryable_failed", 1)])
            if "percentile_cont" in normalized:
                return _Rows(rows=[(1.25, 1, 0)])
            if "state IN ('started','unknown')" in normalized:
                return _Rows(rows=[(1, 61.0)])
        if "FROM deployment_restore_state" in normalized:
            return _Rows(rows=[("open",)])
        raise AssertionError(normalized)


@pytest.mark.unit
def test_monitor_uses_latest_service_projection_complete_jobs_and_free_space_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("monitor")
    now = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
    ops_state = tmp_path / "ops-state"
    media = tmp_path / "media"
    ops_state.mkdir()
    media.mkdir()
    for kind in (
        "wal-archive",
        "postgres-backup",
        "session-backup",
        "restore-drill",
        "erasure-replica",
    ):
        marker: dict[str, object] = {
            "schema_version": 1,
            "kind": kind,
            "completed_at": now.isoformat().replace("+00:00", "Z"),
            "result": "PASS",
        }
        if kind == "restore-drill":
            marker["ledger_snapshot_id"] = "synthetic-ledger-a"
        (ops_state / f"{kind}.json").write_text(
            json.dumps(marker, separators=(",", ":")),
            encoding="ascii",
        )
    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setenv("OPS_STATE_DIR", str(ops_state))
    monkeypatch.setenv("MEDIA_DATA_DIR", str(media))
    monkeypatch.setattr(module, "_database_connection", lambda: _MonitorConnection(now))
    monkeypatch.setattr(module, "_disk_stats", lambda _path: (10.0, 1024**3 - 1, False))

    metrics, alerts = module.collect_snapshot(now=now)
    codes = {item["code"] for item in alerts}

    assert 'tudt_service_instances{readiness="ready",service="app"} 1' in metrics
    assert 'tudt_service_instances{readiness="stopped",service="app"} 0' in metrics
    assert 'tudt_jobs{state="retry_wait"} 2' in metrics
    assert 'tudt_jobs{state="failed"} 0' in metrics
    assert 'tudt_jobs{state="cancelled"} 1' in metrics
    assert "tudt_queue_depth 2" in metrics
    assert "tudt_queue_oldest_age_seconds 12.0" in metrics
    assert 'tudt_send_reconciliation_pending{state="unknown"} 0' in metrics
    assert "tudt_erasure_pending 0" in metrics
    assert "tudt_restore_drill_age_seconds 0.0" in metrics
    assert "tudt_wal_archive_check_age_seconds 0.0" in metrics
    assert 'tudt_provider_attempts_15m{state="succeeded"} 3' in metrics
    assert 'tudt_provider_attempts_15m{state="retryable_failed"} 1' in metrics
    assert "tudt_provider_latency_p95_seconds_15m 1.25" in metrics
    assert "tudt_provider_rate_limited_15m 1" in metrics
    assert "tudt_provider_inflight_oldest_age_seconds 61.0" in metrics
    assert f'tudt_disk_available_bytes{{scope="media-volume"}} {1024**3 - 1}' in metrics
    assert "APP_STOPPED" not in codes
    assert "CONTROL_DEGRADED" in codes
    assert "WORKER_NOT_READY" in codes
    assert "DISK_FREE_LT_1G" in codes
    assert "PROVIDER_INFLIGHT_SLOW" in codes
    assert "PROVIDER_RATE_LIMITED" in codes


@pytest.mark.unit
def test_session_backup_identity_json_rejects_duplicate_security_keys(tmp_path: Path) -> None:
    module = _load_script("run_session_backup")
    identity = tmp_path / "deployment.json"
    identity.write_text(
        '{"deployment_id":"one","deployment_id":"two","runtime_identity":{"telegram_user_id":1}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"^DEPLOYMENT_CONFIG_INVALID$"):
        module._load_identity(identity)
