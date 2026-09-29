from __future__ import annotations

import json
import os
import stat
from io import StringIO
from pathlib import Path

import pytest

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.config.deployment_preflight import (
    DeploymentPreflightError,
    DeploymentPreflightInputs,
    DeploymentPreflightResult,
    validate_deployment_preflight,
)
from telegram_userbot.platform.config.secrets import SecretFilePolicy
from telegram_userbot.processes.deployment_preflight import run

ROOT = Path(__file__).resolve().parents[2]
SECRET_ROOT = "/etc/telegram-userbot/secrets"  # noqa: S105 - path, not a credential
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


class _SafeRootOperations:
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


class _UnsafeRootOperations(_SafeRootOperations):
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        if path == SECRET_ROOT:
            return os.stat_result((stat.S_IFDIR | 0o750, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return _SafeRootOperations.lstat(path)


class _UnsafeConfigDirectoryOperations(_SafeRootOperations):
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        if path != SECRET_ROOT and Path(path).suffix != ".json":
            return os.stat_result((stat.S_IFDIR | 0o750, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return _SafeRootOperations.lstat(path)


class _UnsafeConfigFileOperations(_SafeRootOperations):
    @staticmethod
    def lstat(path: str) -> os.stat_result:
        if Path(path).suffix == ".json":
            return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 10001, 0, 0, 0, 0))
        return _SafeRootOperations.lstat(path)


def _deployment_files(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    deployment = json.loads(
        (ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")
    )
    deployment.update(
        deployable=True,
        deployment_id="m8-preflight-test",
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
    compose_path = tmp_path / "compose.json"
    compose = json.loads((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    compose["name"] = "m8-preflight-test"
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
        for entry in json.loads((tmp_path / "secret-files.json").read_text(encoding="utf-8"))[
            "files"
        ]
    }
    compose_path.write_text(json.dumps(compose), encoding="utf-8")
    return deployment_path, compose_path, references


@pytest.mark.unit
def test_host_preflight_binds_all_images_and_secret_metadata_without_returning_values(
    tmp_path: Path,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)
    seen: list[tuple[str, SecretFilePolicy]] = []

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        seen.append((path, policy))
        return SensitiveValue(b"opaque-value-never-returned")

    result = validate_deployment_preflight(
        DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
        operations=_SafeRootOperations(),
        secret_reader=reader,
    )

    assert result.deployment_id == "m8-preflight-test"
    assert result.checked_services == 11
    assert result.checked_secrets == 23
    assert len(seen) == 23
    assert all(path.startswith(f"{SECRET_ROOT}/") for path, _policy in seen)
    assert all(policy.enforce_posix for _path, policy in seen)
    assert "opaque" not in repr(result)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret_root", "operations", "code"),
    [
        (
            "etc/telegram-userbot/secrets",
            _SafeRootOperations(),
            "DEPLOYMENT_PREFLIGHT_SECRET_ROOT_MISMATCH",
        ),
        (
            "/etc/telegram-userbot/secrets/../secrets",
            _SafeRootOperations(),
            "DEPLOYMENT_PREFLIGHT_SECRET_ROOT_MISMATCH",
        ),
        (
            SECRET_ROOT,
            _UnsafeRootOperations(),
            "DEPLOYMENT_PREFLIGHT_SECRET_ROOT_UNSAFE",
        ),
    ],
)
def test_host_preflight_rejects_nonexact_or_unsafe_secret_root_without_disclosure(
    tmp_path: Path,
    secret_root: str,
    operations: _SafeRootOperations,
    code: str,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)

    with pytest.raises(DeploymentPreflightError, match=f"^{code}$") as error:
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, secret_root),
            operations=operations,
            secret_reader=lambda _path, _policy: SensitiveValue(b"not-returned"),
        )

    assert str(tmp_path) not in repr(error.value)
    assert secret_root not in repr(error.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "operations",
    [_UnsafeConfigDirectoryOperations(), _UnsafeConfigFileOperations()],
)
def test_host_preflight_rejects_unsafe_config_reader_contract_without_disclosure(
    tmp_path: Path,
    operations: _SafeRootOperations,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)

    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_CONFIG_UNSAFE$",
    ) as error:
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
            operations=operations,
            secret_reader=lambda _path, _policy: SensitiveValue(b"not-returned"),
        )

    assert str(tmp_path) not in repr(error.value)


@pytest.mark.unit
def test_host_preflight_rejects_any_rendered_image_drift_without_disclosing_reference(
    tmp_path: Path,
) -> None:
    deployment_path, compose_path, references = _deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    compose["services"]["worker"]["image"] = "registry.example.org/drift@sha256:" + "f" * 64
    compose_path.write_text(json.dumps(compose), encoding="utf-8")

    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_IMAGE_MISMATCH$",
    ) as error:
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
            operations=_SafeRootOperations(),
            secret_reader=lambda _path, _policy: SensitiveValue(b"not-returned"),
        )

    assert all(reference not in repr(error.value) for reference in references.values())
    assert "registry.example.org/drift" not in repr(error.value)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["wrong-root", "missing", "external"])
def test_host_preflight_rejects_rendered_secret_binding_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    if mutation == "wrong-root":
        compose["secrets"]["redis_password"]["file"] = "/etc/other/redis_password"
    elif mutation == "missing":
        del compose["secrets"]["redis_password"]
    else:
        compose["secrets"]["redis_password"] = {"external": True}
    compose_path.write_text(json.dumps(compose), encoding="utf-8")

    with pytest.raises(
        DeploymentPreflightError,
        match=r"^DEPLOYMENT_PREFLIGHT_SECRET_BINDING_MISMATCH$",
    ):
        validate_deployment_preflight(
            DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
            operations=_SafeRootOperations(),
            secret_reader=lambda _path, _policy: SensitiveValue(b"not-returned"),
        )


@pytest.mark.unit
def test_host_preflight_accepts_compose_generated_names_and_steady_bootstrap_pruning(
    tmp_path: Path,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    del compose["secrets"]["postgres_database_password"]
    for secret_id, binding in compose["secrets"].items():
        binding["name"] = f"m8-preflight-test_{secret_id}"
    compose_path.write_text(json.dumps(compose), encoding="utf-8")

    result = validate_deployment_preflight(
        DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT),
        operations=_SafeRootOperations(),
        secret_reader=lambda _path, _policy: SensitiveValue(b"not-returned"),
    )

    assert result.checked_secrets == 23


@pytest.mark.unit
def test_preflight_cli_requires_secret_root_and_emits_only_stable_code(tmp_path: Path) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)
    output = StringIO()
    errors = StringIO()

    result = run(
        [
            "--deployment-config",
            str(deployment_path),
            "--compose-config",
            str(compose_path),
            "--source-root",
            str(ROOT),
        ],
        values={},
        stdout=output,
        stderr=errors,
    )

    assert result == 2
    assert output.getvalue() == ""
    assert errors.getvalue() == (
        "DEPLOYMENT_PREFLIGHT_FAILED:DEPLOYMENT_PREFLIGHT_SECRET_ROOT_MISSING\n"
    )
    assert str(tmp_path) not in errors.getvalue()


@pytest.mark.unit
def test_preflight_cli_passes_explicit_secret_root_and_emits_stable_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment_path, compose_path, _references = _deployment_files(tmp_path)
    output = StringIO()
    errors = StringIO()
    seen: list[DeploymentPreflightInputs] = []

    def validate(inputs: DeploymentPreflightInputs) -> DeploymentPreflightResult:
        seen.append(inputs)
        return DeploymentPreflightResult("m8-preflight-test", 10, 20)

    monkeypatch.setattr(
        "telegram_userbot.processes.deployment_preflight.validate_deployment_preflight",
        validate,
    )

    result = run(
        [
            "--deployment-config",
            str(deployment_path),
            "--compose-config",
            str(compose_path),
            "--source-root",
            str(ROOT),
        ],
        values={"SECRET_ROOT": SECRET_ROOT},
        stdout=output,
        stderr=errors,
    )

    assert result == 0
    assert seen == [DeploymentPreflightInputs(deployment_path, compose_path, ROOT, SECRET_ROOT)]
    assert output.getvalue() == "DEPLOYMENT_PREFLIGHT_PASS:services=10:secrets=20\n"
    assert errors.getvalue() == ""
    assert str(tmp_path) not in output.getvalue()


@pytest.mark.unit
def test_install_runbook_passes_only_a_canonical_release_root_to_preflight() -> None:
    runbook = (ROOT / "docs/runbooks/m8-install.md").read_text(encoding="utf-8")

    assert 'release_root="$(readlink -e -- /opt/telegram-userbot/current)"' in runbook
    assert 'cd -- "$release_root"' in runbook
    assert 'PYTHONPATH="$release_root/src"' in runbook
    assert '--source-root "$release_root"' in runbook
    assert "preflight intentionally rejects a symlink" in runbook
    assert "PYTHONPATH=/opt/telegram-userbot/current/src" not in runbook
    assert "--source-root /opt/telegram-userbot/current" not in runbook
