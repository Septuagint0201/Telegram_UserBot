from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from telegram_userbot.platform.config import deployment_preflight as preflight
from telegram_userbot.platform.config.deployment_preflight import DeploymentPreflightError

RUNTIME_CONTRACT_ERROR = r"^DEPLOYMENT_PREFLIGHT_RUNTIME_CONTRACT_MISMATCH$"


def directory_metadata(*, mode: int = 0o750, uid: int = 0, gid: int = 10001) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


@pytest.mark.unit
def test_system_preflight_file_operations_use_lstat_and_strict_resolve(tmp_path: Path) -> None:
    path = tmp_path / "probe"
    path.write_text("probe", encoding="utf-8")

    metadata = preflight._SystemPreflightFileOperations.lstat(path.as_posix())
    resolved = preflight._SystemPreflightFileOperations.resolve(path.as_posix())

    assert stat.S_ISREG(metadata.st_mode)
    assert resolved == path.resolve(strict=True).as_posix()


@pytest.mark.unit
@pytest.mark.parametrize("services", [None, {1: {}}])
def test_runtime_contract_rejects_noncanonical_service_structures(services: object) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_runtime_contract(
            {"services": services},
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("service_name", "user", "init"),
    [
        ("https-gateway", "1000:1000", False),
        ("app", "10002:10002", None),
    ],
)
def test_runtime_contract_rejects_gateway_without_init_and_wrong_config_consumer_user(
    service_name: str, user: str, init: bool | None
) -> None:
    service: dict[str, Any] = {
        "user": user,
        "platform": "linux/amd64",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 1,
        "mem_limit": "1m",
        "cpus": 0.1,
        "restart": "no",
        "stop_grace_period": "1s",
        "networks": list(preflight._SERVICE_NETWORKS[service_name]),
        "secrets": list(preflight._SERVICE_SECRETS[service_name]),
    }
    if init is not None:
        service["init"] = init

    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_runtime_contract(
            {"services": {service_name: service}},
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
@pytest.mark.parametrize("name", [None, "Uppercase", "-leading"])
def test_compose_project_name_rejects_missing_or_noncanonical_names(name: object) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._compose_project_name({"name": name})


@pytest.mark.unit
@pytest.mark.parametrize(
    "definition",
    [
        [],
        {"unexpected": True},
        {"external": "false"},
        {"external": True},
        {"driver": "nfs"},
        {"name": "wrong_rendered_name"},
    ],
)
def test_top_level_volume_definitions_fail_closed(definition: object) -> None:
    volumes: dict[str, object] = {name: {} for name in preflight._TOP_LEVEL_VOLUMES}
    volumes["postgres-data"] = definition

    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_top_level_volumes({"name": "m8-branch-test", "volumes": volumes})


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount",
    [
        {
            "type": "bind",
            "source": "/safe",
            "target": "/target",
            "volume": {},
        },
        {
            "type": "bind",
            "source": "/safe",
            "target": "/target",
            "bind": [],
        },
        {
            "type": "bind",
            "source": "/safe",
            "target": "/target",
            "bind": {"create_host_path": True},
        },
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "bind": {},
        },
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "volume": [],
        },
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "volume": {"unexpected": False},
        },
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "volume": {"nocopy": "false"},
        },
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "unexpected": False,
        },
    ],
)
def test_mount_options_reject_cross_type_or_noncanonical_options(
    mount: dict[str, object],
) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._normalize_volume(mount)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount",
    [
        {"type": "volume", "source": "safe", "target": "/target", "volume": {}},
        {
            "type": "volume",
            "source": "safe",
            "target": "/target",
            "volume": {"nocopy": False},
        },
    ],
)
def test_mount_options_accept_explicit_safe_volume_options(
    mount: dict[str, object],
) -> None:
    assert preflight._normalize_volume(mount) == ("volume", "safe", "/target", False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount",
    [
        "host:/container",
        {"type": "tmpfs", "source": "safe", "target": "/target"},
        {"type": "bind", "source": 1, "target": "/target"},
        {"type": "bind", "source": "/safe", "target": 1},
        {"type": "bind", "source": "/safe", "target": "/target", "read_only": 1},
        {"type": "bind", "source": "", "target": "/target"},
        {"type": "bind", "source": "/safe", "target": "relative"},
        {"type": "bind", "source": "/safe\x00tail", "target": "/target"},
        {"type": "bind", "source": "/safe", "target": "/target\x00tail"},
        {"type": "bind", "source": "/safe", "target": "/target/../escape"},
    ],
)
def test_volume_normalization_rejects_noncanonical_shapes_and_paths(mount: object) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._normalize_volume(mount)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        1,
        "/tmp",  # noqa: S108 - synthetic tmpfs target
        "relative:rw,size=1m",
        "/tmp:",  # noqa: S108 - synthetic tmpfs target
        "/tmp:rw,,size=1m",  # noqa: S108 - synthetic tmpfs target
        "/tmp:rw,rw,size=1m",  # noqa: S108 - synthetic tmpfs target
        "/tmp:ro,size=1m",  # noqa: S108 - synthetic tmpfs target
        "/tmp:rw,noexec",  # noqa: S108 - synthetic tmpfs target
        "/tmp/../escape:rw,size=1m",  # noqa: S108 - synthetic tmpfs target
    ],
)
def test_tmpfs_signature_rejects_ambiguous_or_unsafe_values(value: object) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._tmpfs_signature(value)


@pytest.mark.unit
def test_unknown_bind_source_token_never_matches() -> None:
    assert not preflight._bind_source_matches(
        "/unexpected",
        "$UNKNOWN",
        "/etc/telegram-userbot/config",
        "/opt/telegram-userbot/release",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "compose",
    [
        {"services": None},
        {"services": {"https-gateway": []}, "volumes": {}},
        {"services": {"https-gateway": {"volumes": {}}}, "volumes": {}},
    ],
)
def test_service_volume_validation_rejects_noncanonical_structures(
    compose: dict[str, object],
) -> None:
    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_service_volumes(
            compose,
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
def test_service_volume_validation_rejects_mount_signature_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_SERVICE_VOLUMES",
        {"app": (("bind", "/different-target", True, "$CONFIG"),)},
    )
    monkeypatch.setattr(preflight, "_SERVICE_TMPFS", {"app": ()})
    compose = {
        "name": "m8-branch-test",
        "volumes": {name: {} for name in preflight._TOP_LEVEL_VOLUMES},
        "services": {
            "app": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/etc/telegram-userbot/config",
                        "target": "/actual-target",
                        "read_only": True,
                    }
                ],
                "tmpfs": [],
            }
        },
    }

    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_service_volumes(
            compose,
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
def test_service_volume_validation_rejects_non_list_tmpfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_SERVICE_VOLUMES", {"app": ()})
    monkeypatch.setattr(preflight, "_SERVICE_TMPFS", {"app": ()})
    compose = {
        "name": "m8-branch-test",
        "volumes": {name: {} for name in preflight._TOP_LEVEL_VOLUMES},
        "services": {"app": {"volumes": [], "tmpfs": "/tmp:rw,size=1m"}},  # noqa: S108
    }

    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_service_volumes(
            compose,
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
@pytest.mark.parametrize("service", [[], {"volumes": {}}])
def test_service_volume_validation_rejects_noncanonical_service_or_volume_types(
    monkeypatch: pytest.MonkeyPatch, service: object
) -> None:
    monkeypatch.setattr(preflight, "_SERVICE_VOLUMES", {"app": ()})
    monkeypatch.setattr(preflight, "_SERVICE_TMPFS", {"app": ()})
    compose = {
        "name": "m8-branch-test",
        "volumes": {name: {} for name in preflight._TOP_LEVEL_VOLUMES},
        "services": {"app": service},
    }

    with pytest.raises(DeploymentPreflightError, match=RUNTIME_CONTRACT_ERROR):
        preflight._validate_service_volumes(
            compose,
            "/etc/telegram-userbot/config",
            "/opt/telegram-userbot/release",
        )


@pytest.mark.unit
def test_service_volume_validation_accepts_optional_postgres_bootstrap_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_SERVICE_VOLUMES",
        {"postgres": (("bind", "/ops-state", False, preflight._SOURCE_OPS_STATE),)},
    )
    monkeypatch.setattr(
        preflight,
        "_SERVICE_TMPFS",
        {"postgres": ("/tmp:rw,size=1m",)},  # noqa: S108 - synthetic tmpfs target
    )
    compose = {
        "name": "m8-branch-test",
        "volumes": {name: {} for name in preflight._TOP_LEVEL_VOLUMES},
        "services": {
            "postgres": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/lib/telegram-userbot/ops-state",
                        "target": "/ops-state",
                    },
                    {
                        "type": "bind",
                        "source": "/opt/telegram-userbot/release/deploy/postgres/bootstrap",
                        "target": "/docker-entrypoint-initdb.d",
                        "read_only": True,
                    },
                ],
                "tmpfs": ["/tmp:rw,size=1m"],  # noqa: S108 - synthetic tmpfs target
            }
        },
    }

    preflight._validate_service_volumes(
        compose,
        "/etc/telegram-userbot/config",
        "/opt/telegram-userbot/release",
    )


class UnavailableSourceOperations:
    @staticmethod
    def lstat(_path: str) -> os.stat_result:
        raise OSError("synthetic unavailable source")

    @staticmethod
    def resolve(path: str) -> str:
        return path


class UnsafeSourceOperations:
    @staticmethod
    def lstat(_path: str) -> os.stat_result:
        return directory_metadata(mode=0o750)

    @staticmethod
    def resolve(_path: str) -> str:
        return "/different/source"


class ResolveUnavailableSourceOperations:
    @staticmethod
    def lstat(_path: str) -> os.stat_result:
        return directory_metadata(mode=0o750)

    @staticmethod
    def resolve(_path: str) -> str:
        raise OSError("synthetic resolve failure")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_root", "operations"),
    [
        (Path("relative"), UnsafeSourceOperations()),
        (Path("C:/"), UnsafeSourceOperations()),
        (Path("C:/opt/release"), UnavailableSourceOperations()),
        (Path("C:/opt/release"), ResolveUnavailableSourceOperations()),
        (Path("C:/opt/release"), UnsafeSourceOperations()),
    ],
)
def test_preflight_rejects_invalid_unavailable_or_retargeted_source_roots(
    source_root: Path, operations: object
) -> None:
    inputs = preflight.DeploymentPreflightInputs(
        Path("/etc/telegram-userbot/config/deployment.json"),
        Path("C:/synthetic/compose.json"),
        source_root,
        "/etc/telegram-userbot/secrets",
    )

    with pytest.raises(
        DeploymentPreflightError, match=r"^DEPLOYMENT_PREFLIGHT_SOURCE_ROOT_INVALID$"
    ):
        preflight.validate_deployment_preflight(
            inputs,
            operations=operations,  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_deployment_config_rejects_relative_reference() -> None:
    with pytest.raises(
        DeploymentPreflightError, match=r"^DEPLOYMENT_PREFLIGHT_CONFIG_REFERENCE_INVALID$"
    ):
        preflight._validate_deployment_config(
            Path("deployment.json"), operations=UnsafeSourceOperations()
        )


@pytest.mark.unit
@pytest.mark.parametrize("fail_target", ["directory", "file"])
def test_deployment_config_hides_directory_and_file_access_failures(fail_target: str) -> None:
    config_path = Path("C:/synthetic/config/deployment.json")
    config_directory = config_path.parent.as_posix()
    config_filename = config_path.as_posix()
    fail_path = config_directory if fail_target == "directory" else config_filename

    class SelectivelyUnavailableOperations:
        @staticmethod
        def lstat(path: str) -> os.stat_result:
            if path == fail_path:
                raise OSError("private host path")
            if path == config_directory:
                return directory_metadata()
            return os.stat_result((stat.S_IFREG | 0o640, 0, 0, 1, 0, 10001, 0, 0, 0, 0))

        @staticmethod
        def resolve(path: str) -> str:
            return path

    with pytest.raises(
        DeploymentPreflightError, match=r"^DEPLOYMENT_PREFLIGHT_CONFIG_UNAVAILABLE$"
    ) as raised:
        preflight._validate_deployment_config(
            config_path,
            operations=SelectivelyUnavailableOperations(),
        )

    assert fail_path not in repr(raised.value)
    assert "private host path" not in repr(raised.value)
