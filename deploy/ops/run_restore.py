"""Host-side fail-closed restore orchestration for a new isolated Compose project."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

_PROJECT = re.compile(r"^tudt-restore-[a-z0-9][a-z0-9-]{2,39}$")
_PGBACKREST_SET = re.compile(r"^[0-9]{8}-[0-9]{6}F(?:_[0-9]{8}-[0-9]{6}[DI])?$")
_RESTIC_SNAPSHOT = re.compile(r"^[0-9a-f]{8,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEPLOYMENT = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_OBJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_OPS_STATE_GID = 21016
_LOCAL_VALIDATION_ACK = "I_ACKNOWLEDGE_LOCAL_SYNTHETIC_RESTORE_IS_NOT_OFF_HOST_EVIDENCE"
_LOCAL_DOCKER_HOST = "unix:///run/docker.sock"
_LOCAL_VALIDATION_VOLUMES = (
    "pgbackrest-validation-repository",
    "restic-validation-repository",
)
_IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_ZERO_DIGEST_SUFFIX = "@sha256:" + "0" * 64
_ARTIFACT_NAMES = frozenset(
    {
        "application",
        "database",
        "gateway",
        "redis",
        "session-backup",
        "data-export",
    }
)
_RENDERED_SERVICE_ARTIFACTS = {
    "https-gateway": "gateway",
    "app": "application",
    "control": "application",
    "worker": "application",
    "postgres": "database",
    "redis": "redis",
    "migrate": "application",
    "session-backup": "session-backup",
    "data-export": "data-export",
    "ops-monitor": "session-backup",
    "erasure-ledger-export": "session-backup",
    "postgres-restore": "database",
    "session-restore": "session-backup",
    "restore-gate-close": "session-backup",
    "restore-gate-open": "session-backup",
}
_COMPOSE_CONFIG_ARGS: tuple[str, ...] = (
    "--profile",
    "ops",
    "--profile",
    "restore",
    "config",
    "--format",
    "json",
)
_MAX_RENDERED_COMPOSE_BYTES = 1 * 1024 * 1024
_RESTORE_GATE_CLOSE_ARGS: tuple[str, ...] = (
    "--profile",
    "restore",
    "run",
    "--rm",
    "--no-deps",
    "--pull",
    "never",
    "restore-gate-close",
)


class RestoreOrchestrationError(RuntimeError):
    """Stable content-free host orchestration failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> int:
    print(f"RESTORE_ORCHESTRATION_FAILED:{code}", file=sys.stderr)
    return 2


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RestoreOrchestrationError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _regular_file(path: Path, *, maximum: int = _MAX_INPUT_BYTES) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as error:
        raise RestoreOrchestrationError("INPUT_FILE_INVALID") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RestoreOrchestrationError("INPUT_FILE_INVALID")
    if not 0 < info.st_size <= maximum:
        raise RestoreOrchestrationError("INPUT_FILE_INVALID")
    return resolved


def _require_root_runtime() -> None:
    if os.name != "posix":
        raise RestoreOrchestrationError("PLATFORM_UNSUPPORTED")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RestoreOrchestrationError("ROOT_REQUIRED")


def _root_owned_input(
    path: Path,
    *,
    maximum: int,
    exact_mode: int | None = None,
    forbid_other_access: bool = False,
) -> Path:
    resolved = _regular_file(path, maximum=maximum)
    if os.name != "posix":
        return resolved
    try:
        info = resolved.stat()
    except OSError as error:
        raise RestoreOrchestrationError("INPUT_FILE_PERMISSION_INVALID") from error
    mode = stat.S_IMODE(info.st_mode)
    if (
        info.st_uid != 0
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
        or (forbid_other_access and mode & 0o007)
    ):
        raise RestoreOrchestrationError("INPUT_FILE_PERMISSION_INVALID")
    return resolved


def _load_deployment_contract(path: Path) -> tuple[str, dict[str, str]]:
    try:
        document = json.loads(
            _regular_file(path, maximum=1_048_576).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
        deployment_id = document["deployment_id"]
        restore_required = document["startup_policy"]["restore_gate_required"]
        deployable = document["deployable"]
        images = document["images"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise RestoreOrchestrationError("DEPLOYMENT_CONFIG_INVALID") from error
    if (
        not isinstance(deployment_id, str)
        or _DEPLOYMENT.fullmatch(deployment_id) is None
        or restore_required is not True
        or deployable is not True
        or not isinstance(images, Mapping)
        or set(images) != _ARTIFACT_NAMES
    ):
        raise RestoreOrchestrationError("DEPLOYMENT_CONFIG_INVALID")
    references: dict[str, str] = {}
    for artifact_name in sorted(_ARTIFACT_NAMES):
        item = images.get(artifact_name)
        if (
            not isinstance(item, Mapping)
            or set(item) != {"status", "reference"}
            or item.get("status") != "BUILT"
            or not isinstance(item.get("reference"), str)
            or _IMAGE_REFERENCE.fullmatch(item["reference"]) is None
            or item["reference"].endswith(_ZERO_DIGEST_SUFFIX)
        ):
            raise RestoreOrchestrationError("DEPLOYMENT_CONFIG_INVALID")
        references[artifact_name] = item["reference"]
    if references["session-backup"] != references["data-export"]:
        raise RestoreOrchestrationError("DEPLOYMENT_CONFIG_INVALID")
    return deployment_id, references


def _load_deployment(path: Path) -> str:
    """Load the deployment identity while retaining the historical helper API."""

    return _load_deployment_contract(path)[0]


def _validate_rendered_compose_images(
    rendered: object,
    *,
    deployment_images: Mapping[str, str],
) -> None:
    """Require every base and restore service to use the reviewed digest reference."""

    if not isinstance(rendered, Mapping):
        raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID")
    services = rendered.get("services")
    if not isinstance(services, Mapping) or set(services) != set(_RENDERED_SERVICE_ARTIFACTS):
        raise RestoreOrchestrationError("COMPOSE_SERVICE_INVENTORY_MISMATCH")
    if set(deployment_images) != _ARTIFACT_NAMES:
        raise RestoreOrchestrationError("DEPLOYMENT_CONFIG_INVALID")
    for service_name, artifact_name in _RENDERED_SERVICE_ARTIFACTS.items():
        service = services.get(service_name)
        if not isinstance(service, Mapping):
            raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID")
        image = service.get("image")
        if not isinstance(image, str) or _IMAGE_REFERENCE.fullmatch(image) is None:
            raise RestoreOrchestrationError("COMPOSE_IMAGE_INVALID")
        if image != deployment_images[artifact_name]:
            raise RestoreOrchestrationError("COMPOSE_IMAGE_MISMATCH")


def _validate_rendered_compose(
    prefix: Sequence[str],
    *,
    environment: Mapping[str, str],
    deployment_images: Mapping[str, str],
) -> None:
    result = _run(
        [*prefix, *_COMPOSE_CONFIG_ARGS],
        environment=environment,
        timeout=60,
    )
    output = result.stdout
    if not isinstance(output, str):
        raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID")
    try:
        output_size = len(output.encode("utf-8"))
    except UnicodeError as error:
        raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID") from error
    if not output or output_size > _MAX_RENDERED_COMPOSE_BYTES or "\x00" in output:
        raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID")
    try:
        rendered = json.loads(output, object_pairs_hook=_unique_object)
    except RestoreOrchestrationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RestoreOrchestrationError("COMPOSE_CONFIG_INVALID") from error
    _validate_rendered_compose_images(rendered, deployment_images=deployment_images)


def _declared_project_objects(
    compose_file: Path,
    project_name: str,
    *,
    extra_volume_names: Sequence[str] = (),
) -> tuple[tuple[str, str], ...]:
    try:
        document = json.loads(
            _regular_file(compose_file, maximum=2_097_152).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise RestoreOrchestrationError("COMPOSE_FILE_INVALID") from error
    objects: list[tuple[str, str]] = []
    for section in ("volumes", "networks"):
        values = document.get(section)
        if not isinstance(values, Mapping):
            raise RestoreOrchestrationError("COMPOSE_FILE_INVALID")
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or _OBJECT.fullmatch(key) is None
                or not isinstance(value, Mapping)
                or "name" in value
                or value.get("external") is True
            ):
                raise RestoreOrchestrationError("COMPOSE_OBJECT_UNSAFE")
            kind = "volume" if section == "volumes" else "network"
            objects.append((kind, f"{project_name}_{key}"))
    for name in extra_volume_names:
        if not isinstance(name, str) or _OBJECT.fullmatch(name) is None:
            raise RestoreOrchestrationError("COMPOSE_OBJECT_UNSAFE")
        objects.append(("volume", f"{project_name}_{name}"))
    if len(set(objects)) != len(objects):
        raise RestoreOrchestrationError("COMPOSE_OBJECT_UNSAFE")
    return tuple(sorted(objects))


def _docker_command(docker: str, *arguments: str) -> list[str]:
    return [docker, "--host", _LOCAL_DOCKER_HOST, *arguments]


def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed Docker executable and validated arguments
            list(command),
            env=dict(environment),
            check=check,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RestoreOrchestrationError("DOCKER_COMMAND_FAILED") from error


def _assert_new_target(
    docker: str,
    *,
    project_name: str,
    objects: Sequence[tuple[str, str]],
    environment: Mapping[str, str],
) -> None:
    containers = _run(
        _docker_command(
            docker,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ),
        environment=environment,
        timeout=30,
    )
    if containers.stdout.strip():
        raise RestoreOrchestrationError("TARGET_PROJECT_EXISTS")
    for kind, name in objects:
        inspected = _run(
            _docker_command(docker, kind, "inspect", name),
            environment=environment,
            timeout=30,
            check=False,
        )
        if inspected.returncode == 0:
            raise RestoreOrchestrationError("TARGET_OBJECT_EXISTS")
        if inspected.returncode != 1:
            raise RestoreOrchestrationError("TARGET_INSPECTION_FAILED")


def _validate_evidence_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as error:
        raise RestoreOrchestrationError("EVIDENCE_DIRECTORY_INVALID") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RestoreOrchestrationError("EVIDENCE_DIRECTORY_INVALID")
    if os.name == "posix" and (
        info.st_uid != 0 or info.st_gid != 21016 or stat.S_IMODE(info.st_mode) != 0o2770
    ):
        raise RestoreOrchestrationError("EVIDENCE_DIRECTORY_PERMISSION_INVALID")
    return resolved


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _chown(path: Path, uid: int, gid: int) -> None:
    """Apply the POSIX ownership contract while remaining type-checkable on Windows."""

    chown = getattr(os, "chown", None)
    if chown is None:
        raise RestoreOrchestrationError("EVIDENCE_OWNERSHIP_UNAVAILABLE")
    chown(path, uid, gid)


def _reserve_evidence_run(directory: Path, project_name: str) -> Path:
    inventory = directory / "restore-orchestrations"
    try:
        inventory.mkdir(mode=0o2750, exist_ok=True)
        if os.name == "posix":
            _chown(inventory, 0, _OPS_STATE_GID)
            inventory.chmod(0o2750)
        inventory_info = inventory.lstat()
    except OSError as error:
        raise RestoreOrchestrationError("EVIDENCE_RESERVATION_FAILED") from error
    if stat.S_ISLNK(inventory_info.st_mode) or not stat.S_ISDIR(inventory_info.st_mode):
        raise RestoreOrchestrationError("EVIDENCE_RESERVATION_FAILED")
    if os.name == "posix" and (
        inventory_info.st_uid != 0
        or inventory_info.st_gid != _OPS_STATE_GID
        or stat.S_IMODE(inventory_info.st_mode) != 0o2750
    ):
        raise RestoreOrchestrationError("EVIDENCE_RESERVATION_FAILED")

    run = inventory / project_name
    try:
        run.mkdir(mode=0o2750, exist_ok=False)
        if os.name == "posix":
            _chown(run, 0, _OPS_STATE_GID)
            run.chmod(0o2750)
        _fsync_directory(inventory)
    except FileExistsError as error:
        raise RestoreOrchestrationError("EVIDENCE_PROJECT_ALREADY_RECORDED") from error
    except OSError as error:
        raise RestoreOrchestrationError("EVIDENCE_RESERVATION_FAILED") from error
    return run


def _write_exclusive_json(directory: Path, filename: str, payload: object) -> tuple[Path, str]:
    temporary: Path | None = None
    final = directory / filename
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=directory,
            prefix=".restore-orchestration.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o640)
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, final)
        temporary.unlink()
        _fsync_directory(directory)
    except FileExistsError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RestoreOrchestrationError("EVIDENCE_FILE_ALREADY_EXISTS") from error
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RestoreOrchestrationError("EVIDENCE_WRITE_FAILED") from error
    return final, _sha256(final)


def _write_stage(  # noqa: PLR0913 - immutable audit identity is explicit
    directory: Path,
    *,
    deployment_id: str,
    project_name: str,
    evidence_scope: str,
    sequence: int,
    stage: str,
    result: str,
    code: str,
    previous_sha256: str,
) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "kind": "restore-orchestration",
        "deployment_id": deployment_id,
        "project_name": project_name,
        "evidence_scope": evidence_scope,
        "sequence": sequence,
        "stage": stage,
        "result": result,
        "code": code,
        "previous_sha256": previous_sha256,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    filename = f"{sequence:02d}-{stage}.json"
    _path, digest = _write_exclusive_json(directory, filename, payload)
    return {
        "sequence": sequence,
        "stage": stage,
        "result": result,
        "path": filename,
        "sha256": digest,
    }


def _write_final_manifest(  # noqa: PLR0913 - immutable evidence identity is explicit
    directory: Path,
    *,
    deployment_id: str,
    project_name: str,
    evidence_scope: str,
    result: str,
    stages: Sequence[Mapping[str, object]],
) -> str:
    if not stages:
        raise RestoreOrchestrationError("EVIDENCE_INVENTORY_EMPTY")
    payload = {
        "schema_version": 1,
        "kind": "restore-orchestration-manifest",
        "deployment_id": deployment_id,
        "project_name": project_name,
        "evidence_scope": evidence_scope,
        "result": result,
        "stage_count": len(stages),
        "chain_head_sha256": stages[-1]["sha256"],
        "stages": list(stages),
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _path, digest = _write_exclusive_json(directory, "manifest.json", payload)
    _write_exclusive_text(directory, "manifest.sha256", f"{digest}  manifest.json\n")
    return digest


def _write_exclusive_text(directory: Path, filename: str, value: str) -> None:
    temporary: Path | None = None
    final = directory / filename
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=directory,
            prefix=".restore-orchestration.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o640)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, final)
        temporary.unlink()
        _fsync_directory(directory)
    except FileExistsError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RestoreOrchestrationError("EVIDENCE_FILE_ALREADY_EXISTS") from error
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RestoreOrchestrationError("EVIDENCE_WRITE_FAILED") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compose_prefix(  # noqa: PLR0913 - every Compose input remains explicit
    docker: str,
    *,
    base: Path,
    overlay: Path,
    env_file: Path,
    project_name: str,
    validation_overlay: Path | None,
) -> list[str]:
    prefix = [
        docker,
        "--host",
        _LOCAL_DOCKER_HOST,
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project_name,
        "--file",
        str(base),
        "--file",
        str(overlay),
    ]
    if validation_overlay is not None:
        prefix.extend(("--file", str(validation_overlay)))
    return prefix


def _verify_runtime_set(prefix: Sequence[str], *, environment: Mapping[str, str]) -> None:
    result = _run(
        [*prefix, "ps", "--services", "--status", "running"],
        environment=environment,
        timeout=30,
    )
    running = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if len(running) != 2 or set(running) != {"postgres", "redis"}:
        raise RestoreOrchestrationError("RESTORE_RUNTIME_SET_INVALID")


def _reclose_gate_best_effort(prefix: Sequence[str], *, environment: Mapping[str, str]) -> bool:
    """Return the gate to ``validating`` if the post-open runtime check fails."""

    try:
        _run(
            [*prefix, *_RESTORE_GATE_CLOSE_ARGS],
            environment=environment,
            timeout=300,
        )
    except RestoreOrchestrationError:
        return False
    return True


def run_restore(  # noqa: PLR0912, PLR0913, PLR0915 - destructive boundaries remain explicit
    *,
    compose_file: Path,
    restore_overlay: Path,
    env_file: Path,
    deployment_config: Path,
    project_name: str,
    confirmation: str,
    postgres_backup_set: str,
    session_snapshot_id: str,
    erasure_ledger: Path,
    erasure_ledger_sha256: str,
    evidence_directory: Path,
    validation_overlay: Path | None = None,
    local_validation_ack: str | None = None,
) -> None:
    _require_root_runtime()
    if _PROJECT.fullmatch(project_name) is None or confirmation != project_name:
        raise RestoreOrchestrationError("NEW_PROJECT_CONFIRMATION_REQUIRED")
    if _PGBACKREST_SET.fullmatch(postgres_backup_set) is None:
        raise RestoreOrchestrationError("POSTGRES_BACKUP_SET_INVALID")
    if _RESTIC_SNAPSHOT.fullmatch(session_snapshot_id) is None:
        raise RestoreOrchestrationError("SESSION_SNAPSHOT_ID_INVALID")
    if _SHA256.fullmatch(erasure_ledger_sha256) is None:
        raise RestoreOrchestrationError("LEDGER_DIGEST_INVALID")

    docker = shutil.which("docker")
    if docker is None:
        raise RestoreOrchestrationError("DOCKER_UNAVAILABLE")
    base = _root_owned_input(compose_file, maximum=2_097_152)
    overlay = _root_owned_input(restore_overlay, maximum=2_097_152)
    environment_file = _root_owned_input(env_file, maximum=1_048_576, exact_mode=0o600)
    deployment = _root_owned_input(deployment_config, maximum=1_048_576)
    ledger = _root_owned_input(
        erasure_ledger,
        maximum=_MAX_INPUT_BYTES,
        forbid_other_access=True,
    )
    local_overlay: Path | None = None
    evidence_scope = "production-off-host"
    if validation_overlay is not None:
        if local_validation_ack != _LOCAL_VALIDATION_ACK:
            raise RestoreOrchestrationError("LOCAL_VALIDATION_ACK_REQUIRED")
        local_overlay = _root_owned_input(validation_overlay, maximum=2_097_152)
        evidence_scope = "local-synthetic-only"
    elif local_validation_ack is not None:
        raise RestoreOrchestrationError("LOCAL_VALIDATION_OVERLAY_REQUIRED")
    evidence = _validate_evidence_directory(evidence_directory)
    deployment_id, deployment_images = _load_deployment_contract(deployment)
    if not hmac.compare_digest(_sha256(ledger), erasure_ledger_sha256):
        raise RestoreOrchestrationError("LEDGER_DIGEST_MISMATCH")

    environment = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "DEPLOYMENT_ID": deployment_id,
        "BOOTSTRAP_MAINTENANCE": "1",
        "PGBACKREST_RESTORE_SET": postgres_backup_set,
        "RESTIC_SNAPSHOT_ID": session_snapshot_id,
        "ERASURE_LEDGER_FILE": str(ledger),
        "ERASURE_LEDGER_SHA256": erasure_ledger_sha256,
    }
    if local_overlay is not None:
        environment["TUDT_LOCAL_BACKUP_VALIDATION"] = "explicit-local-synthetic-only"
    objects = _declared_project_objects(
        base,
        project_name,
        extra_volume_names=_LOCAL_VALIDATION_VOLUMES if local_overlay is not None else (),
    )
    _assert_new_target(
        docker,
        project_name=project_name,
        objects=objects,
        environment=environment,
    )
    prefix = _compose_prefix(
        docker,
        base=base,
        overlay=overlay,
        env_file=environment_file,
        project_name=project_name,
        validation_overlay=local_overlay,
    )
    # Keep migration ahead of gate reset: deployment_restore_state is introduced by
    # the M8 migration, so a restored pre-M8 database cannot be gated safely first.
    stages: tuple[tuple[str, tuple[str, ...], int], ...] = (
        ("compose-validated", _COMPOSE_CONFIG_ARGS, 60),
        (
            "postgres-restored",
            (
                "--profile",
                "restore",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "postgres-restore",
            ),
            7200,
        ),
        (
            "session-restored",
            (
                "--profile",
                "restore",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "session-restore",
            ),
            1800,
        ),
        (
            "dependencies-ready",
            ("up", "--detach", "--wait", "--pull", "never", "postgres", "redis"),
            600,
        ),
        (
            "schema-migrated",
            ("run", "--rm", "--no-deps", "--pull", "never", "migrate"),
            1800,
        ),
        (
            "restore-gate-closed",
            _RESTORE_GATE_CLOSE_ARGS,
            300,
        ),
        (
            "erasure-replay-staged",
            (
                "--profile",
                "restore",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "restore-gate-open",
                "/opt/ops/restore_gate.py",
                "--stage-erasure-replay",
            ),
            900,
        ),
        (
            "restore-gate-open",
            (
                "--profile",
                "restore",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "restore-gate-open",
            ),
            900,
        ),
    )
    evidence_run = _reserve_evidence_run(evidence, project_name)
    records: list[Mapping[str, object]] = []
    record = _write_stage(
        evidence_run,
        deployment_id=deployment_id,
        project_name=project_name,
        evidence_scope=evidence_scope,
        sequence=0,
        stage="target-verified-new",
        result="PASS",
        code="TARGET_NEW",
        previous_sha256="0" * 64,
    )
    records.append(record)
    for sequence, (stage, arguments, timeout) in enumerate(stages, start=1):
        try:
            if stage == "compose-validated":
                _validate_rendered_compose(
                    prefix,
                    environment=environment,
                    deployment_images=deployment_images,
                )
            else:
                _run([*prefix, *arguments], environment=environment, timeout=timeout)
        except RestoreOrchestrationError:
            record = _write_stage(
                evidence_run,
                deployment_id=deployment_id,
                project_name=project_name,
                evidence_scope=evidence_scope,
                sequence=sequence,
                stage=stage,
                result="FAIL",
                code="STAGE_FAILED",
                previous_sha256=str(records[-1]["sha256"]),
            )
            records.append(record)
            _write_final_manifest(
                evidence_run,
                deployment_id=deployment_id,
                project_name=project_name,
                evidence_scope=evidence_scope,
                result="FAIL",
                stages=records,
            )
            raise
        record = _write_stage(
            evidence_run,
            deployment_id=deployment_id,
            project_name=project_name,
            evidence_scope=evidence_scope,
            sequence=sequence,
            stage=stage,
            result="PASS",
            code="STAGE_COMPLETE",
            previous_sha256=str(records[-1]["sha256"]),
        )
        records.append(record)
    sequence = len(stages) + 1
    try:
        _verify_runtime_set(prefix, environment=environment)
    except RestoreOrchestrationError:
        # gate-open changes the durable state before this final inventory check.  If
        # an unexpected service is running, immediately re-close the gate so a
        # failed restore cannot leave an apparently usable database behind.
        _reclose_gate_best_effort(prefix, environment=environment)
        record = _write_stage(
            evidence_run,
            deployment_id=deployment_id,
            project_name=project_name,
            evidence_scope=evidence_scope,
            sequence=sequence,
            stage="runtime-set-verified",
            result="FAIL",
            code="RESTORE_RUNTIME_SET_INVALID",
            previous_sha256=str(records[-1]["sha256"]),
        )
        records.append(record)
        _write_final_manifest(
            evidence_run,
            deployment_id=deployment_id,
            project_name=project_name,
            evidence_scope=evidence_scope,
            result="FAIL",
            stages=records,
        )
        raise
    record = _write_stage(
        evidence_run,
        deployment_id=deployment_id,
        project_name=project_name,
        evidence_scope=evidence_scope,
        sequence=sequence,
        stage="runtime-set-verified",
        result="PASS",
        code="ONLY_POSTGRES_REDIS_RUNNING",
        previous_sha256=str(records[-1]["sha256"]),
    )
    records.append(record)
    _write_final_manifest(
        evidence_run,
        deployment_id=deployment_id,
        project_name=project_name,
        evidence_scope=evidence_scope,
        result="PASS",
        stages=records,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--restore-overlay", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--deployment-config", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--confirm-new-project", required=True)
    parser.add_argument("--postgres-backup-set", required=True)
    parser.add_argument("--session-snapshot-id", required=True)
    parser.add_argument("--erasure-ledger", type=Path, required=True)
    parser.add_argument("--erasure-ledger-sha256", required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--validation-overlay", type=Path)
    parser.add_argument("--ack-local-synthetic-only")
    args = parser.parse_args(argv)
    try:
        run_restore(
            compose_file=args.compose_file,
            restore_overlay=args.restore_overlay,
            env_file=args.env_file,
            deployment_config=args.deployment_config,
            project_name=args.project_name,
            confirmation=args.confirm_new_project,
            postgres_backup_set=args.postgres_backup_set,
            session_snapshot_id=args.session_snapshot_id,
            erasure_ledger=args.erasure_ledger,
            erasure_ledger_sha256=args.erasure_ledger_sha256,
            evidence_directory=args.evidence_directory,
            validation_overlay=args.validation_overlay,
            local_validation_ack=args.ack_local_synthetic_only,
        )
    except RestoreOrchestrationError as error:
        return _fail(error.code)
    print("RESTORE_ORCHESTRATION_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
