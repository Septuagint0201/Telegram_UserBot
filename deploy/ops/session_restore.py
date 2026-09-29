"""Restore one exact restic Telethon Session snapshot into an empty volume."""

from __future__ import annotations

import os
import re
import shutil
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
OPS_STATE_ROOT = Path("/ops-state")
EXPECTED_TABLES = frozenset({"entities", "sent_files", "sessions", "update_state", "version"})
OPS_STATE_GID = 21016
_SNAPSHOT = re.compile(r"^[0-9a-f]{8,64}$")


def _fail(code: str) -> int:
    print(f"SESSION_RESTORE_FAILED:{code}", file=sys.stderr)
    return 2


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SECRET_INVALID")
    value = path.read_bytes()
    if not 16 <= len(value) <= 256 or any(byte in value for byte in (0, 10, 13)):
        raise ValueError("SECRET_INVALID")
    return value.decode("ascii")


def _validate_target() -> None:
    if SESSION_ROOT.is_symlink() or not SESSION_ROOT.is_dir():
        raise ValueError("SESSION_TARGET_INVALID")
    if any(SESSION_ROOT.iterdir()):
        raise ValueError("SESSION_TARGET_NOT_EMPTY")


def _restored_session(root: Path) -> Path:
    candidates = tuple(root.glob("*.session"))
    session = root / SESSION_FILENAME
    if candidates != (session,):
        raise ValueError("RESTORED_SESSION_INVENTORY_INVALID")
    return session


def _validate_session(session: Path) -> None:
    if session.is_symlink() or not session.is_file() or session.stat().st_size <= 0:
        raise ValueError("SESSION_FILE_INVALID")
    connection = sqlite3.connect(f"file:{session.as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SESSION_INTEGRITY_FAILED")
        tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
        if not tables >= EXPECTED_TABLES:
            raise ValueError("SESSION_SCHEMA_INVALID")
        sessions = connection.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions"
        ).fetchall()
        if (
            len(sessions) != 1
            or type(sessions[0][0]) is not int
            or not isinstance(sessions[0][1], str)
            or type(sessions[0][2]) is not int
            or not isinstance(sessions[0][3], bytes)
            or len(sessions[0][3]) < 128
        ):
            raise ValueError("SESSION_AUTH_MATERIAL_INVALID")
    finally:
        connection.close()


def _write_marker(snapshot_id: str) -> None:
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
    payload = (
        '{"schema_version":1,"kind":"session-restore","completed_at":"'
        + datetime.now(UTC).isoformat().replace("+00:00", "Z")
        + f'","result":"PASS","snapshot_id":"{snapshot_id}"}}\n'
    ).encode("ascii")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=OPS_STATE_ROOT,
            prefix=".session-restore.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o640)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(OPS_STATE_ROOT / "session-restore.json")
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        _validate_target()
        snapshot_id = os.environ.get("RESTIC_SNAPSHOT_ID", "")
        if _SNAPSHOT.fullmatch(snapshot_id) is None:
            raise ValueError("SNAPSHOT_ID_INVALID")  # noqa: TRY301
        repository = os.environ.get("RESTIC_REPOSITORY", "")
        local_validation = (
            repository == "/local-restic-repository"
            and os.environ.get("TUDT_LOCAL_BACKUP_VALIDATION") == "explicit-local-synthetic-only"
        )
        if (not local_validation and not repository.startswith("s3:https://")) or any(
            character in repository for character in "\r\n\x00"
        ):
            raise ValueError("REPOSITORY_INVALID")  # noqa: TRY301
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",  # noqa: S108 - sealed job sandbox
            "RESTIC_REPOSITORY": repository,
            "RESTIC_PASSWORD": _read_secret(Path("/run/secrets/session_restic_password")),
        }
        if not local_validation:
            environment.update(
                AWS_ACCESS_KEY_ID=_read_secret(Path("/run/secrets/session_s3_access_key")),
                AWS_SECRET_ACCESS_KEY=_read_secret(Path("/run/secrets/session_s3_secret_key")),
            )
        # See the corresponding backup helper: all stdio is binary/redirected,
        # and this shared keyword map is only used with fixed restic arguments.
        common: dict[str, Any] = {
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 900,
        }
        subprocess.run([_RESTIC, "check"], check=True, **common)  # noqa: S603 - sealed image binary
        with tempfile.TemporaryDirectory(prefix="session-restore-", dir="/tmp") as root:
            destination = Path(root)
            subprocess.run(  # noqa: S603 - exact reviewed restic invocation
                [
                    _RESTIC,
                    "restore",
                    snapshot_id,
                    "--tag",
                    "telegram-userbot-session-v1",
                    "--target",
                    str(destination),
                ],
                check=True,
                **common,
            )
            restored_session = _restored_session(destination / "session")
            _validate_session(restored_session)
            with restored_session.open("rb") as source, SESSION_PATH.open("xb") as output:
                SESSION_PATH.chmod(0o600)
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            _validate_session(SESSION_PATH)
        _write_marker(snapshot_id)
    except OSError, UnicodeError, ValueError, sqlite3.Error, subprocess.SubprocessError:
        for candidate in SESSION_ROOT.glob("*.session") if SESSION_ROOT.is_dir() else ():
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        return _fail("OPERATION_FAILED")
    print("SESSION_RESTORE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
