"""Copy the cumulative erasure ledger to an independent encrypted restic repository."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
import restore_gate

_HEX = re.compile(r"^[0-9a-f]{64}$")
_LIMIT = 16 * 1024 * 1024
_TAG = "telegram-userbot-erasure-v1"
_LOCAL_REPO = "/local-erasure-repository"


def _environment() -> dict[str, str]:
    repository = os.environ.get("ERASURE_RESTIC_REPOSITORY", "")
    local = repository == _LOCAL_REPO and os.environ.get("TUDT_LOCAL_BACKUP_VALIDATION") == (
        "explicit-local-synthetic-only"
    )
    if (not local and not repository.startswith("s3:https://")) or any(
        char in repository for char in "\r\n\x00"
    ):
        raise ValueError("REPOSITORY_INVALID")
    if not local:
        url = urlsplit(repository.removeprefix("s3:"))
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("REPOSITORY_INVALID")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",  # noqa: S108 - sealed container tmpfs
        "RESTIC_REPOSITORY": repository,
        "RESTIC_PASSWORD": restore_gate._read_secret(
            Path("/run/secrets/erasure_restic_password"), maximum=256
        ).decode("ascii"),
    }
    if not local:
        for variable, filename in (
            ("AWS_ACCESS_KEY_ID", "erasure_s3_access_key"),
            ("AWS_SECRET_ACCESS_KEY", "erasure_s3_secret_key"),
        ):
            environment[variable] = restore_gate._read_secret(
                Path("/run/secrets") / filename, minimum=16, maximum=256
            ).decode("ascii")
    return environment


def _restic(
    arguments: Sequence[str], environment: dict[str, str], payload: bytes | None = None
) -> bytes:
    # Never send tool stderr (which can contain credentials/paths) to the journal.
    # Output is bounded by the container's tmpfs as well as this parser limit.
    with tempfile.TemporaryFile() as output:
        subprocess.run(  # noqa: S603 - fixed image binary; no shell
            ["/usr/bin/restic", "--no-cache", *arguments],
            input=payload,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=240,
            check=True,
        )
        output.seek(0)
        raw = output.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("REPLICA_OUTPUT_TOO_LARGE")
    return raw


def _snapshot(environment: dict[str, str], tag: str) -> str | None:
    snapshots = json.loads(
        _restic(["snapshots", "--json", "--tag", tag, "--latest", "1"], environment)
    )
    if not isinstance(snapshots, list) or len(snapshots) > 1:
        raise ValueError("REPLICA_SNAPSHOT_INVALID")
    if not snapshots:
        return None
    if not isinstance(snapshots[0], dict):
        raise TypeError("REPLICA_SNAPSHOT_INVALID")
    snapshot = snapshots[0].get("id")
    if not isinstance(snapshot, str) or _HEX.fullmatch(snapshot) is None:
        raise ValueError("REPLICA_SNAPSHOT_INVALID")
    return snapshot


def _document(
    raw: bytes, deployment_id: str, account_id: UUID, secret: bytes
) -> tuple[dict[str, Any], dict[object, restore_gate.LedgerEntry]]:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "ledger.jsonl"
        path.write_bytes(raw)
        _, entries = restore_gate._load_ledger(
            path,
            expected_digest=hashlib.sha256(raw).hexdigest(),
            deployment_id=deployment_id,
            account_id=account_id,
            scope_secret=secret,
            supported_scopes=frozenset({"memory", "contact", "account"}),
        )
    header = json.loads(raw.splitlines()[0])
    exported = datetime.fromisoformat(header["exported_at"])
    if exported.tzinfo is None or exported > datetime.now(UTC) + timedelta(seconds=60):
        raise ValueError("REPLICA_TIMESTAMP_INVALID")
    return header, {entry["request_id"]: entry for entry in entries}


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    fcntl = importlib.import_module("fcntl")  # Linux operations image only

    nofollow = getattr(os, "O_NOFOLLOW")  # noqa: B009 - absent on Windows type stubs
    descriptor = os.open(root / ".replica.lock", os.O_CREAT | os.O_RDWR | nofollow, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _marker(root: Path, exported_at: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("OPS_STATE_INVALID")
    info = root.stat()
    if info.st_uid != 0 or info.st_gid != 21016 or stat.S_IMODE(info.st_mode) != 0o2770:
        raise ValueError("OPS_STATE_PERMISSION_INVALID")
    restore_gate._write_ledger_document(
        root / "erasure-replica.json",
        json.dumps(
            {
                "schema_version": 1,
                "kind": "erasure-replica",
                "result": "PASS",
                # Freshness describes the exported DB state, never a retry's upload time.
                "completed_at": exported_at,
            }
        ).encode("ascii"),
    )


def backup(*, config: Path, ledger: Path, marker_root: Path) -> dict[str, str]:
    deployment, account, _ = restore_gate._load_identity(config)
    environment = _environment()
    secret = restore_gate._read_secret(Path("/run/secrets/erasure_hmac_key"))
    with _lock(ledger.parent):
        previous = _snapshot(environment, f"{_TAG},{deployment}")
        # A missing/inaccessible repository must fail; initialization is an explicit operation.
        restore_gate.export_ledger(
            deployment_id=deployment,
            account_id=account,
            output_path=ledger,
            snapshot_id="ledger-" + uuid4().hex,
        )
        raw = restore_gate._read_file(ledger, minimum=1, maximum=_LIMIT)
        header, entries = _document(raw, deployment, account, secret)
        if previous is not None:
            old_raw = _restic(["dump", previous, "/ledger.jsonl"], environment)
            old_header, old_entries = _document(old_raw, deployment, account, secret)
            if any(entries.get(key) != value for key, value in old_entries.items()) or (
                datetime.fromisoformat(header["exported_at"])
                < datetime.fromisoformat(old_header["exported_at"])
            ):
                raise ValueError("REPLICA_HISTORY_REGRESSED")
        _restic(
            [
                "backup",
                "--stdin",
                "--stdin-filename",
                "ledger.jsonl",
                "--json",
                "--host",
                deployment,
                "--tag",
                _TAG,
                "--tag",
                deployment,
                "--tag",
                header["snapshot_id"],
            ],
            environment,
            raw,
        )
        snapshot = _snapshot(environment, f"{_TAG},{deployment},{header['snapshot_id']}")
        if snapshot is None:
            raise ValueError("REPLICA_SNAPSHOT_MISSING")
        recovered = _restic(["dump", snapshot, "/ledger.jsonl"], environment)
        if recovered != raw:
            raise ValueError("REPLICA_READBACK_MISMATCH")
        receipt = {
            "snapshot_id": snapshot,
            "ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "exported_at": header["exported_at"],
            "deployment_id": deployment,
        }
        restore_gate._write_ledger_document(
            ledger.with_suffix(".receipt.json"), json.dumps(receipt).encode("ascii")
        )
        _marker(marker_root, header["exported_at"])
    return receipt


def recover(*, config: Path, output: Path, snapshot: str, digest: str) -> None:
    if _HEX.fullmatch(snapshot) is None or _HEX.fullmatch(digest) is None:
        raise ValueError("REPLICA_ID_INVALID")
    if output.exists() or output.is_symlink():
        raise ValueError("REPLICA_TARGET_EXISTS")
    deployment, account, _ = restore_gate._load_identity(config)
    raw = _restic(["dump", snapshot, "/ledger.jsonl"], _environment())
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("LEDGER_DIGEST_MISMATCH")
    _document(
        raw, deployment, account, restore_gate._read_secret(Path("/run/secrets/erasure_hmac_key"))
    )
    restore_gate._write_ledger_document(output, raw)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--backup", action="store_true")
    operation.add_argument("--recover", action="store_true")
    operation.add_argument("--init-repository", action="store_true")
    parser.add_argument(
        "--deployment-config",
        type=Path,
        default=Path("/etc/telegram-userbot/config/deployment.json"),
    )
    parser.add_argument("--ledger", type=Path, default=Path("/erasure-ledger/ledger.jsonl"))
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--sha256", default="")
    args = parser.parse_args(argv)
    try:
        if args.init_repository:
            # Explicit provisioning only. Ordinary backups never recreate a lost repository.
            restore_gate._load_identity(args.deployment_config)
            _restic(["init"], _environment())
            print("ERASURE_REPLICA_INITIALIZED")
        elif args.backup:
            receipt = backup(
                config=args.deployment_config, ledger=args.ledger, marker_root=Path("/ops-state")
            )
            print(json.dumps(receipt, sort_keys=True))
        else:
            recover(
                config=args.deployment_config,
                output=args.ledger,
                snapshot=args.snapshot,
                digest=args.sha256,
            )
            print("ERASURE_REPLICA_RECOVERED")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        psycopg.Error,
        subprocess.SubprocessError,
    ) as error:
        code = str(error) if isinstance(error, ValueError) else "OPERATION_FAILED"
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
            code = "OPERATION_FAILED"
        print(f"ERASURE_REPLICA_FAILED:{code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
