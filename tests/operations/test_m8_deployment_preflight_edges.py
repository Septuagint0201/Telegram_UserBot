from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.config import deployment_preflight
from telegram_userbot.platform.config.deployment_preflight import (
    DeploymentPreflightError,
    DeploymentPreflightInputs,
    validate_deployment_preflight,
)
from telegram_userbot.platform.config.secrets import SecretFileError, SecretFilePolicy

ROOT = Path(__file__).resolve().parents[2]
SECRET_ROOT = "/etc/telegram-userbot/secrets"  # noqa: S105 - reviewed test-only root
SERVICE_ARTIFACTS = {
    "https-gateway": "gateway",
    "app": "application",
    "control": "application",
    "worker": "application",
    "postgres": "database",
    "redis": "redis",
    "migrate": "application",
    "session-backup": "session-backup",
    "data-export": "data-export",
    "erasure-ledger-export": "session-backup",
    "ops-monitor": "session-backup",
}


class SafeRootOperations:
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        if path == SECRET_ROOT:
            return os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        if Path(path).suffix == ".json":
            return os.stat_result((stat.S_IFREG | 0o640, 0, 0, 1, 0, 10001, 0, 0, 0, 0))
        return os.stat_result((stat.S_IFDIR | 0o750, 0, 0, 1, 0, 10001, 0, 0, 0, 0))

    @staticmethod
    def resolve(path: str) -> str:
        return path


class UnavailableRootOperations(SafeRootOperations):
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        if path == SECRET_ROOT:
            raise OSError("synthetic unavailable root")
        return SafeRootOperations.lstat(path)


class ResolvedElsewhereRootOperations(SafeRootOperations):
    @staticmethod
    def resolve(path: str) -> str:
        return "/elsewhere" if path == SECRET_ROOT else path


def deployment_files(tmp_path: Path) -> tuple[Path, Path]:
    deployment = json.loads(
        (ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")
    )
    deployment.update(
        deployable=True,
        deployment_id="m8-preflight-edge-test",
        source_commit="1" * 40,
        public_host="preflight.example.org",
    )
    references = {
        name: f"registry.example.org/{name}@sha256:{position:064x}"
        for position, name in enumerate(deployment["images"], start=1)
    }
    references["data-export"] = references["session-backup"]
    for name, reference in references.items():
        deployment["images"][name] = {"status": "BUILT", "reference": reference}

    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
    (tmp_path / "secret-files.json").write_text(
        (ROOT / "deploy/config/secret-files.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    compose = json.loads((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    compose["name"] = "m8-preflight-edge-test"
    config_directory = deployment_path.parent.as_posix()
    source_root = ROOT.as_posix()
    for service, artifact in SERVICE_ARTIFACTS.items():
        compose["services"][service]["image"] = references[artifact]
    for service in compose["services"].values():
        for volume in service.get("volumes", []):
            target = volume.get("target")
            if target == "/etc/telegram-userbot/config":
                volume["source"] = config_directory
            elif target == "/etc/caddy/Caddyfile":
                volume["source"] = f"{source_root}/deploy/caddy/Caddyfile"
            elif target == "/etc/redis/redis.conf":
                volume["source"] = f"{source_root}/deploy/redis/redis.conf"
            elif target == "/ops-state":
                volume["source"] = "/var/lib/telegram-userbot/ops-state"
            elif target == "/export-staging":
                volume["source"] = "/var/lib/telegram-userbot/export-staging"
            elif target == "/etc/telegram-userbot/export/age-recipient":
                volume["source"] = "/etc/telegram-userbot/export/age-recipient"
    compose["secrets"] = {
        entry["id"]: {"file": f"{SECRET_ROOT}/{entry['filename']}"}
        for entry in json.loads(
            (ROOT / "deploy/config/secret-files.json").read_text(encoding="utf-8")
        )["files"]
    }
    compose_path = tmp_path / "compose.json"
    compose_path.write_text(json.dumps(compose), encoding="utf-8")
    return deployment_path, compose_path


def read_secret(_path: str, _policy: SecretFilePolicy) -> SensitiveValue[bytes]:
    return SensitiveValue(b"opaque-value-never-returned")


def validate(deployment_path: Path, compose_path: Path) -> None:
    validate_deployment_preflight(
        DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
        operations=SafeRootOperations(),
        secret_reader=read_secret,
    )


def write_compose(compose_path: Path, compose: dict[str, Any]) -> None:
    compose_path.write_text(json.dumps(compose), encoding="utf-8")


ComposeMutation = Callable[[dict[str, Any]], None]


def _remove_worker(compose: dict[str, Any]) -> None:
    del compose["services"]["worker"]


def _remove_worker_image(compose: dict[str, Any]) -> None:
    del compose["services"]["worker"]["image"]


def _mount_docker_socket(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]


def _mount_run_docker_socket(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["volumes"].append(
        {
            "type": "bind",
            "source": "/run/docker.sock",
            "target": "/run/docker.sock",
        }
    )


def _mount_unexpected_host_directory(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["volumes"].append(
        {"type": "bind", "source": "/etc", "target": "/unexpected-host", "read_only": True}
    )


def _add_unexpected_top_level_volume(compose: dict[str, Any]) -> None:
    compose["volumes"]["unexpected"] = {}


def _add_unexpected_tmpfs(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["tmpfs"].append(
        "/unexpected-tmpfs:rw,noexec,nosuid,size=1m,uid=10001,gid=10001"
    )


def _set_root_user(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["user"] = "0:0"


def _set_invalid_networks(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["networks"] = "backend"


def _set_network_mismatch(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["networks"] = ["edge"]


def _set_invalid_secrets(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["secrets"] = {"source": "telegram_api_id"}


def _duplicate_app_secret(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["secrets"].append("telegram_api_id")


def _set_incorrect_secret_target(compose: dict[str, Any]) -> None:
    service_secrets = compose["services"]["app"]["secrets"]
    compose["services"]["app"]["secrets"] = [
        {"source": service_secrets[0], "target": "/run/secrets/not-the-source"},
        *service_secrets[1:],
    ]


def _remove_app_secret(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["secrets"] = compose["services"]["app"]["secrets"][:-1]


def _set_invalid_secret_mapping(compose: dict[str, Any]) -> None:
    service_secrets = compose["services"]["app"]["secrets"]
    compose["services"]["app"]["secrets"] = [
        {"source": service_secrets[0], "read_only": True},
        *service_secrets[1:],
    ]


def _set_invalid_environment(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["environment"] = []


def _set_inline_password(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["environment"]["DATABASE_PASSWORD"] = "synthetic"  # noqa: S105 - test fixture


def _set_non_gateway_port(compose: dict[str, Any]) -> None:
    compose["services"]["app"]["ports"] = [{"target": 1234, "published": "1234"}]


def _set_gateway_target_port(compose: dict[str, Any]) -> None:
    compose["services"]["https-gateway"]["ports"][0]["target"] = 8080


def _remove_gateway_port(compose: dict[str, Any]) -> None:
    compose["services"]["https-gateway"]["ports"] = []


def _set_control_expose(compose: dict[str, Any]) -> None:
    compose["services"]["control"]["expose"] = ["9090"]


def _set_monitor_expose(compose: dict[str, Any]) -> None:
    compose["services"]["ops-monitor"]["expose"] = ["8080"]


def _remove_ops_profile(compose: dict[str, Any]) -> None:
    compose["services"]["data-export"]["profiles"] = []


def _set_config_mount_source(compose: dict[str, Any]) -> None:
    for volume in compose["services"]["app"]["volumes"]:
        if volume.get("target") == "/etc/telegram-userbot/config":
            volume["source"] = "/etc/telegram-userbot/other"
            return
    raise AssertionError("app config mount was not found")


RUNTIME_MUTATIONS: dict[str, ComposeMutation] = {
    "service-inventory": _remove_worker,
    "missing-image": _remove_worker_image,
    "forbidden-socket": _mount_docker_socket,
    "alternate-docker-socket": _mount_run_docker_socket,
    "unexpected-host-bind": _mount_unexpected_host_directory,
    "unexpected-top-level-volume": _add_unexpected_top_level_volume,
    "unexpected-tmpfs": _add_unexpected_tmpfs,
    "root-user": _set_root_user,
    "invalid-networks": _set_invalid_networks,
    "network-mismatch": _set_network_mismatch,
    "invalid-secrets": _set_invalid_secrets,
    "duplicate-secret": _duplicate_app_secret,
    "incorrect-secret-target": _set_incorrect_secret_target,
    "invalid-secret-mapping": _set_invalid_secret_mapping,
    "secret-inventory-mismatch": _remove_app_secret,
    "invalid-environment": _set_invalid_environment,
    "inline-password": _set_inline_password,
    "non-gateway-port": _set_non_gateway_port,
    "gateway-port": _set_gateway_target_port,
    "gateway-port-shape": _remove_gateway_port,
    "control-expose": _set_control_expose,
    "monitor-expose": _set_monitor_expose,
    "ops-profile": _remove_ops_profile,
    "config-mount-source": _set_config_mount_source,
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"services":{},"services":{}}', "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
        (b'{"services":{}\x00}', "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
        (b"{", "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
        (b"[]", "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
        (b"", "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
    ],
)
def test_preflight_fails_closed_for_malformed_rendered_compose(
    tmp_path: Path, raw: bytes, code: str
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)
    compose_path.write_bytes(raw)

    with pytest.raises(DeploymentPreflightError, match=f"^{code}$"):
        validate(deployment_path, compose_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("service-inventory", "DEPLOYMENT_PREFLIGHT_SERVICE_INVENTORY_MISMATCH"),
        ("missing-image", "DEPLOYMENT_PREFLIGHT_COMPOSE_INVALID"),
        ("forbidden-socket", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("alternate-docker-socket", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("unexpected-host-bind", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("unexpected-top-level-volume", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("unexpected-tmpfs", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("root-user", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("invalid-networks", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("network-mismatch", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("invalid-secrets", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("duplicate-secret", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("incorrect-secret-target", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("invalid-secret-mapping", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("secret-inventory-mismatch", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("invalid-environment", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("inline-password", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("non-gateway-port", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("gateway-port", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("gateway-port-shape", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("control-expose", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("monitor-expose", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("ops-profile", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
        ("config-mount-source", "DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH"),
    ],
)
def test_preflight_rejects_runtime_contract_boundary_drift(
    tmp_path: Path, mutation: str, code: str
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    RUNTIME_MUTATIONS[mutation](compose)
    write_compose(compose_path, compose)

    with pytest.raises(DeploymentPreflightError, match=f"^{code}$"):
        validate(deployment_path, compose_path)


@pytest.mark.unit
def test_preflight_accepts_rendered_dict_networks_and_explicit_secret_mappings(
    tmp_path: Path,
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    for service in compose["services"].values():
        service["networks"] = {name: {} for name in service["networks"]}
    app_secrets = compose["services"]["app"]["secrets"]
    compose["services"]["app"]["secrets"] = [
        {"source": secret, "target": f"/run/secrets/{secret}"} for secret in app_secrets
    ]
    write_compose(compose_path, compose)

    validate(deployment_path, compose_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "binding",
    [
        [],
        "not-a-binding",
        {"file": f"{SECRET_ROOT}-wrong/redis_password"},
        {"file": f"{SECRET_ROOT}/redis_password", "name": ""},
    ],
)
def test_preflight_rejects_malformed_top_level_secret_bindings(
    tmp_path: Path, binding: object
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    if binding == []:
        compose["secrets"] = binding
    else:
        compose["secrets"]["redis_password"] = binding
    write_compose(compose_path, compose)

    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH$",
    ):
        validate(deployment_path, compose_path)


@pytest.mark.unit
def test_preflight_rejects_relative_or_unreadable_compose_references(tmp_path: Path) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)

    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_COMPOSE_REFERENCE_INVALID$",
    ):
        validate(deployment_path, Path("relative-compose.json"))
    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_COMPOSE_UNREADABLE$",
    ):
        validate(deployment_path, compose_path.with_name("not-present.json"))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("operations", "reader", "code"),
    [
        (
            UnavailableRootOperations(),
            read_secret,
            "DEPLOYMENT_PREFLIGHT_SECRET_ROOT_UNAVAILABLE",
        ),
        (
            ResolvedElsewhereRootOperations(),
            read_secret,
            "DEPLOYMENT_PREFLIGHT_SECRET_ROOT_UNSAFE",
        ),
        (
            SafeRootOperations(),
            lambda _path, _policy: (_ for _ in ()).throw(SecretFileError("SYNTHETIC")),
            "DEPLOYMENT_PREFLIGHT_SECRET_FILE_INVALID",
        ),
    ],
)
def test_preflight_keeps_host_and_secret_failures_content_free(
    tmp_path: Path,
    operations: SafeRootOperations,
    reader: object,
    code: str,
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)

    with pytest.raises(DeploymentPreflightError, match=f"^{code}$") as raised:
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
            operations=operations,
            secret_reader=reader,  # type: ignore[arg-type]
        )

    assert "SYNTHETIC" not in repr(raised.value)


@pytest.mark.unit
def test_preflight_uses_default_secret_reader_and_hides_invalid_manifest_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment_path, compose_path = deployment_files(tmp_path)
    calls: list[str] = []

    def default_reader(path: str, _policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        calls.append(path)
        return SensitiveValue(b"opaque")

    monkeypatch.setattr(deployment_preflight, "read_secret_file", default_reader)
    result = validate_deployment_preflight(
        DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
        operations=SafeRootOperations(),
    )
    assert result.checked_secrets == len(calls)

    deployment_path.write_text("{}", encoding="utf-8")
    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_MANIFEST_INVALID$",
    ) as raised:
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
            operations=SafeRootOperations(),
            secret_reader=read_secret,
        )
    assert str(deployment_path) not in repr(raised.value)
