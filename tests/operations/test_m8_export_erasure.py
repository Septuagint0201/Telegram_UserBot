"""Filesystem evidence for revoked exports, including crashed and live writers."""

import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from tests.operations.test_m8_data_export_leases import (
    ACCOUNT_ID,
    REQUEST_ID,
    ROOT,
    _Connection,
    _load_export_script,
    _Result,
)


def _row() -> dict[str, object]:
    return {"id": REQUEST_ID, "attempt_count": 3, "version": 9, "artifact_sha256": None}


@pytest.mark.unit
def test_maintenance_cleans_before_claiming_and_stops_on_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_export_script()
    events: list[str] = []

    def cleanup() -> int:
        events.append("clean")
        return 2

    def run(_request: UUID | None) -> bool:
        events.append("claim")
        return False

    monkeypatch.setattr(module, "cleanup_due", cleanup)
    monkeypatch.setattr(module, "run", run)
    assert module.main(["--maintenance"]) == 0
    assert events == ["clean", "claim"]

    def fail() -> int:
        raise ValueError("ARTIFACT_DIGEST_MISMATCH")

    monkeypatch.setattr(module, "cleanup_due", fail)
    events.clear()
    assert module.main(["--maintenance"]) != 0
    assert events == []


@pytest.mark.unit
def test_erased_export_removes_all_attempts_and_orphans_only_in_its_scope(tmp_path: Path) -> None:
    module = _load_export_script()
    names = [
        f"{REQUEST_ID}.1.jsonl.age",
        f".{REQUEST_ID}.2.jsonl.age.tmp",
        f"{REQUEST_ID}.3.jsonl.age",
        f"{REQUEST_ID}.jsonl.age",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"encrypted-payload")
    other = tmp_path / f"{ACCOUNT_ID}.1.jsonl.age"
    other.write_bytes(b"keep")
    row = _row() | {"artifact_sha256": hashlib.sha256(b"encrypted-payload").digest()}
    for _ in range(2):
        connection = _Connection([_Result(rows=[row]), _Result(row={"id": REQUEST_ID})])
        assert module._cleanup_erased(connection, tmp_path, datetime.now(UTC)) == 1
        assert all(not (tmp_path / name).exists() for name in names)
    assert other.read_bytes() == b"keep"


@pytest.mark.unit
def test_live_writer_blocks_erasure_and_stale_temp_cleanup(tmp_path: Path) -> None:
    module = _load_export_script()
    path = tmp_path / f".{REQUEST_ID}.3.jsonl.age.tmp"
    path.write_bytes(b"encrypted-live-stream")
    os.utime(path, (1, 1))
    lock = module._ArtifactLock(tmp_path, REQUEST_ID)
    assert lock.acquire()
    try:
        connection = _Connection([_Result(rows=[_row()])])
        assert module._cleanup_erased(connection, tmp_path, datetime.now(UTC)) == 0
        module._cleanup_stale_locked(path, cutoff=float("inf"), directory_fd=None)
        assert path.exists()
        assert len(connection.calls) == 1  # No durable deletion acknowledgment.
    finally:
        lock.close()
    connection = _Connection([_Result(rows=[_row()]), _Result(row={"id": REQUEST_ID})])
    assert module._cleanup_erased(connection, tmp_path, datetime.now(UTC)) == 1
    assert not path.exists()


@pytest.mark.unit
@pytest.mark.parametrize("corruption", ["symlink", "digest"])
def test_erasure_never_acknowledges_an_unverified_artifact(tmp_path: Path, corruption: str) -> None:
    module = _load_export_script()
    outside = tmp_path / "outside.age"
    outside.write_bytes(b"unrelated")
    target = tmp_path / f"{REQUEST_ID}.3.jsonl.age"
    if corruption == "symlink":
        target.symlink_to(outside)
    else:
        target.write_bytes(b"unexpected")
    row = _row() | {"artifact_sha256": b"d" * 32}
    connection = _Connection([_Result(rows=[row])])
    with pytest.raises(ValueError, match="ARTIFACT_"):
        module._cleanup_erased(connection, tmp_path, datetime.now(UTC))
    assert len(connection.calls) == 1
    assert outside.read_bytes() == b"unrelated"
    assert target.exists()


@pytest.mark.unit
def test_revocation_between_claim_and_lock_does_not_start_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_export_script()
    connection = _Connection()
    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setattr(module, "_validated_staging", lambda _path: tmp_path)
    monkeypatch.setattr(module, "_validated_recipient", lambda _path: tmp_path / "recipient")
    monkeypatch.setattr(module, "_database_connection", lambda **_kw: connection)
    monkeypatch.setattr(module, "_lease_connection", _Connection)
    monkeypatch.setattr(
        module, "_claim", lambda *_args: _row() | {"account_id": ACCOUNT_ID, "contact_id": None}
    )
    monkeypatch.setattr(module, "_renew", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module, "_encrypt_export", lambda **_kwargs: pytest.fail("revoked writer ran")
    )
    with pytest.raises(module.ExportLeaseLostError):
        module.run(REQUEST_ID)
    assert not list(tmp_path.glob("*.age*"))
    lock = module._ArtifactLock(tmp_path, REQUEST_ID)
    assert lock.acquire()
    lock.close()


@pytest.mark.unit
def test_request_lock_is_exclusive_between_processes_and_released_after_crash(
    tmp_path: Path,
) -> None:
    module = _load_export_script()
    script = """
import sys, time
from pathlib import Path
from uuid import UUID
from tests.operations.test_m8_data_export_leases import _load_export_script
lock = _load_export_script()._ArtifactLock(Path(sys.argv[1]), UUID(sys.argv[2]))
assert lock.acquire()
print('locked', flush=True)
time.sleep(30)
"""
    process = subprocess.Popen(  # noqa: S603 - fixed synthetic child with argv-only temp path
        [sys.executable, "-c", script, str(tmp_path), str(REQUEST_ID)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        contender = module._ArtifactLock(tmp_path, REQUEST_ID)
        assert not contender.acquire()
    finally:
        process.terminate()
        process.communicate(timeout=10)
    assert contender.acquire()
    contender.close()
