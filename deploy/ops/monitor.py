"""Internal-only, content-free Prometheus and alert snapshot endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import psycopg

_DATABASE_LOGIN = "telegram_userbot_monitor_login"
_DATABASE_ROLE = "telegram_userbot_monitor_runtime"
_MAX_MARKER_BYTES = 4096
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_SERVICE_NAMES = ("app", "control", "worker")
_READINESS = ("starting", "ready", "degraded", "not_ready", "draining", "stopped")
_JOB_STATES = (
    "pending",
    "leased",
    "retry_wait",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
)
_MODEL_ATTEMPT_STATES = (
    "started",
    "succeeded",
    "retryable_failed",
    "terminal_failed",
    "cancelled",
    "unknown",
)
_MIN_OPERATIONAL_FREE_BYTES = 1024**3


class MonitorError(RuntimeError):
    """Stable monitor failure without database, path, or credential details."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MonitorError("MARKER_INVALID")
        result[key] = value
    return result


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise MonitorError("SECRET_INVALID")
    raw = path.read_bytes()
    if not 32 <= len(raw) <= 128 or re.fullmatch(rb"[A-Za-z0-9._~-]+", raw) is None:
        raise MonitorError("SECRET_INVALID")
    return raw.decode("ascii")


def _database_connection() -> psycopg.Connection[tuple[Any, ...]]:
    expected = {
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "telegram_userbot",
        "DATABASE_USER": _DATABASE_LOGIN,
        "DATABASE_RUNTIME_ROLE": _DATABASE_ROLE,
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise MonitorError("DATABASE_CONFIG_INVALID")
    password_file = Path(os.environ.get("DATABASE_PASSWORD_FILE", ""))
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname="telegram_userbot",
        user=_DATABASE_LOGIN,
        password=_read_secret(password_file),
        application_name="telegram_userbot_ops_monitor",
        connect_timeout=3,
        options=f"-c role={_DATABASE_ROLE} -c statement_timeout=3000 -c lock_timeout=1000",
    )


def _metric(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{key}="{labels[key]}"' for key in sorted(labels)) + "}"
    return f"{name}{suffix} {value}"


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MonitorError("MARKER_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MonitorError("MARKER_INVALID") from None
    if parsed.tzinfo is None:
        raise MonitorError("MARKER_INVALID")
    return parsed.astimezone(UTC)


def _backup_age_seconds(marker_root: Path, name: str, now: datetime) -> float | None:
    path = marker_root / f"{name}.json"
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= _MAX_MARKER_BYTES:
        return None
    try:
        document = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise MonitorError("MARKER_INVALID") from None
    expected_fields = {
        "schema_version",
        "kind",
        "completed_at",
        "result",
    }
    if name == "restore-drill":
        expected_fields.add("ledger_snapshot_id")
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise MonitorError("MARKER_INVALID")
    if document["schema_version"] != 1 or document["kind"] != name or document["result"] != "PASS":
        raise MonitorError("MARKER_INVALID")
    if name == "restore-drill":
        snapshot_id = document["ledger_snapshot_id"]
        if not isinstance(snapshot_id, str) or _TOKEN.fullmatch(snapshot_id) is None:
            raise MonitorError("MARKER_INVALID")
    completed = _parse_timestamp(document["completed_at"])
    if name == "erasure-replica" and (completed - now).total_seconds() > 60:
        raise MonitorError("MARKER_INVALID")
    return max(0.0, (now - completed).total_seconds())


def _disk_stats(path: Path) -> tuple[float, int, bool]:
    statvfs = getattr(os, "statvfs", None)
    if not callable(statvfs):
        raise MonitorError("DISK_STATS_UNAVAILABLE")
    filesystem = statvfs(path)
    total = filesystem.f_blocks * filesystem.f_frsize
    available = filesystem.f_bavail * filesystem.f_frsize
    if total <= 0 or not 0 <= available <= total:
        raise MonitorError("DISK_STAT_INVALID")
    used = total - available
    return used * 100.0 / total, available, used * 100 >= total * 95


def _service_snapshot(
    statuses: dict[str, tuple[str, float]],
) -> tuple[list[str], list[dict[str, str]]]:
    lines: list[str] = []
    alerts: list[dict[str, str]] = []
    for service in _SERVICE_NAMES:
        current = statuses.get(service)
        current_readiness = current[0] if current is not None else None
        heartbeat_age = current[1] if current is not None else None
        lines.extend(
            _metric(
                "tudt_service_instances",
                int(readiness == current_readiness),
                {"service": service, "readiness": readiness},
            )
            for readiness in _READINESS
        )
        lines.append(
            _metric(
                "tudt_service_heartbeat_age_seconds",
                -1 if heartbeat_age is None else round(heartbeat_age, 3),
                {"service": service},
            )
        )
        if current is None:
            alerts.append({"severity": "critical", "code": f"{service.upper()}_MISSING"})
        elif heartbeat_age is not None and heartbeat_age > 30:
            alerts.append({"severity": "critical", "code": f"{service.upper()}_STALE"})
        if current_readiness in {"not_ready", "draining", "stopped"}:
            alerts.append(
                {
                    "severity": "critical",
                    "code": f"{service.upper()}_{current_readiness.upper()}",
                }
            )
        elif current_readiness in {"starting", "degraded"}:
            alerts.append(
                {
                    "severity": "warning",
                    "code": f"{service.upper()}_{current_readiness.upper()}",
                }
            )
    return lines, alerts


def _database_snapshot(  # noqa: PLR0912, PLR0915 - one bounded transaction emits the snapshot
    observed_at: datetime, deployment_id: str
) -> tuple[list[str], list[dict[str, str]]]:
    with _database_connection() as connection:
        status_rows = connection.execute(
            """
            SELECT DISTINCT ON (service_name) service_name, readiness,
                   EXTRACT(EPOCH FROM (%s - last_heartbeat_at))::double precision
            FROM service_instances
            WHERE service_name IN ('app','control','worker')
            ORDER BY service_name, last_heartbeat_at DESC, instance_id DESC
            """,
            (observed_at,),
        ).fetchall()
        statuses = {str(row[0]): (str(row[1]), max(0.0, float(row[2]))) for row in status_rows}
        lines, alerts = _service_snapshot(statuses)
        job_rows = dict(
            connection.execute(
                "SELECT state, count(*) FROM background_jobs GROUP BY state"
            ).fetchall()
        )
        lines.extend(
            _metric("tudt_jobs", int(job_rows.get(state, 0)), {"state": state})
            for state in _JOB_STATES
        )
        queued = connection.execute(
            """
            SELECT count(*), COALESCE(
              EXTRACT(EPOCH FROM (%s - min(created_at)))::double precision, 0
            )
            FROM background_jobs WHERE state IN ('pending','retry_wait')
            """,
            (observed_at,),
        ).fetchone()
        stale_leases = connection.execute(
            "SELECT count(*) FROM background_jobs "
            "WHERE state = 'leased' AND lease_expires_at <= %s",
            (observed_at,),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT count(*), COALESCE(
              EXTRACT(EPOCH FROM (%s - min(created_at)))::double precision, 0
            )
            FROM transactional_outbox WHERE published_at IS NULL
            """,
            (observed_at,),
        ).fetchone()
        send_rows = dict(
            connection.execute(
                "SELECT state, count(*) FROM outbound_delivery_groups "
                "WHERE state IN ('partial','unknown') GROUP BY state"
            ).fetchall()
        )
        send_oldest = connection.execute(
            """
            SELECT COALESCE(
              EXTRACT(EPOCH FROM (%s - min(updated_at)))::double precision, 0
            )
            FROM outbound_delivery_groups WHERE state IN ('partial','unknown')
            """,
            (observed_at,),
        ).fetchone()
        erasure = connection.execute(
            """
            SELECT count(*), COALESCE(
              EXTRACT(EPOCH FROM (%s - min(created_at)))::double precision, 0
            )
            FROM data_erasure_requests WHERE state <> 'completed'
            """,
            (observed_at,),
        ).fetchone()
        export_pending = connection.execute(
            "SELECT count(*) FROM data_export_requests WHERE state IN ('requested','claimed')"
        ).fetchone()
        model_attempt_rows = dict(
            connection.execute(
                "SELECT state, count(*) FROM model_run_attempts "
                "WHERE started_at >= %s - interval '15 minutes' GROUP BY state",
                (observed_at,),
            ).fetchall()
        )
        model_attempt_window = connection.execute(
            """
            SELECT COALESCE(percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at))
                   ) FILTER (WHERE completed_at IS NOT NULL), 0)::double precision,
                   count(*) FILTER (WHERE http_status = 429),
                   count(*) FILTER (WHERE state = 'terminal_failed')
            FROM model_run_attempts WHERE started_at >= %s - interval '15 minutes'
            """,
            (observed_at,),
        ).fetchone()
        model_inflight = connection.execute(
            """
            SELECT count(*), COALESCE(
              EXTRACT(EPOCH FROM (%s - min(started_at)))::double precision, 0
            ) FROM model_run_attempts WHERE state IN ('started','unknown')
            """,
            (observed_at,),
        ).fetchone()
        restore = connection.execute(
            "SELECT gate_state FROM deployment_restore_state WHERE deployment_id = %s",
            (deployment_id,),
        ).fetchone()
        if (
            queued is None
            or stale_leases is None
            or outbox is None
            or send_oldest is None
            or erasure is None
            or export_pending is None
            or model_attempt_window is None
            or model_inflight is None
        ):
            raise MonitorError("DATABASE_AGGREGATE_MISSING")
        queued_count, queue_age = int(queued[0]), max(0.0, float(queued[1]))
        stale_lease_count = int(stale_leases[0])
        outbox_count, outbox_age = int(outbox[0]), max(0.0, float(outbox[1]))
        erasure_count, erasure_age = int(erasure[0]), max(0.0, float(erasure[1]))
        lines.append(_metric("tudt_queue_depth", queued_count))
        lines.append(_metric("tudt_queue_oldest_age_seconds", round(queue_age, 3)))
        lines.append(_metric("tudt_stale_leases", stale_lease_count))
        lines.append(_metric("tudt_outbox_unpublished", outbox_count))
        lines.append(_metric("tudt_outbox_oldest_age_seconds", round(outbox_age, 3)))
        for state in ("partial", "unknown"):
            lines.append(
                _metric(
                    "tudt_send_reconciliation_pending",
                    int(send_rows.get(state, 0)),
                    {"state": state},
                )
            )
        send_age = max(0.0, float(send_oldest[0]))
        lines.append(_metric("tudt_send_reconciliation_oldest_age_seconds", round(send_age, 3)))
        lines.append(_metric("tudt_erasure_pending", erasure_count))
        lines.append(_metric("tudt_erasure_oldest_age_seconds", round(erasure_age, 3)))
        lines.append(_metric("tudt_data_exports_pending", int(export_pending[0])))
        lines.extend(
            _metric(
                "tudt_provider_attempts_15m",
                int(model_attempt_rows.get(state, 0)),
                {"state": state},
            )
            for state in _MODEL_ATTEMPT_STATES
        )
        provider_latency, rate_limited, terminal_failed = (
            max(0.0, float(model_attempt_window[0])),
            int(model_attempt_window[1]),
            int(model_attempt_window[2]),
        )
        provider_inflight, provider_inflight_age = (
            int(model_inflight[0]),
            max(0.0, float(model_inflight[1])),
        )
        lines.append(_metric("tudt_provider_latency_p95_seconds_15m", round(provider_latency, 3)))
        lines.append(_metric("tudt_provider_rate_limited_15m", rate_limited))
        lines.append(_metric("tudt_provider_terminal_failed_15m", terminal_failed))
        lines.append(_metric("tudt_provider_inflight", provider_inflight))
        lines.append(
            _metric("tudt_provider_inflight_oldest_age_seconds", round(provider_inflight_age, 3))
        )
        restore_open = restore is not None and restore[0] == "open"
        lines.append(_metric("tudt_restore_gate_open", int(restore_open)))
        if int(job_rows.get("dead_letter", 0)):
            alerts.append({"severity": "critical", "code": "JOB_DEAD_LETTER"})
        if stale_lease_count:
            alerts.append({"severity": "critical", "code": "JOB_LEASE_STALE"})
        if queue_age > 900:
            alerts.append({"severity": "critical", "code": "QUEUE_AGE_CRITICAL"})
        elif queue_age > 300:
            alerts.append({"severity": "warning", "code": "QUEUE_AGE_WARNING"})
        if outbox_age > 900:
            alerts.append({"severity": "critical", "code": "OUTBOX_AGE_CRITICAL"})
        elif outbox_age > 300:
            alerts.append({"severity": "warning", "code": "OUTBOX_AGE_WARNING"})
        if any(int(send_rows.get(state, 0)) for state in ("partial", "unknown")):
            alerts.append({"severity": "critical", "code": "SEND_RECONCILIATION_PENDING"})
        if erasure_age > 900:
            alerts.append({"severity": "critical", "code": "ERASURE_LAG_CRITICAL"})
        if int(model_attempt_rows.get("unknown", 0)):
            alerts.append({"severity": "critical", "code": "PROVIDER_RESULT_UNKNOWN"})
        if provider_inflight_age > 120:
            alerts.append({"severity": "critical", "code": "PROVIDER_INFLIGHT_STALE"})
        elif provider_inflight_age > 60:
            alerts.append({"severity": "warning", "code": "PROVIDER_INFLIGHT_SLOW"})
        if rate_limited:
            alerts.append({"severity": "warning", "code": "PROVIDER_RATE_LIMITED"})
        if terminal_failed:
            alerts.append({"severity": "warning", "code": "PROVIDER_TERMINAL_FAILURE"})
        if not restore_open:
            alerts.append({"severity": "critical", "code": "RESTORE_GATE_CLOSED"})
    return lines, alerts


def _disk_snapshot(media_root: Path) -> tuple[list[str], list[dict[str, str]]]:
    used_percent, available_bytes, at_95_percent = _disk_stats(media_root)
    lines = [
        _metric("tudt_disk_used_percent", round(used_percent, 3), {"scope": "media-volume"}),
        _metric("tudt_disk_available_bytes", available_bytes, {"scope": "media-volume"}),
    ]
    alerts: list[dict[str, str]] = []
    if at_95_percent:
        alerts.append({"severity": "critical", "code": "DISK_95"})
    elif used_percent >= 90:
        alerts.append({"severity": "critical", "code": "DISK_90"})
    elif used_percent >= 80:
        alerts.append({"severity": "warning", "code": "DISK_80"})
    elif used_percent >= 70:
        alerts.append({"severity": "warning", "code": "DISK_70"})
    if available_bytes < _MIN_OPERATIONAL_FREE_BYTES:
        alerts.append({"severity": "critical", "code": "DISK_FREE_LT_1G"})
    return lines, alerts


def _operation_snapshot(
    marker_root: Path, observed_at: datetime
) -> tuple[list[str], list[dict[str, str]]]:
    lines: list[str] = []
    alerts: list[dict[str, str]] = []
    operations = (
        ("wal-archive", "tudt_wal_archive_check_age_seconds", 15 * 60),
        ("erasure-replica", "tudt_erasure_replica_age_seconds", 15 * 60),
        ("postgres-backup", "tudt_backup_age_seconds", 36 * 3600),
        ("session-backup", "tudt_backup_age_seconds", 36 * 3600),
        ("restore-drill", "tudt_restore_drill_age_seconds", 31 * 24 * 3600),
    )
    for kind, metric_name, maximum_age in operations:
        try:
            age = _backup_age_seconds(marker_root, kind, observed_at)
        except MonitorError, OSError:
            age = None
        lines.append(
            _metric(
                metric_name,
                -1 if age is None else round(age, 3),
                None
                if kind in {"restore-drill", "wal-archive", "erasure-replica"}
                else {"kind": kind},
            )
        )
        if age is None or age > maximum_age:
            alerts.append(
                {"severity": "critical", "code": kind.replace("-", "_").upper() + "_STALE"}
            )
    return lines, alerts


def collect_snapshot(*, now: datetime | None = None) -> tuple[str, tuple[dict[str, str], ...]]:
    """Collect bounded aggregate metrics; individual business identifiers never leave SQL."""

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    deployment_id = os.environ.get("DEPLOYMENT_ID", "")
    if _TOKEN.fullmatch(deployment_id) is None:
        raise MonitorError("DEPLOYMENT_ID_INVALID")
    marker_root = Path(os.environ.get("OPS_STATE_DIR", "/ops-state"))
    media_root = Path(os.environ.get("MEDIA_DATA_DIR", "/media"))
    if marker_root.is_symlink() or not marker_root.is_dir():
        raise MonitorError("OPS_STATE_INVALID")
    if media_root.is_symlink() or not media_root.is_dir():
        raise MonitorError("MEDIA_STATE_INVALID")

    lines = ["# TYPE tudt_monitor_collection_success gauge"]
    alerts: list[dict[str, str]] = []
    try:
        database_lines, database_alerts = _database_snapshot(observed_at, deployment_id)
    except MonitorError, OSError, psycopg.Error, TypeError, ValueError:
        lines.append(_metric("tudt_monitor_collection_success", 0))
        alerts.append({"severity": "critical", "code": "DATABASE_UNAVAILABLE"})
    else:
        lines.extend(database_lines)
        alerts.extend(database_alerts)
        lines.append(_metric("tudt_monitor_collection_success", 1))
    disk_lines, disk_alerts = _disk_snapshot(media_root)
    operation_lines, operation_alerts = _operation_snapshot(marker_root, observed_at)
    lines.extend((*disk_lines, *operation_lines))
    alerts.extend((*disk_alerts, *operation_alerts))
    lines.append("# EOF")
    return "\n".join(lines) + "\n", tuple(sorted(alerts, key=lambda item: item["code"]))


class _Handler(BaseHTTPRequestHandler):
    server_version = "tudt-ops-monitor"
    sys_version = ""

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(HTTPStatus.OK, b'{"status":"alive"}\n', "application/json")
            return
        if self.path not in {"/metrics", "/alerts"}:
            self._send(HTTPStatus.NOT_FOUND, b'{"status":"not_found"}\n', "application/json")
            return
        try:
            metrics, alerts = collect_snapshot()
        except MonitorError, OSError:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b'{"status":"unavailable"}\n',
                "application/json",
            )
            return
        if self.path == "/metrics":
            self._send(HTTPStatus.OK, metrics.encode("ascii"), "text/plain; version=0.0.4")
        else:
            body = (
                json.dumps({"schema_version": 1, "alerts": alerts}, separators=(",", ":")).encode(
                    "ascii"
                )
                + b"\n"
            )
            self._send(HTTPStatus.OK, body, "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_arguments: object) -> None:
        # Client addresses and raw request paths are not copied to application logs.
        return


def _healthcheck() -> int:
    try:
        with urlopen("http://127.0.0.1:9090/health", timeout=2) as response:
            return 0 if response.status == HTTPStatus.OK else 2
    except OSError:
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return _healthcheck()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", 9090), _Handler)  # noqa: S104 - container-only
        server.serve_forever()
    except MonitorError, OSError:
        print("OPS_MONITOR_FAILED:STARTUP", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
