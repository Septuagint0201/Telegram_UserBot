"""Strict host entrypoint for bounded systemd Operations jobs."""

from __future__ import annotations

import argparse
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
_APPROVAL = Path("/run/telegram-userbot/session-backup-approved")
_ACTIONS = frozenset(
    {"postgres-full", "postgres-diff", "wal-check", "session", "data-export", "erasure-ledger"}
)
_LOCAL_DOCKER_HOST = "unix:///run/docker.sock"


class MaintenanceError(RuntimeError):
    pass


def _fail(code: str) -> int:
    print(f"MAINTENANCE_JOB_FAILED:{code}", file=sys.stderr)
    return 2


def _regular_file(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise MaintenanceError("INPUT_FILE_INVALID") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
        raise MaintenanceError("INPUT_FILE_INVALID")
    return resolved


def _consume_session_approval(project_name: str, approval_file: Path) -> bool:
    try:
        info = approval_file.lstat()
        value = approval_file.read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError) as error:
        raise MaintenanceError("SESSION_MAINTENANCE_APPROVAL_INVALID") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (sys.platform != "win32" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o400))
        or value != f"SESSION_BACKUP_APPROVED:{project_name}\n"
    ):
        raise MaintenanceError("SESSION_MAINTENANCE_APPROVAL_INVALID")
    try:
        approval_file.unlink()
    except OSError as error:
        raise MaintenanceError("SESSION_MAINTENANCE_APPROVAL_CONSUME_FAILED") from error
    return True


def _run(command: Sequence[str], *, timeout: int) -> None:
    try:
        subprocess.run(  # noqa: S603 - Docker path and every argument are fixed or validated
            list(command),
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MaintenanceError("DOCKER_COMMAND_FAILED") from error


def run_job(  # noqa: PLR0913 - systemd passes every host boundary explicitly
    action: str,
    *,
    compose_file: Path,
    env_file: Path,
    deployment_config: Path,
    project_name: str,
    approval_file: Path = _APPROVAL,
) -> bool:
    if action not in _ACTIONS:
        raise MaintenanceError("ACTION_INVALID")
    if _PROJECT.fullmatch(project_name) is None or project_name.startswith("tudt-restore-"):
        raise MaintenanceError("PROJECT_NAME_INVALID")
    compose = _regular_file(compose_file)
    environment = _regular_file(env_file)
    deployment = _regular_file(deployment_config)
    docker = shutil.which("docker")
    if docker is None:
        raise MaintenanceError("DOCKER_UNAVAILABLE")
    if action == "session":
        if not _consume_session_approval(project_name, approval_file):
            return False
        session_runner = _regular_file(Path(__file__).with_name("run_session_backup.py"))
        _run(
            (
                sys.executable,
                str(session_runner),
                "--compose-file",
                str(compose),
                "--env-file",
                str(environment),
                "--deployment-config",
                str(deployment),
                "--project-name",
                project_name,
            ),
            timeout=1800,
        )
        return True

    prefix = [
        docker,
        "--host",
        _LOCAL_DOCKER_HOST,
        "compose",
        "--env-file",
        str(environment),
        "--project-name",
        project_name,
        "--file",
        str(compose),
    ]
    commands: dict[str, tuple[tuple[str, ...], int]] = {
        "postgres-full": (
            ("exec", "-T", "postgres", "/usr/local/bin/tudt-pgbackrest-backup", "full"),
            7200,
        ),
        "postgres-diff": (
            ("exec", "-T", "postgres", "/usr/local/bin/tudt-pgbackrest-backup", "diff"),
            7200,
        ),
        "wal-check": (
            ("exec", "-T", "postgres", "/usr/local/bin/tudt-pgbackrest-wal-check"),
            300,
        ),
        "data-export": (
            (
                "--profile",
                "ops",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "data-export",
            ),
            1800,
        ),
        "erasure-ledger": (
            (
                "--profile",
                "ops",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "erasure-ledger-export",
            ),
            300,
        ),
    }
    arguments, timeout = commands[action]
    _run((*prefix, *arguments), timeout=timeout)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=sorted(_ACTIONS))
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--deployment-config", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    args = parser.parse_args(argv)
    try:
        executed = run_job(
            args.action,
            compose_file=args.compose_file,
            env_file=args.env_file,
            deployment_config=args.deployment_config,
            project_name=args.project_name,
        )
    except MaintenanceError as error:
        code = str(error)
        return _fail(code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "JOB_FAILED")
    outcome = "COMPLETE" if executed else "SKIPPED_NO_APPROVAL"
    print(f"MAINTENANCE_JOB_{outcome}:{args.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
