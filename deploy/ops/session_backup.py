"""Validate a stopped Telethon SQLite Session and create an encrypted restic snapshot."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SESSION_ROOT = Path("/session")
SESSION_FILENAME = "account.session"
SESSION_PATH = SESSION_ROOT / SESSION_FILENAME
_RESTIC = "/usr/bin/restic"
EXPECTED_TABLES = frozenset({"entities", "sent_files", "sessions", "update_state", "version"})
OPS_STATE_ROOT = Path("/ops-state")
OPS_STATE_GID = 21016


def fail(code: str) -> int:
    print(f"SESSION_BACKUP_FAILED:{code}", file=sys.stderr)
    return 2


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SECRET_INVALID")
    value = path.read_bytes()
    if not 16 <= len(value) <= 256 or b"\x00" in value or b"\n" in value or b"\r" in value:
        raise ValueError("SECRET_INVALID")
    return value.decode("ascii")


def _session_file() -> Path:
    candidates = tuple(SESSION_ROOT.glob("*.session"))
    if candidates != (SESSION_PATH,):
        raise ValueError("SESSION_INVENTORY_INVALID")
    session = SESSION_PATH
    if session.is_symlink() or not session.is_file() or session.parent.resolve() != SESSION_ROOT:
        raise ValueError("SESSION_FILE_INVALID")
    return session


def _integrity_check(session: Path) -> None:
    connection = sqlite3.connect(f"file:{session.as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("SESSION_INTEGRITY_FAILED")
        tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
        if not tables >= EXPECTED_TABLES:
            raise ValueError("SESSION_SCHEMA_INVALID")
    finally:
        connection.close()


def _write_success_marker() -> None:
    if OPS_STATE_ROOT.is_symlink() or not OPS_STATE_ROOT.is_dir():
        raise ValueError("OPS_STATE_INVALID")
    info = OPS_STATE_ROOT.stat()
    if (
        info.st_uid != 0
        or info.st_gid != OPS_STATE_GID
        or info.st_mode & 0o007
        or not info.st_mode & stat.S_IWGRP
        or not info.st_mode & stat.S_ISGID
    ):
        raise ValueError("OPS_STATE_PERMISSION_INVALID")
    final = OPS_STATE_ROOT / "session-backup.json"
    if final.is_symlink():
        raise ValueError("OPS_STATE_INVALID")
    payload = (
        '{"schema_version":1,"kind":"session-backup","completed_at":"'
        + datetime.now(UTC).isoformat().replace("+00:00", "Z")
        + '","result":"PASS"}\n'
    ).encode("ascii")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=OPS_STATE_ROOT,
            prefix=".session-backup.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o640)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(final)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        session = _session_file()
        _integrity_check(session)
        repository = os.environ["RESTIC_REPOSITORY"]
        local_validation = (
            repository == "/local-restic-repository"
            and os.environ.get("TUDT_LOCAL_BACKUP_VALIDATION") == "explicit-local-synthetic-only"
        )
        if (not local_validation and not repository.startswith("s3:https://")) or any(
            ch in repository for ch in "\r\n\x00"
        ):
            raise ValueError("REPOSITORY_INVALID")  # noqa: TRY301
        child_environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",  # noqa: S108 - sealed job sandbox
            "RESTIC_REPOSITORY": repository,
            "RESTIC_PASSWORD": _read_secret(Path("/run/secrets/session_restic_password")),
        }
        if not local_validation:
            child_environment.update(
                AWS_ACCESS_KEY_ID=_read_secret(Path("/run/secrets/session_s3_access_key")),
                AWS_SECRET_ACCESS_KEY=_read_secret(Path("/run/secrets/session_s3_secret_key")),
            )
        # ``subprocess.run`` has overloads for each text/binary combination.  The
        # shared invocation is intentionally all-binary (stdio is redirected), but
        # keeping this small map typed as ``Any`` lets mypy select that overload
        # across the three reviewed restic calls without weakening the command
        # arguments themselves.
        common: dict[str, Any] = {
            "env": child_environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if local_validation:
            initialized = subprocess.run(  # noqa: S603 - sealed image binary
                [_RESTIC, "snapshots", "--json"],
                check=False,
                **common,
            )
            if initialized.returncode != 0:
                subprocess.run(  # noqa: S603 - sealed image binary
                    [_RESTIC, "init"],
                    check=True,
                    **common,
                )
        subprocess.run(  # noqa: S603 - fixed binary and arguments in a sealed image
            [_RESTIC, "backup", "--tag", "telegram-userbot-session-v1", SESSION_ROOT.as_posix()],
            check=True,
            **common,
        )
        subprocess.run(  # noqa: S603 - sealed image binary
            [_RESTIC, "check", "--read-data-subset=1/100"],
            check=True,
            **common,
        )
        subprocess.run(  # noqa: S603 - sealed image binary
            [
                _RESTIC,
                "forget",
                "--tag",
                "telegram-userbot-session-v1",
                "--keep-within",
                "7d",
                "--prune",
            ],
            check=True,
            **common,
        )
        _write_success_marker()
    except (
        KeyError,
        OSError,
        UnicodeError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        code = str(error) if isinstance(error, ValueError) else "OPERATION_FAILED"
        return fail(code if code.isupper() and len(code) <= 64 else "OPERATION_FAILED")
    print("SESSION_BACKUP_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
