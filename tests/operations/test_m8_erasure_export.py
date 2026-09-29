"""Cumulative ledger compatibility and failure preservation."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.operations.test_m8_restore_and_systemd import _load_script


@pytest.mark.unit
def test_repeated_target_keeps_each_request_and_rejects_duplicate_request_key(
    tmp_path: Path,
) -> None:
    module = _load_script("restore_gate")
    account_id = uuid4()
    key = b"s" * 32
    row = {
        "account_scope_hmac": module.hmac.new(key, account_id.bytes, "sha256").digest(),
        "target_scope_hmac": b"t" * 32,
        "request_id": uuid4(),
        "request_idempotency_key": b"i" * 32,
        "scope_type": "memory",
        "policy_version": 1,
        "completed_at": datetime(2030, 1, 1, tzinfo=UTC),
    }
    rows = [row, {**row, "request_id": uuid4(), "request_idempotency_key": b"j" * 32}]
    payload = module._ledger_document(
        deployment_id="test-primary",
        account_id=account_id,
        scope_secret=key,
        snapshot_id="ledger-repeat",
        rows=rows,
    )
    path = tmp_path / "ledger.jsonl"
    module._write_ledger_document(path, payload)
    _, entries = module._load_ledger(
        path,
        expected_digest=module.hashlib.sha256(payload).hexdigest(),
        deployment_id="test-primary",
        account_id=account_id,
        scope_secret=key,
    )
    assert len(entries) == 2
    rows[1]["request_idempotency_key"] = row["request_idempotency_key"]
    with pytest.raises(ValueError, match="LEDGER_EXPORT_ROW_INVALID"):
        module._ledger_document(
            deployment_id="test-primary",
            account_id=account_id,
            scope_secret=key,
            snapshot_id="ledger-repeat",
            rows=rows,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_scope_hmac", b"x" * 32),
        ("target_scope_hmac", b"short"),
        ("request_id", "invalid"),
        ("request_idempotency_key", b"short"),
        ("scope_type", "unknown"),
        ("policy_version", True),
        ("policy_version", 2**31),
        ("completed_at", datetime(2030, 1, 1)),  # noqa: DTZ001 - invalid fixture
    ],
)
def test_export_rejects_malformed_or_wrong_account_rows(field: str, value: object) -> None:
    module = _load_script("restore_gate")
    account_id = uuid4()
    key = b"s" * 32
    row = {
        "account_scope_hmac": module.hmac.new(key, account_id.bytes, "sha256").digest(),
        "target_scope_hmac": b"t" * 32,
        "request_id": uuid4(),
        "request_idempotency_key": b"i" * 32,
        "scope_type": "memory",
        "policy_version": 1,
        "completed_at": datetime(2030, 1, 1, tzinfo=UTC),
    }
    row[field] = value
    with pytest.raises(ValueError, match="LEDGER_EXPORT_ROW_INVALID"):
        module._ledger_document(
            deployment_id="test-primary",
            account_id=account_id,
            scope_secret=key,
            snapshot_id="ledger-invalid",
            rows=[row],
        )


@pytest.mark.unit
def test_failed_atomic_replace_preserves_previous_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("restore_gate")
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b"previous-ledger")

    def fail_replace(*_args: Any) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        module._write_ledger_document(path, b"next-ledger")
    assert path.read_bytes() == b"previous-ledger"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.unit
def test_export_connection_requires_read_only_export_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("restore_gate")
    for name, value in {
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "telegram_userbot",
        "DATABASE_USER": "telegram_userbot_exporter_login",
        "DATABASE_RUNTIME_ROLE": "telegram_userbot_export_runtime",
        "DATABASE_PASSWORD_FILE": "/run/secrets/export_database_password",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "_read_secret", lambda _path: b"synthetic-test-only")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(module.psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    module._export_connection()
    assert len(calls) == 1
    assert "default_transaction_read_only=on" in calls[0]["options"]
    assert "telegram_userbot_migrator" not in str(calls)
    monkeypatch.setenv("DATABASE_USER", "telegram_userbot_migrator_login")
    with pytest.raises(ValueError, match="DATABASE_CONFIG_INVALID"):
        module._export_connection()
    assert len(calls) == 1


@pytest.mark.unit
def test_maintenance_runs_ledger_service_with_bounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("run_maintenance")
    files = [tmp_path / name for name in ("compose.yaml", "compose.env", "deployment.json")]
    for path in files:
        path.write_text("synthetic", encoding="ascii")
    calls: list[tuple[tuple[str, ...], int]] = []
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        module, "_run", lambda command, *, timeout: calls.append((command, timeout))
    )
    assert module.run_job(
        "erasure-ledger",
        compose_file=files[0],
        env_file=files[1],
        deployment_config=files[2],
        project_name="test-primary",
    )
    command, timeout = calls[0]
    assert command[-8:] == (
        "--profile",
        "ops",
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "erasure-ledger-export",
    )
    assert timeout == 300
