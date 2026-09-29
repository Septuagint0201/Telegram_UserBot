"""Host-side, content-free deployment preflight.

This module never invokes Docker and is not imported by a business process. An operator
renders Compose to JSON on the host, then validates that rendered image references and the
root-owned secret source exactly match the reviewed deployment manifest.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.config.production import (
    DeploymentContract,
    ProductionConfigurationError,
    load_deployment_contract,
)
from telegram_userbot.platform.config.secrets import (
    SecretFileError,
    SecretFilePolicy,
    read_secret_file,
)

_MAX_COMPOSE_CONFIG_BYTES = 1024 * 1024
_CONFIG_READER_GID = 10001
_CONFIG_DIRECTORY_MODE = 0o750
_CONFIG_FILE_MODE = 0o640
_CONFIG_MOUNT_TARGET = "/etc/telegram-userbot/config"
_CONFIG_CONSUMER_SERVICES = frozenset(
    {"app", "control", "worker", "migrate", "data-export", "erasure-ledger-export"}
)
_SERVICE_ARTIFACTS = {
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
}
_SERVICE_NETWORKS = {
    "https-gateway": frozenset({"edge"}),
    "app": frozenset({"backend"}),
    "control": frozenset({"edge", "backend"}),
    "worker": frozenset({"backend"}),
    "postgres": frozenset({"backend", "backup-egress"}),
    "redis": frozenset({"backend"}),
    "migrate": frozenset({"backend"}),
    "session-backup": frozenset({"backup-egress"}),
    "data-export": frozenset({"backend"}),
    "ops-monitor": frozenset({"backend"}),
    "erasure-ledger-export": frozenset({"backend", "backup-egress"}),
}
_TOP_LEVEL_VOLUMES = frozenset(
    {
        "postgres-data",
        "pgbackrest-spool",
        "redis-data",
        "telethon-session",
        "media-data",
        "caddy-data",
    }
)

# The source field uses a token for host paths which are supplied by Compose
# interpolation.  The rendered value is checked against the fixed host layout
# below; the unresolved token remains accepted by unit fixtures which do not
# invoke Compose.
_SOURCE_CONFIG = "$CONFIG"
_SOURCE_OPS_STATE = "$OPS_STATE"
_SOURCE_EXPORT_STAGING = "$EXPORT_STAGING"
_SOURCE_ERASURE_LEDGER = "$ERASURE_LEDGER"
_SOURCE_AGE_RECIPIENT = "$AGE_RECIPIENT"
_SOURCE_CADDYFILE = "$CADDYFILE"
_SOURCE_REDIS_CONFIG = "$REDIS_CONFIG"
_SOURCE_BOOTSTRAP = "$BOOTSTRAP"
# These are the only host paths that may be supplied by deployment-time
# interpolation.  They are deliberately compared after Compose has rendered the
# file; an unresolved ``${...}`` token is not a valid preflight input.
_FIXED_HOST_BIND_SOURCES = {
    _SOURCE_OPS_STATE: "/var/lib/telegram-userbot/ops-state",
    _SOURCE_EXPORT_STAGING: "/var/lib/telegram-userbot/export-staging",
    _SOURCE_ERASURE_LEDGER: "/var/lib/telegram-userbot/erasure-ledger",
    _SOURCE_AGE_RECIPIENT: "/etc/telegram-userbot/export/age-recipient",
}

# (type, target, read_only, source).  A source beginning with "$" is one of
# the reviewed host-path tokens above; all other sources are named volumes.
_SERVICE_VOLUMES: dict[str, tuple[tuple[str, str, bool, str], ...]] = {
    "https-gateway": (
        ("bind", "/etc/caddy/Caddyfile", True, _SOURCE_CADDYFILE),
        ("volume", "/data", False, "caddy-data"),
    ),
    "app": (
        ("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),
        ("volume", "/var/lib/telegram-userbot/session", False, "telethon-session"),
        ("volume", "/var/lib/telegram-userbot/media", False, "media-data"),
    ),
    "control": (("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),),
    "worker": (
        ("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),
        ("volume", "/var/lib/telegram-userbot/media", True, "media-data"),
    ),
    "postgres": (
        ("volume", "/var/lib/postgresql/data", False, "postgres-data"),
        ("volume", "/var/spool/pgbackrest", False, "pgbackrest-spool"),
        ("bind", "/ops-state", False, _SOURCE_OPS_STATE),
    ),
    "redis": (
        ("bind", "/etc/redis/redis.conf", True, _SOURCE_REDIS_CONFIG),
        ("volume", "/data", False, "redis-data"),
    ),
    "migrate": (("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),),
    "session-backup": (
        ("volume", "/session", True, "telethon-session"),
        ("bind", "/ops-state", False, _SOURCE_OPS_STATE),
    ),
    "data-export": (
        ("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),
        ("bind", "/export-staging", False, _SOURCE_EXPORT_STAGING),
        (
            "bind",
            "/etc/telegram-userbot/export/age-recipient",
            True,
            _SOURCE_AGE_RECIPIENT,
        ),
    ),
    "erasure-ledger-export": (
        ("bind", _CONFIG_MOUNT_TARGET, True, _SOURCE_CONFIG),
        ("bind", "/erasure-ledger", False, _SOURCE_ERASURE_LEDGER),
        ("bind", "/ops-state", False, _SOURCE_OPS_STATE),
    ),
    "ops-monitor": (
        ("bind", "/ops-state", True, _SOURCE_OPS_STATE),
        ("volume", "/media", True, "media-data"),
    ),
}
_OPTIONAL_BOOTSTRAP_VOLUME = (
    "bind",
    "/docker-entrypoint-initdb.d",
    True,
    _SOURCE_BOOTSTRAP,
)
_SERVICE_TMPFS = {
    "https-gateway": (
        "/tmp:rw,noexec,nosuid,size=16m,uid=1000,gid=1000",  # noqa: S108 - container tmpfs
        "/config:rw,noexec,nosuid,size=16m,uid=1000,gid=1000",
    ),
    "app": (
        "/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",  # noqa: S108 - container tmpfs
        "/var/lib/telegram-userbot/runtime:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
    ),
    "control": (
        "/tmp:rw,noexec,nosuid,size=32m,uid=10001,gid=10001",  # noqa: S108 - container tmpfs
        "/var/lib/telegram-userbot/runtime:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
    ),
    "worker": (
        "/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",  # noqa: S108 - container tmpfs
        "/var/lib/telegram-userbot/runtime:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
    ),
    "postgres": (
        "/tmp:rw,noexec,nosuid,size=64m,uid=999,gid=999",  # noqa: S108 - container tmpfs
        "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999",
        "/run/pgbackrest:rw,noexec,nosuid,size=4m,uid=999,gid=999",
    ),
    "redis": (
        "/tmp:rw,noexec,nosuid,size=16m,uid=999,gid=999",  # noqa: S108 - container tmpfs
        "/run/redis:rw,noexec,nosuid,size=4m,uid=999,gid=999",
    ),
    "migrate": (
        "/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",  # noqa: S108 - container tmpfs
        "/var/lib/telegram-userbot/runtime:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
    ),
    "session-backup": ("/tmp:rw,noexec,nosuid,size=32m,uid=10001,gid=10001",),  # noqa: S108 - container tmpfs
    "data-export": ("/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",),  # noqa: S108 - container tmpfs
    "ops-monitor": ("/tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",),  # noqa: S108 - container tmpfs
    "erasure-ledger-export": ("/tmp:rw,noexec,nosuid,size=32m,uid=10001,gid=10001",),  # noqa: S108 - container tmpfs
}
_SERVICE_SECRETS = {
    "https-gateway": frozenset(),
    "app": frozenset(
        {
            "telegram_api_id",
            "telegram_api_hash",
            "credential_master_keyring",
            "app_database_password",
            "redis_password",
            "erasure_hmac_key",
        }
    ),
    "control": frozenset(
        {
            "control_bot_token",
            "credential_master_keyring",
            "control_database_password",
            "redis_password",
            "export_actor_hmac_key",
        }
    ),
    "worker": frozenset(
        {
            "credential_master_keyring",
            "worker_database_password",
            "redis_password",
            "erasure_hmac_key",
        }
    ),
    "postgres": frozenset(
        {
            "pgbackrest_s3_access_key",
            "pgbackrest_s3_secret_key",
            "pgbackrest_repo_cipher_pass",
        }
    ),
    "redis": frozenset({"redis_password"}),
    "migrate": frozenset({"migrator_database_password"}),
    "session-backup": frozenset(
        {"session_restic_password", "session_s3_access_key", "session_s3_secret_key"}
    ),
    "data-export": frozenset({"export_database_password"}),
    "ops-monitor": frozenset({"monitor_database_password"}),
    "erasure-ledger-export": frozenset(
        {
            "export_database_password",
            "erasure_hmac_key",
            "erasure_restic_password",
            "erasure_s3_access_key",
            "erasure_s3_secret_key",
        }
    ),
}


class DeploymentPreflightError(RuntimeError):
    """Stable preflight failure that contains no path, image, or secret content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PreflightFileOperations(Protocol):
    def lstat(self, path: str) -> os.stat_result: ...

    def resolve(self, path: str) -> str: ...


class _SystemPreflightFileOperations:
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        return os.lstat(path)

    @staticmethod
    def resolve(path: str) -> str:
        return Path(path).resolve(strict=True).as_posix()


SecretReader = Callable[[str, SecretFilePolicy], SensitiveValue[bytes]]


@dataclass(frozen=True, slots=True)
class DeploymentPreflightResult:
    deployment_id: str
    checked_services: int
    checked_secrets: int


@dataclass(frozen=True, slots=True)
class DeploymentPreflightInputs:
    """Explicit host paths consumed by one preflight invocation."""

    deployment_config: Path
    compose_config: Path
    source_root: Path
    secret_root: str


def _fail(code: str) -> DeploymentPreflightError:
    return DeploymentPreflightError(code)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID")
        result[key] = value
    return result


def _read_compose_config(path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_REFERENCE_INVALID")
    try:
        with path.open("rb") as stream:
            content = stream.read(_MAX_COMPOSE_CONFIG_BYTES + 1)
    except OSError:
        raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_UNREADABLE") from None
    if not content or len(content) > _MAX_COMPOSE_CONFIG_BYTES or b"\x00" in content:
        raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID")
    try:
        value: object = json.loads(content, object_pairs_hook=_unique_object)
    except DeploymentPreflightError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError:
        raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID") from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID")
    return value


def _validate_images(contract: DeploymentContract, compose: dict[str, object]) -> int:
    services = compose.get("services")
    if not isinstance(services, dict) or set(services) != set(_SERVICE_ARTIFACTS):
        raise _fail("DEPLOYMENT_PREFLIGHT_SERVICE_INVENTORY_MISMATCH")
    for service_name, artifact_name in _SERVICE_ARTIFACTS.items():
        service = services.get(service_name)
        if not isinstance(service, dict) or set(service).isdisjoint({"image"}):
            raise _fail("DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID")
        image = service.get("image")
        if image != contract.deployment.artifact(artifact_name).reference:
            raise _fail("DEPLOYMENT_PREFLIGHT_IMAGE_MISMATCH")
    return len(services)


def _service_networks(service: dict[str, object]) -> frozenset[str]:
    networks = service.get("networks")
    if isinstance(networks, dict) and all(isinstance(name, str) for name in networks):
        return frozenset(networks)
    if isinstance(networks, list) and all(isinstance(name, str) for name in networks):
        return frozenset(networks)
    raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")


def _service_secrets(service: dict[str, object]) -> frozenset[str]:
    raw_secrets = service.get("secrets", [])
    if not isinstance(raw_secrets, list):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    parsed: set[str] = set()
    for item in raw_secrets:
        if isinstance(item, str):
            source = item
            target = f"/run/secrets/{item}"
        elif isinstance(item, dict) and set(item) <= {"source", "target", "uid", "gid", "mode"}:
            source_value = item.get("source")
            target_value = item.get("target")
            source = source_value if isinstance(source_value, str) else ""
            target = target_value if isinstance(target_value, str) else ""
        else:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if not isinstance(source, str) or source in parsed or target != f"/run/secrets/{source}":
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        parsed.add(source)
    return frozenset(parsed)


def _validate_runtime_contract(  # noqa: PLR0912
    compose: dict[str, object], config_directory: str, source_root: str
) -> None:
    services = compose.get("services")
    if not isinstance(services, dict):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    serialized = json.dumps(services, sort_keys=True)
    if any(
        forbidden in serialized
        for forbidden in (
            "/var/run/docker.sock",
            "/run/docker.sock",
            '"privileged": true',
            '"network_mode": "host"',
            '"pid": "host"',
        )
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")

    for service_name, raw_service in services.items():
        if not isinstance(service_name, str) or not isinstance(raw_service, dict):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        service = raw_service
        user = service.get("user")
        pids_limit = service.get("pids_limit")
        cpu_limit = service.get("cpus")
        if (
            not isinstance(user, str)
            or user.split(":", maxsplit=1)[0] == "0"
            or service.get("platform") != "linux/amd64"
            or service.get("read_only") is not True
            or service.get("cap_drop") != ["ALL"]
            or service.get("security_opt") != ["no-new-privileges:true"]
            or type(pids_limit) is not int
            or pids_limit <= 0
            or not isinstance(service.get("mem_limit"), str)
            or not service.get("mem_limit")
            or not isinstance(cpu_limit, int | float)
            or cpu_limit <= 0
            or service.get("restart") not in {"unless-stopped", "no"}
            or not service.get("stop_grace_period")
            or "build" in service
        ):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if service_name == "https-gateway" and service.get("init") is not True:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if _service_networks(service) != _SERVICE_NETWORKS[service_name]:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if _service_secrets(service) != _SERVICE_SECRETS[service_name]:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if service_name in _CONFIG_CONSUMER_SERVICES and user != "10001:10001":
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        environment = service.get("environment", {})
        if not isinstance(environment, dict):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or "\x00" in str(value)
                or ("PASSWORD" in key and not key.endswith("_FILE"))
                or key.endswith("DSN")
            ):
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")

    for service_name, raw_service in services.items():
        service = cast(dict[str, object], raw_service)
        ports = service.get("ports", [])
        if service_name == "https-gateway":
            if not isinstance(ports, list) or len(ports) != 1 or not isinstance(ports[0], dict):
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
            port = ports[0]
            if (
                port.get("target") != 8443
                or str(port.get("published")) != "443"
                or port.get("protocol", "tcp") != "tcp"
                or port.get("host_ip") not in (None, "")
            ):
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        elif ports:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")

    control = cast(dict[str, object], services["control"])
    monitor = cast(dict[str, object], services["ops-monitor"])
    if control.get("expose") not in (["8080"], [8080]):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    if monitor.get("expose") not in (["9090"], [9090]):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    for service_name in ("session-backup", "data-export", "erasure-ledger-export"):
        service = cast(dict[str, object], services[service_name])
        if service.get("profiles") != ["ops"]:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    _validate_service_volumes(compose, config_directory, source_root)


def _validate_deployment_config(
    deployment_config: Path,
    *,
    operations: PreflightFileOperations,
) -> str:
    if not deployment_config.is_absolute():
        raise _fail("DEPLOYMENT_PREFLIGHT_CONFIG_REFERENCE_INVALID")
    config_path = deployment_config.as_posix()
    config_directory = deployment_config.parent.as_posix()
    try:
        directory_metadata = operations.lstat(config_directory)
        directory_resolved = operations.resolve(config_directory)
    except OSError:
        raise _fail("DEPLOYMENT_PREFLIGHT_CONFIG_UNAVAILABLE") from None
    if (
        directory_resolved != config_directory
        or stat.S_ISLNK(directory_metadata.st_mode)
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_uid != 0
        or directory_metadata.st_gid != _CONFIG_READER_GID
        or stat.S_IMODE(directory_metadata.st_mode) != _CONFIG_DIRECTORY_MODE
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_CONFIG_UNSAFE")
    _validate_config_file(config_path, operations=operations)
    return config_directory


def _validate_config_file(config_path: str, *, operations: PreflightFileOperations) -> None:
    try:
        metadata = operations.lstat(config_path)
        resolved = operations.resolve(config_path)
    except OSError:
        raise _fail("DEPLOYMENT_PREFLIGHT_CONFIG_UNAVAILABLE") from None
    if (
        resolved != config_path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != _CONFIG_READER_GID
        or stat.S_IMODE(metadata.st_mode) != _CONFIG_FILE_MODE
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_CONFIG_UNSAFE")


def _compose_project_name(compose: Mapping[str, object]) -> str:
    name = compose.get("name")
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) is None:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    return name


def _validate_top_level_volumes(compose: Mapping[str, object]) -> dict[str, frozenset[str]]:
    raw_volumes = compose.get("volumes")
    if not isinstance(raw_volumes, dict) or set(raw_volumes) != _TOP_LEVEL_VOLUMES:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")

    project_name = _compose_project_name(compose)
    aliases: dict[str, frozenset[str]] = {}
    for declared_name, raw_definition in raw_volumes.items():
        if not isinstance(declared_name, str) or not isinstance(raw_definition, dict):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if set(raw_definition) - {"name", "external", "driver"}:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        external = raw_definition.get("external", False)
        if type(external) is not bool or external:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        driver = raw_definition.get("driver")
        if driver is not None and driver != "local":
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        rendered_name = raw_definition.get("name")
        expected_rendered_name = f"{project_name}_{declared_name}"
        if rendered_name is not None and rendered_name != expected_rendered_name:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        aliases[declared_name] = frozenset(
            {declared_name} | ({rendered_name} if isinstance(rendered_name, str) else set())
        )
    return aliases


def _validate_mount_options(value: dict[str, object], mount_type: str) -> None:
    allowed = {"type", "source", "target", "read_only"}
    if mount_type == "bind":
        allowed.add("bind")
        if "volume" in value:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        bind_options = value.get("bind")
        if bind_options is not None:
            if not isinstance(bind_options, dict) or set(bind_options) != {"create_host_path"}:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
            if bind_options["create_host_path"] is not False:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    else:
        allowed.add("volume")
        if "bind" in value:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        volume_options = value.get("volume")
        if volume_options is not None:
            if not isinstance(volume_options, dict) or set(volume_options) - {"nocopy"}:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
            if "nocopy" in volume_options and type(volume_options["nocopy"]) is not bool:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    if set(value) - allowed:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")


def _normalize_volume(value: object) -> tuple[str, str, str, bool]:
    mount_type: str
    source: str
    target: str
    read_only: bool
    if isinstance(value, dict):
        mount_type_value = value.get("type")
        source_value = value.get("source")
        target_value = value.get("target")
        read_only_value = value.get("read_only", False)
        if not isinstance(mount_type_value, str) or mount_type_value not in {"bind", "volume"}:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if not isinstance(source_value, str) or not isinstance(target_value, str):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        if type(read_only_value) is not bool:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        mount_type = mount_type_value
        source = source_value
        target = target_value
        read_only = read_only_value
        _validate_mount_options(value, mount_type)
    else:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    if (
        not source
        or not target.startswith("/")
        or "\x00" in source
        or "\x00" in target
        or any(part in {"", ".", ".."} for part in PurePosixPath(target).parts[1:])
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    return mount_type, source, target, read_only


def _tmpfs_signature(value: object) -> tuple[str, frozenset[str]]:
    if not isinstance(value, str):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    target, separator, options = value.partition(":")
    if not separator or not target.startswith("/") or not options:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    option_values = options.split(",")
    if any(not item for item in option_values) or len(set(option_values)) != len(option_values):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    modes = {item for item in option_values if item in {"ro", "rw"}}
    if modes != {"rw"}:
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    if not any(item.startswith("size=") for item in option_values):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    if any(part in {"", ".", ".."} for part in PurePosixPath(target).parts[1:]):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    return target, frozenset(option_values)


def _bind_source_matches(
    source: str, expected: str, config_directory: str, source_root: str
) -> bool:
    if expected == _SOURCE_CONFIG:
        return source == config_directory
    fixed = _FIXED_HOST_BIND_SOURCES.get(expected)
    if fixed is not None:
        return source == fixed
    static_sources = {
        _SOURCE_CADDYFILE: "caddy/Caddyfile",
        _SOURCE_REDIS_CONFIG: "redis/redis.conf",
        _SOURCE_BOOTSTRAP: "postgres/bootstrap",
    }
    relative = static_sources.get(expected)
    if relative is None:
        return False
    return source == f"{source_root}/deploy/{relative}"


def _volume_source_matches(
    source: str, expected: str, volume_aliases: dict[str, frozenset[str]]
) -> bool:
    return source in volume_aliases.get(expected, frozenset())


def _validate_service_volumes(
    compose: Mapping[str, object], config_directory: str, source_root: str
) -> None:
    services = compose.get("services")
    if not isinstance(services, dict):
        raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
    volume_aliases = _validate_top_level_volumes(compose)
    for service_name, expected_mounts in _SERVICE_VOLUMES.items():
        raw_service = services.get(service_name)
        if not isinstance(raw_service, dict):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        raw_volumes = raw_service.get("volumes", [])
        if not isinstance(raw_volumes, list):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        actual = [_normalize_volume(item) for item in raw_volumes]
        expected = list(expected_mounts)
        if service_name == "postgres" and any(
            item[2] == _OPTIONAL_BOOTSTRAP_VOLUME[1] for item in actual
        ):
            expected.append(_OPTIONAL_BOOTSTRAP_VOLUME)
        if len(actual) != len(expected) or len({item[2] for item in actual}) != len(actual):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        for mount_type, source, target, read_only in actual:
            matching = [
                item
                for item in expected
                if item[0] == mount_type and item[1] == target and item[2] == read_only
            ]
            if len(matching) != 1:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
            expected_source = matching[0][3]
            source_matches = (
                _bind_source_matches(source, expected_source, config_directory, source_root)
                if mount_type == "bind"
                else _volume_source_matches(source, expected_source, volume_aliases)
            )
            if not source_matches:
                raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")

        raw_tmpfs = raw_service.get("tmpfs")
        if not isinstance(raw_tmpfs, list):
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")
        actual_tmpfs = {_tmpfs_signature(item) for item in raw_tmpfs}
        expected_tmpfs = {_tmpfs_signature(item) for item in _SERVICE_TMPFS[service_name]}
        if actual_tmpfs != expected_tmpfs:
            raise _fail("DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH")


def _validate_compose_secret_bindings(
    contract: DeploymentContract,
    compose: dict[str, object],
    secret_root: str,
) -> None:
    secrets = compose.get("secrets")
    if not isinstance(secrets, dict):
        raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")
    expected = {reference.id: reference for reference in contract.secrets}
    # Compose prunes an unused top-level secret from its rendered model. The postgres
    # bootstrap password is therefore absent from the steady-state render and present
    # only when compose.bootstrap.yaml is included. No other manifest secret may vanish.
    allowed_inventories = (
        frozenset(expected),
        frozenset(expected) - {"postgres_database_password"},
    )
    if frozenset(secrets) not in allowed_inventories:
        raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")
    for secret_id, raw_binding in secrets.items():
        if not isinstance(secret_id, str) or not isinstance(raw_binding, dict):
            raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")
        if set(raw_binding) not in ({"file"}, {"file", "name"}):
            raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")
        generated_name = raw_binding.get("name")
        if generated_name is not None and (
            not isinstance(generated_name, str) or not generated_name
        ):
            raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")
        reference = expected[secret_id]
        if raw_binding.get("file") != f"{secret_root}/{reference.filename}":
            raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH")


def _validate_secret_root(
    contract: DeploymentContract,
    secret_root: str,
    *,
    operations: PreflightFileOperations,
    secret_reader: SecretReader,
) -> int:
    source = PurePosixPath(secret_root)
    if (
        not source.is_absolute()
        or str(source) != secret_root
        or secret_root != contract.secret_source_root
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_ROOT_MISMATCH")
    try:
        metadata = operations.lstat(secret_root)
        resolved = operations.resolve(secret_root)
    except OSError:
        raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_ROOT_UNAVAILABLE") from None
    if (
        resolved != secret_root
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_ROOT_UNSAFE")

    for reference in contract.secrets:
        try:
            secret_reader(f"{secret_root}/{reference.filename}", reference.policy())
        except SecretFileError:
            raise _fail("DEPLOYMENT_PREFLIGHT_SECRET_FILE_INVALID") from None
    return len(contract.secrets)


def validate_deployment_preflight(
    inputs: DeploymentPreflightInputs,
    *,
    operations: PreflightFileOperations | None = None,
    secret_reader: SecretReader | None = None,
) -> DeploymentPreflightResult:
    """Validate one already-rendered deployment without returning sensitive values."""

    file_operations = operations or _SystemPreflightFileOperations()
    source_root = inputs.source_root
    if not source_root.is_absolute() or source_root.parent == source_root:
        raise _fail("DEPLOYMENT_PREFLIGHT_SOURCE_ROOT_INVALID")
    source_root_value = source_root.as_posix()
    try:
        source_metadata = file_operations.lstat(source_root_value)
        source_resolved = file_operations.resolve(source_root_value)
    except OSError:
        raise _fail("DEPLOYMENT_PREFLIGHT_SOURCE_ROOT_INVALID") from None
    if (
        source_resolved != source_root_value
        or stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISDIR(source_metadata.st_mode)
    ):
        raise _fail("DEPLOYMENT_PREFLIGHT_SOURCE_ROOT_INVALID")
    config_directory = _validate_deployment_config(
        inputs.deployment_config,
        operations=file_operations,
    )
    try:
        contract = load_deployment_contract(inputs.deployment_config)
    except ProductionConfigurationError:
        raise _fail("DEPLOYMENT_PREFLIGHT_MANIFEST_INVALID") from None
    _validate_config_file(
        (inputs.deployment_config.parent / contract.deployment.secret_manifest).as_posix(),
        operations=file_operations,
    )
    if secret_reader is None:
        secret_reader = read_secret_file
    checked_secrets = _validate_secret_root(
        contract,
        inputs.secret_root,
        operations=file_operations,
        secret_reader=secret_reader,
    )
    compose = _read_compose_config(inputs.compose_config)
    checked_services = _validate_images(contract, compose)
    _validate_runtime_contract(compose, config_directory, source_root_value)
    _validate_compose_secret_bindings(contract, compose, inputs.secret_root)
    return DeploymentPreflightResult(
        deployment_id=contract.deployment.deployment_id,
        checked_services=checked_services,
        checked_secrets=checked_secrets,
    )


__all__ = [
    "DeploymentPreflightError",
    "DeploymentPreflightInputs",
    "DeploymentPreflightResult",
    "PreflightFileOperations",
    "SecretReader",
    "validate_deployment_preflight",
]
