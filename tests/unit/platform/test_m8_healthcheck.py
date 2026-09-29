import io
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health import HealthReason, write_health_snapshot
from telegram_userbot.processes.healthcheck import HEALTH_SNAPSHOT_PATH_ENV, run
from tests.unit.platform.test_m8_health import health_state

NOW = UtcTimestamp(datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC))
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_healthcheck_defaults_to_strict_readiness_and_outputs_one_code(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, health_state())
    stdout = io.StringIO()
    result = run(["--service", "app"], stdout=stdout, snapshot_path=path, now=NOW)
    assert result == 0
    assert stdout.getvalue() == f"{HealthReason.READY.value}\n"


@pytest.mark.unit
def test_healthcheck_does_not_use_liveness_as_readiness(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_health_snapshot(path, replace(health_state(), database_ok=False))
    ready_output = io.StringIO()
    live_output = io.StringIO()
    assert run(["--service", "app"], stdout=ready_output, snapshot_path=path, now=NOW) == 1
    assert (
        run(
            ["--service", "app", "--kind", "live"],
            stdout=live_output,
            snapshot_path=path,
            now=NOW,
        )
        == 0
    )
    assert ready_output.getvalue() == f"{HealthReason.DATABASE_UNAVAILABLE.value}\n"
    assert live_output.getvalue() == f"{HealthReason.LIVE.value}\n"


@pytest.mark.unit
def test_healthcheck_snapshot_failure_outputs_no_path_or_exception_text(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-private-name.json"
    stdout = io.StringIO()
    assert run(["--service", "worker"], stdout=stdout, snapshot_path=path, now=NOW) == 1
    assert stdout.getvalue() == f"{HealthReason.SNAPSHOT_MISSING.value}\n"
    assert str(path) not in stdout.getvalue()


@pytest.mark.unit
def test_module_cli_imports_and_reads_writer_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    current = UtcTimestamp(datetime.now(UTC))
    write_health_snapshot(path, health_state(observed_at=current, heartbeat_at=current))
    environment = os.environ.copy()
    environment[HEALTH_SNAPSHOT_PATH_ENV] = str(path)
    completed = subprocess.run(
        [sys.executable, "-m", "telegram_userbot.processes.healthcheck", "--service", "app"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == f"{HealthReason.READY.value}\n"
    assert completed.stderr == ""
