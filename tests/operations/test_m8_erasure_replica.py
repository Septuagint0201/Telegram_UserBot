"""Independent ledger copy must preserve history and prove the recovered bytes."""

import hashlib
import json
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.operations.test_m8_restore_and_systemd import ROOT, _load_script

pytestmark = pytest.mark.unit


def _replica(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, bytes, Path]:
    monkeypatch.syspath_prepend(str(ROOT / "deploy/ops"))
    module = _load_script("erasure_replica")
    account = uuid4()
    secret = b"s" * 32
    payload: bytes = module.restore_gate._ledger_document(
        deployment_id="replica-test",
        account_id=account,
        scope_secret=secret,
        snapshot_id="ledger-test",
        rows=[],
    )
    monkeypatch.setattr(
        module.restore_gate, "_load_identity", lambda _: ("replica-test", account, "head")
    )
    monkeypatch.setattr(module.restore_gate, "_read_secret", lambda *a, **k: secret)
    monkeypatch.setattr(module, "_environment", dict)
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(
        module.restore_gate, "export_ledger", lambda **k: k["output_path"].write_bytes(payload)
    )
    return module, payload, tmp_path / "ledger.jsonl"


def test_backup_reads_back_exact_snapshot_before_publishing_receipt_and_freshness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, payload, ledger = _replica(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    markers: list[str] = []
    snapshots = iter([None, "a" * 64])
    monkeypatch.setattr(module, "_snapshot", lambda *a: next(snapshots))
    monkeypatch.setattr(module, "_marker", lambda root, stamp: markers.append(stamp))

    def restic(args: list[str], env: object, raw: bytes | None = None) -> bytes:
        calls.append(args)
        if args[0] == "backup":
            assert raw == payload
            return b"{}"
        assert args == ["dump", "a" * 64, "/ledger.jsonl"]
        return payload

    monkeypatch.setattr(module, "_restic", restic)
    receipt = module.backup(config=tmp_path / "config", ledger=ledger, marker_root=tmp_path)
    assert receipt["ledger_sha256"] == hashlib.sha256(payload).hexdigest()
    assert markers == [json.loads(payload.splitlines()[0])["exported_at"]]
    assert json.loads(ledger.with_suffix(".receipt.json").read_bytes()) == receipt
    assert [call[0] for call in calls] == ["backup", "dump"]


@pytest.mark.parametrize("failure", ["upload", "readback", "missing", "history"])
def test_failed_copy_preserves_previous_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    module, payload, ledger = _replica(monkeypatch, tmp_path)
    receipt = ledger.with_suffix(".receipt.json")
    receipt.write_bytes(b"previous-receipt")
    snapshots = iter(
        ["b" * 64 if failure == "history" else None, None if failure == "missing" else "a" * 64]
    )
    monkeypatch.setattr(module, "_snapshot", lambda *a: next(snapshots))
    monkeypatch.setattr(module, "_marker", lambda *a: pytest.fail("must not refresh marker"))
    if failure == "history":
        original = module._document

        def document(raw: bytes, *args: Any) -> Any:
            header, entries = original(payload, *args)
            return header, {"previous": {"request_id": "previous"}} if raw == b"old" else entries

        monkeypatch.setattr(module, "_document", document)

    def restic(args: list[str], env: object, raw: bytes | None = None) -> bytes:
        if args[0] == "backup" and failure == "upload":
            raise subprocess.CalledProcessError(1, "restic")
        return b"old" if failure == "history" else b"corrupt"

    monkeypatch.setattr(module, "_restic", restic)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        module.backup(config=tmp_path / "config", ledger=ledger, marker_root=tmp_path)
    assert receipt.read_bytes() == b"previous-receipt"


@pytest.mark.parametrize(
    ("snapshot", "digest"), [("latest", "a" * 64), ("a" * 8, "b" * 64), ("a" * 64, "bad")]
)
def test_recovery_requires_exact_snapshot_and_independent_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    snapshot: str,
    digest: str,
) -> None:
    module, _, ledger = _replica(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module, "_restic", lambda *a: pytest.fail("invalid input must not reach storage")
    )
    with pytest.raises(ValueError, match="REPLICA_ID_INVALID"):
        module.recover(config=tmp_path / "config", output=ledger, snapshot=snapshot, digest=digest)
    assert not ledger.exists()


def test_recovery_checks_digest_identity_and_empty_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, payload, ledger = _replica(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_restic", lambda *a: payload)
    with pytest.raises(ValueError, match="LEDGER_DIGEST_MISMATCH"):
        module.recover(
            config=tmp_path / "config", output=ledger, snapshot="a" * 64, digest="b" * 64
        )
    assert not ledger.exists()
    digest = hashlib.sha256(payload).hexdigest()
    module.recover(config=tmp_path / "config", output=ledger, snapshot="a" * 64, digest=digest)
    assert ledger.read_bytes() == payload
    with pytest.raises(ValueError, match="REPLICA_TARGET_EXISTS"):
        module.recover(config=tmp_path / "config", output=ledger, snapshot="a" * 64, digest=digest)


def test_monitor_alarms_on_missing_or_stale_replica(tmp_path: Path) -> None:
    module = _load_script("monitor")
    now = datetime.now(UTC)
    _, alerts = module._operation_snapshot(tmp_path, now)
    assert {"severity": "critical", "code": "ERASURE_REPLICA_STALE"} in alerts
    marker = {
        "schema_version": 1,
        "kind": "erasure-replica",
        "result": "PASS",
        "completed_at": (now - timedelta(minutes=16)).isoformat(),
    }
    path = tmp_path / "erasure-replica.json"
    path.write_text(json.dumps(marker))
    lines, alerts = module._operation_snapshot(tmp_path, now)
    assert "tudt_erasure_replica_age_seconds 960.0" in lines
    assert {"severity": "critical", "code": "ERASURE_REPLICA_STALE"} in alerts
    marker["completed_at"] = now.isoformat()
    path.write_text(json.dumps(marker))
    _, alerts = module._operation_snapshot(tmp_path, now)
    assert not any(alert["code"] == "ERASURE_REPLICA_STALE" for alert in alerts)
    marker["completed_at"] = (now + timedelta(minutes=2)).isoformat()
    path.write_text(json.dumps(marker))
    _, alerts = module._operation_snapshot(tmp_path, now)
    assert any(alert["code"] == "ERASURE_REPLICA_STALE" for alert in alerts)


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "/local-erasure-repository",
        "s3:http://example.invalid/ledger",
        "s3:https://user:secret@example.invalid/ledger",
        "s3:https://example.invalid/ledger?token=x",
        "s3:https://example.invalid/ledger\n",
    ],
)
def test_repository_rejects_unacknowledged_local_or_inline_credentials(
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "deploy/ops"))
    module = _load_script("erasure_replica")
    monkeypatch.setenv("ERASURE_RESTIC_REPOSITORY", repository)
    monkeypatch.delenv("TUDT_LOCAL_BACKUP_VALIDATION", raising=False)
    with pytest.raises(ValueError, match="REPOSITORY_INVALID"):
        module._environment()


def test_recovery_rejects_other_account_even_with_matching_file_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, payload, ledger = _replica(monkeypatch, tmp_path)
    header = json.loads(payload.splitlines()[0])
    header["account_scope_hmac"] = "0" * 64
    other = json.dumps(header).encode() + b"\n"
    monkeypatch.setattr(module, "_restic", lambda *a: other)
    with pytest.raises(ValueError, match="LEDGER_HEADER_INVALID"):
        module.recover(
            config=tmp_path / "config",
            output=ledger,
            snapshot="a" * 64,
            digest=hashlib.sha256(other).hexdigest(),
        )
    assert not ledger.exists()
