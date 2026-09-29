"""Host-side Session backup gate.

This script is intentionally run by a trusted deployment operator with Docker
authority.  It never mounts the Docker socket into an application container.  The
gate requires the app container to be stopped, then holds the exact PostgreSQL
advisory lock used by the Telethon Session owner for the full one-shot backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

_DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
_LOCK_NAMESPACE = "telegram-userbot:ownership:v1"
_LOCAL_DOCKER_HOST = "unix:///run/docker.sock"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DEPLOYMENT_CONFIG_INVALID")
        result[key] = value
    return result


def _fail(code: str) -> int:
    print(f"SESSION_BACKUP_GATE_FAILED:{code}", file=sys.stderr)
    return 2


def session_lock_key(*, deployment_id: str, telegram_user_id: int) -> int:
    if _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
        raise ValueError("DEPLOYMENT_ID_INVALID")
    if type(telegram_user_id) is not int or not 0 < telegram_user_id < (1 << 63):
        raise ValueError("TELEGRAM_USER_ID_INVALID")
    material = f"{_LOCK_NAMESPACE}:telegram_session:{deployment_id}:{telegram_user_id}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


def _deployment_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _load_identity(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("DEPLOYMENT_CONFIG_INVALID")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        deployment_id = _deployment_id(document["deployment_id"])
        telegram_user_id = document["runtime_identity"]["telegram_user_id"]
        lock_key = session_lock_key(
            deployment_id=deployment_id,
            telegram_user_id=telegram_user_id,
        )
    except KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError:
        raise ValueError("DEPLOYMENT_CONFIG_INVALID") from None
    return deployment_id, lock_key


def _compose_prefix(
    docker: str, compose_file: Path, env_file: Path, project_name: str
) -> list[str]:
    if _PROJECT_NAME.fullmatch(project_name) is None:
        raise ValueError("PROJECT_NAME_INVALID")
    if compose_file.is_symlink() or not compose_file.is_file():
        raise ValueError("COMPOSE_FILE_INVALID")
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("ENV_FILE_INVALID")
    return [
        docker,
        "--host",
        _LOCAL_DOCKER_HOST,
        "compose",
        "--env-file",
        str(env_file.resolve()),
        "-p",
        project_name,
        "-f",
        str(compose_file.resolve()),
    ]


def _app_is_stopped(prefix: Sequence[str]) -> bool:
    result = subprocess.run(  # noqa: S603 - reviewed fixed command and validated prefix
        [*prefix, "ps", "--status", "running", "-q", "app"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return not result.stdout.strip()


def _open_lock_session(prefix: Sequence[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - reviewed fixed command and validated prefix
        [
            *prefix,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "telegram_userbot",
            "-Atq",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _write_line(stream: IO[str] | None, value: str) -> None:
    if stream is None:
        raise RuntimeError("LOCK_SESSION_INVALID")
    stream.write(value + "\n")
    stream.flush()


def _hold_session_lock(process: subprocess.Popen[str], lock_key: int) -> None:
    _write_line(
        process.stdin,
        "SELECT CASE WHEN pg_try_advisory_lock("
        f"{lock_key}) THEN 'LOCK_ACQUIRED' ELSE 'LOCK_CONTENDED' END;",
    )
    if process.stdout is None or process.stdout.readline().strip() != "LOCK_ACQUIRED":
        raise RuntimeError("SESSION_OWNER_NOT_RELEASED")


def _release_session_lock(process: subprocess.Popen[str], lock_key: int) -> None:
    try:
        if process.poll() is None:
            _write_line(process.stdin, f"SELECT pg_advisory_unlock({lock_key});")
            _write_line(process.stdin, "\\q")
            process.communicate(timeout=15)
    except BrokenPipeError, OSError, subprocess.SubprocessError:
        process.kill()
        process.communicate()


def run_backup(
    *, compose_file: Path, env_file: Path, deployment_config: Path, project_name: str
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("DOCKER_UNAVAILABLE")
    _deployment_id, lock_key = _load_identity(deployment_config)
    prefix = _compose_prefix(docker, compose_file, env_file, project_name)
    if not _app_is_stopped(prefix):
        raise RuntimeError("APP_STILL_RUNNING")

    lock_process = _open_lock_session(prefix)
    try:
        _hold_session_lock(lock_process, lock_key)
        subprocess.run(  # noqa: S603 - exact reviewed Compose service
            [
                *prefix,
                "--profile",
                "ops",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "session-backup",
            ],
            check=True,
            timeout=1800,
        )
    finally:
        _release_session_lock(lock_process, lock_key)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--deployment-config", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    args = parser.parse_args(argv)
    try:
        run_backup(
            compose_file=args.compose_file,
            env_file=args.env_file,
            deployment_config=args.deployment_config,
            project_name=args.project_name,
        )
    except ValueError as error:
        code = str(error)
        return _fail(code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "INPUT_INVALID")
    except OSError, RuntimeError, subprocess.SubprocessError:
        return _fail("OWNER_GATE_OR_BACKUP_FAILED")
    print("SESSION_BACKUP_GATE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
