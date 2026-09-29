from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUEST_ID = UUID("01900000-0000-7000-8000-000000000201")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000202")
OWNER_ID = UUID("01900000-0000-7000-8000-000000000203")


def _load_export_script() -> Any:
    path = ROOT / "deploy" / "ops" / "data_export.py"
    spec = importlib.util.spec_from_file_location("m8_test_data_export_leases", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Transaction:
    def __enter__(self) -> _Transaction:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Result:
    def __init__(
        self,
        *,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> dict[str, object] | None:
        return self._row

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(self, results: list[_Result] | None = None) -> None:
        self.results = results or []
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.closed = False

    def transaction(self) -> _Transaction:
        return _Transaction()

    def execute(self, query: str, parameters: tuple[object, ...] | None = None) -> _Result:
        self.calls.append((query, parameters))
        return self.results.pop(0) if self.results else _Result()

    def close(self) -> None:
        self.closed = True


@pytest.mark.unit
def test_lease_renewal_advances_fence_only_for_the_current_claim_owner() -> None:
    module = _load_export_script()
    connection = _Connection([_Result(row={"version": 8})])

    assert (
        module._renew(
            connection,
            request_id=REQUEST_ID,
            owner_id=OWNER_ID,
            expected_version=7,
        )
        == 8
    )

    query, parameters = connection.calls[0]
    normalized = " ".join(query.split())
    assert "SET lease_expires_at = %s, version = version + 1" in normalized
    assert "state = 'claimed'" in normalized
    assert "owner_instance_id = %s" in normalized
    assert "version = %s" in normalized
    assert "lease_expires_at > %s" in normalized
    assert "expires_at > %s" in normalized
    assert parameters is not None
    assert tuple(parameters)[3] == 7


@pytest.mark.unit
def test_finalization_uses_the_latest_renewed_fencing_version() -> None:
    module = _load_export_script()
    first = _Connection([_Result(row={"version": 8})])
    second = _Connection([_Result(row={"version": 9})])
    connections = iter((first, second))

    guard = module._ExportLeaseGuard(
        request_id=REQUEST_ID,
        owner_id=OWNER_ID,
        expected_version=7,
        connection_factory=lambda: next(connections),
    )

    assert guard._renew_once()
    assert guard.expected_version == 8
    assert guard.finalize_version() == 9
    assert first.closed
    assert second.closed


@pytest.mark.unit
def test_reclaimed_export_aborts_and_cleans_only_its_attempt_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_export_script()
    main_connection = _Connection()
    encrypted_artifact: Path | None = None
    encrypted_attempt_count: int | None = None

    def lease_connection() -> _Connection:
        return _Connection()

    def no_finalize(*_args: object, **_kwargs: object) -> bool:
        pytest.fail("finalize must not run after lease loss")

    def no_record_failure(**_kwargs: object) -> bool:
        pytest.fail("lost claim must not be marked failed")

    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setattr(module, "_validated_staging", lambda _path: tmp_path)
    monkeypatch.setattr(module, "_validated_recipient", lambda _path: tmp_path / "recipient.txt")
    monkeypatch.setattr(module, "_database_connection", lambda **_kwargs: main_connection)
    monkeypatch.setattr(module, "_lease_connection", lease_connection)
    monkeypatch.setattr(
        module,
        "_claim",
        lambda _connection, _request_id, _owner_id: {
            "id": REQUEST_ID,
            "account_id": ACCOUNT_ID,
            "contact_id": None,
            "attempt_count": 2,
            "version": 7,
        },
    )
    renewals = iter((8, None))
    monkeypatch.setattr(module, "_renew", lambda *_args, **_kwargs: next(renewals))
    monkeypatch.setattr(
        module,
        "_iter_export_rows",
        lambda _connection, **_kwargs: iter((b"row\n",)),
    )

    def encrypt(**kwargs: object) -> tuple[Path, bytes]:
        nonlocal encrypted_artifact, encrypted_attempt_count
        temporary, artifact = module._artifact_paths(
            request_id=kwargs["request_id"],
            attempt_count=kwargs["attempt_count"],
            staging=kwargs["staging"],
        )
        assert not temporary.exists()
        artifact.write_bytes(b"encrypted-attempt-two")
        encrypted_artifact = artifact
        encrypted_attempt_count = cast(int, kwargs["attempt_count"])
        return artifact, hashlib.sha256(artifact.read_bytes()).digest()

    monkeypatch.setattr(module, "_encrypt_export", encrypt)
    monkeypatch.setattr(module, "_finalize", no_finalize)
    monkeypatch.setattr(module, "_record_failure", no_record_failure)

    with pytest.raises(module.ExportLeaseLostError, match="EXPORT_LEASE_LOST"):
        module.run(REQUEST_ID)

    assert encrypted_attempt_count == 2
    assert encrypted_artifact is not None
    assert not encrypted_artifact.exists()
    assert main_connection.closed


@pytest.mark.unit
def test_cleanup_due_removes_only_the_completed_attempt_specific_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_export_script()
    _temporary, artifact = module._artifact_paths(
        request_id=REQUEST_ID,
        attempt_count=3,
        staging=tmp_path,
    )
    artifact.write_bytes(b"encrypted-attempt-three")
    digest = hashlib.sha256(artifact.read_bytes()).digest()
    legacy_path = tmp_path / f"{REQUEST_ID}.jsonl.age"
    legacy_path.write_bytes(b"not-this-attempt")
    connection = _Connection(
        [
            _Result(
                rows=[
                    {
                        "id": REQUEST_ID,
                        "artifact_sha256": digest,
                        "attempt_count": 3,
                        "version": 11,
                    }
                ]
            ),
            _Result(row={"id": REQUEST_ID}),
        ]
    )

    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setattr(module, "_validated_staging", lambda _path: tmp_path)
    monkeypatch.setattr(module, "_database_connection", lambda **_kwargs: connection)

    assert module.cleanup_due() == 1
    assert not artifact.exists()
    assert legacy_path.exists()
    assert connection.closed
    assert "attempt_count" in connection.calls[0][0]


@pytest.mark.unit
def test_cleanup_due_removes_stale_temporary_without_following_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_export_script()
    temporary, _artifact = module._artifact_paths(
        request_id=REQUEST_ID,
        attempt_count=4,
        staging=tmp_path,
    )
    temporary.write_bytes(b"encrypted-partial")
    stale_at = time.time() - 7_200 - 1
    os.utime(temporary, (stale_at, stale_at))
    connection = _Connection([_Result(rows=[])])

    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setattr(module, "_validated_staging", lambda _path: tmp_path)
    monkeypatch.setattr(module, "_database_connection", lambda **_kwargs: connection)

    assert module.cleanup_due() == 0
    assert not temporary.exists()
    assert connection.closed


@pytest.mark.unit
def test_cleanup_due_rejects_stale_temporary_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_export_script()
    temporary, _artifact = module._artifact_paths(
        request_id=REQUEST_ID,
        attempt_count=5,
        staging=tmp_path,
    )
    outside = tmp_path / "outside"
    outside.write_bytes(b"must-survive")
    temporary.symlink_to(outside)
    connection = _Connection([_Result(rows=[])])

    monkeypatch.setenv("DEPLOYMENT_ID", "production-primary")
    monkeypatch.setattr(module, "_validated_staging", lambda _path: tmp_path)
    monkeypatch.setattr(module, "_database_connection", lambda **_kwargs: connection)

    with pytest.raises(ValueError, match="ARTIFACT_INVALID"):
        module.cleanup_due()
    assert outside.read_bytes() == b"must-survive"
    assert connection.closed


@pytest.mark.unit
def test_cleanup_artifact_does_not_follow_a_replaced_symlink(tmp_path: Path) -> None:
    if not (
        os.name == "posix"
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    ):
        pytest.skip("directory-fd no-follow operations are unavailable")
    module = _load_export_script()
    outside = tmp_path / "outside"
    outside.write_bytes(b"must-survive")
    artifact = tmp_path / f"{REQUEST_ID}.1.jsonl.age"
    artifact.symlink_to(outside)

    with pytest.raises(ValueError, match="ARTIFACT_INVALID"):
        module._delete_artifact(artifact, expected_digest=b"x" * 32)

    assert outside.read_bytes() == b"must-survive"
