from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION
from telegram_userbot.platform.config.production import (
    ProductionConfigurationError,
    ProductionProcess,
    ProductionSettings,
    SessionProvisioningMode,
)
from telegram_userbot.platform.config.secrets import SecretFilePolicy

ROOT = Path(__file__).resolve().parents[3]
SOURCE_COMMIT = "a" * 40
PUBLIC_HOST = "keys.example.net"

DATABASE_IDENTITIES = {
    ProductionProcess.APP: (
        "telegram_userbot_app_login",
        "telegram_userbot_app_runtime",
    ),
    ProductionProcess.CONTROL: (
        "telegram_userbot_control_login",
        "telegram_userbot_control_runtime",
    ),
    ProductionProcess.WORKER: (
        "telegram_userbot_worker_login",
        "telegram_userbot_worker_runtime",
    ),
    ProductionProcess.MIGRATE: (
        "telegram_userbot_migrator_login",
        "telegram_userbot_migrator",
    ),
}


def _deployable_manifest() -> dict[str, object]:
    manifest = cast(
        dict[str, object],
        json.loads((ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")),
    )
    manifest["deployable"] = True
    manifest["deployment_id"] = "prod-primary"
    manifest["source_commit"] = SOURCE_COMMIT
    manifest["public_host"] = PUBLIC_HOST
    images = manifest["images"]
    assert isinstance(images, dict)
    for index, artifact in enumerate(images.values(), start=1):
        assert isinstance(artifact, dict)
        artifact["status"] = "BUILT"
        artifact["reference"] = (
            f"registry.example/telegram-userbot-{index}@sha256:" + f"{index:x}" * 64
        )
    session_backup = cast(dict[str, object], images["session-backup"])
    data_export = cast(dict[str, object], images["data-export"])
    data_export["reference"] = session_backup["reference"]
    return manifest


def _write_manifests(
    tmp_path: Path,
    *,
    deployment_mutator: Callable[[dict[str, object]], None] | None = None,
    secret_mutator: Callable[[dict[str, object]], None] | None = None,
) -> Path:
    deployment = _deployable_manifest()
    secrets = json.loads((ROOT / "deploy/config/secret-files.json").read_text(encoding="utf-8"))
    if deployment_mutator is not None:
        deployment_mutator(deployment)
    if secret_mutator is not None:
        secret_mutator(secrets)
    config_file = tmp_path / "deployment.json"
    config_file.write_text(json.dumps(deployment), encoding="utf-8")
    (tmp_path / "secret-files.json").write_text(json.dumps(secrets), encoding="utf-8")
    return config_file


def _environment(config_file: Path, process: ProductionProcess) -> dict[str, str]:
    login_role, runtime_role = DATABASE_IDENTITIES[process]
    values = {
        "APP_ENV": "production",
        "CONFIG_FILE": str(config_file),
        "DEPLOYMENT_ID": "prod-primary",
        "SOURCE_COMMIT": SOURCE_COMMIT,
        "PUBLIC_HOST": PUBLIC_HOST,
        "TZ": "UTC",
        "BOOTSTRAP_MAINTENANCE": "1",
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "telegram_userbot",
        "DATABASE_USER": login_role,
        "DATABASE_RUNTIME_ROLE": runtime_role,
        "DATABASE_PASSWORD_FILE": {
            ProductionProcess.APP: "/run/secrets/app_database_password",
            ProductionProcess.CONTROL: "/run/secrets/control_database_password",
            ProductionProcess.WORKER: "/run/secrets/worker_database_password",
            ProductionProcess.MIGRATE: "/run/secrets/migrator_database_password",
        }[process],
    }
    if process is not ProductionProcess.MIGRATE:
        values.update(
            {
                "REDIS_HOST": "redis",
                "REDIS_PORT": "6379",
                "REDIS_PASSWORD_FILE": "/run/secrets/redis_password",
            }
        )
    if process is ProductionProcess.APP:
        values.update(
            {
                "TELEGRAM_API_ID_FILE": "/run/secrets/telegram_api_id",
                "TELEGRAM_API_HASH_FILE": "/run/secrets/telegram_api_hash",
                "CREDENTIAL_KEYRING_FILE": "/run/secrets/credential_master_keyring",
                "ERASURE_HMAC_KEY_FILE": "/run/secrets/erasure_hmac_key",
            }
        )
    elif process is ProductionProcess.CONTROL:
        values.update(
            {
                "PUBLIC_BASE_URL": f"https://{PUBLIC_HOST}",
                "CONTROL_BOT_TOKEN_FILE": "/run/secrets/control_bot_token",
                "CREDENTIAL_KEYRING_FILE": "/run/secrets/credential_master_keyring",
            }
        )
    elif process is ProductionProcess.WORKER:
        values.update(
            {
                "CREDENTIAL_KEYRING_FILE": "/run/secrets/credential_master_keyring",
                "ERASURE_HMAC_KEY_FILE": "/run/secrets/erasure_hmac_key",
                "WORKER_CONCURRENCY": "2",
                "IMAGE_STAGE_CONCURRENCY": "1",
            }
        )
    return values


@pytest.mark.unit
@pytest.mark.parametrize("process", list(ProductionProcess))
def test_production_settings_bind_manifest_endpoints_and_exact_process_secrets(
    tmp_path: Path, process: ProductionProcess
) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(process, _environment(config_file, process))

    assert settings.process is process
    assert settings.deployment.target.ubuntu == "26.04"
    assert settings.deployment.target.vcpu == 2
    assert settings.deployment.target.memory_gib == 4
    assert settings.deployment.target.minimum_disk_gib == 40
    assert settings.deployment.target.validation_disk_gib == 64
    assert settings.deployment.database_compatibility.schema_revision == EXPECTED_SCHEMA_REVISION
    assert settings.deployment.database_compatibility.pgvector_version == "0.8.6"
    assert str(settings.deployment.runtime_identity.account_id) == (
        "01900000-0000-7000-8000-000000000001"
    )
    assert settings.deployment.runtime_identity.telegram_user_id == 1000000001
    assert settings.deployment.runtime_identity.control_bot_user_id == 1000000002
    assert settings.deployment.runtime_identity.control_bot_username == "ExampleControlBot"
    assert settings.deployment.runtime_identity.control_admin_user_ids == (1000000001,)
    assert (
        settings.deployment.startup_policy.session_provisioning
        is SessionProvisioningMode.PREPROVISIONED_ONE_SHOT
    )
    assert settings.deployment.startup_policy.restore_gate_required is True
    assert settings.deployment.startup_policy.app_interactive_login_allowed is False
    assert settings.database.password_secret_id in {item.id for item in settings.secrets}
    assert settings.database.host == "postgres"
    assert settings.database.port == 5432
    assert settings.bootstrap_maintenance is True
    if process is ProductionProcess.MIGRATE:
        assert settings.redis is None
    else:
        assert settings.redis is not None
        assert settings.redis.password_secret_id == "redis_password"  # noqa: S105
    assert settings.safe_log_fields() == {
        "process": process.value,
        "deployment": "configured",
        "source_commit": SOURCE_COMMIT[:12],
        "bootstrap_maintenance": True,
    }


@pytest.mark.unit
def test_secret_loading_uses_fixed_runtime_paths_posix_policy_and_redacted_bundle(
    tmp_path: Path,
) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
    )
    calls: list[tuple[str, SecretFilePolicy]] = []

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        calls.append((path, policy))
        if path.endswith("/app_database_password"):
            return SensitiveValue(b"D" * 40)
        if path.endswith("/redis_password"):
            return SensitiveValue(b"R" * 40)
        if path.endswith("/telegram_api_id"):
            return SensitiveValue(b"123456")
        if path.endswith("/telegram_api_hash"):
            return SensitiveValue(b"a" * 32)
        if path.endswith("/erasure_hmac_key"):
            return SensitiveValue(b"E" * 32)
        return SensitiveValue(b"private-keyring-value")

    bundle = settings.load_secrets(reader=reader)

    assert bundle.ids() == tuple(item.id for item in settings.secrets)
    assert repr(bundle) == "SecretBundle(<redacted>)"
    assert "private-keyring-value" not in repr(bundle)
    assert bundle.get("redis_password").reveal_for_use() == b"R" * 40
    assert {path for path, _ in calls} == {
        f"/run/secrets/{item.filename}" for item in settings.secrets
    }
    for (_, policy), reference in zip(calls, settings.secrets, strict=True):
        assert policy.enforce_posix is True
        assert policy.expected_uid == 0
        assert policy.expected_gid == reference.expected_gid
        assert policy.expected_mode == 0o440
        assert policy.max_bytes == reference.max_bytes


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda value: value.update({"unexpected": True}), "PRODUCTION_DEPLOYMENT_FIELDS_INVALID"),
        (
            lambda value: value["target"].update({"vcpu": 3}),
            "PRODUCTION_TARGET_INVALID",
        ),
        (
            lambda value: value["images"]["application"].update({"status": "NOT_BUILT"}),
            "PRODUCTION_ARTIFACT_NOT_BUILT",
        ),
        (
            lambda value: value["images"]["data-export"].update(
                {"reference": "registry.example/other-ops@sha256:" + "f" * 64}
            ),
            "PRODUCTION_OPERATIONS_IMAGE_MISMATCH",
        ),
        (
            lambda value: cast(dict[str, object], value["database_compatibility"]).update(
                {"schema_revision": "0024_runtime_fencing_provenance"}
            ),
            "PRODUCTION_DATABASE_COMPATIBILITY_INVALID",
        ),
        (
            lambda value: value.update({"timezone": "Mars/Olympus"}),
            "PRODUCTION_TIMEZONE_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["runtime_identity"]).update(
                {"control_bot_user_id": 1000000001}
            ),
            "PRODUCTION_RUNTIME_IDENTITY_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["runtime_identity"]).update(
                {"control_admin_user_ids": [42, 42]}
            ),
            "PRODUCTION_RUNTIME_IDENTITY_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["runtime_identity"]).update(
                {"control_bot_username": "4invalidbot"}
            ),
            "PRODUCTION_RUNTIME_IDENTITY_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["runtime_identity"]).update(
                {"control_bot_username": "ValidUsername"}
            ),
            "PRODUCTION_RUNTIME_IDENTITY_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["startup_policy"]).update(
                {"session_provisioning": "interactive_app_login"}
            ),
            "PRODUCTION_STARTUP_POLICY_INVALID",
        ),
        (
            lambda value: value.update({"public_host": "localhost"}),
            "PRODUCTION_PUBLIC_HOST_INVALID",
        ),
        (
            lambda value: value.update({"deployable": False}),
            "PRODUCTION_DEPLOYMENT_NOT_DEPLOYABLE",
        ),
    ],
)
def test_invalid_deployment_is_rejected_with_stable_code(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], None],
    code: str,
) -> None:
    config_file = _write_manifests(tmp_path, deployment_mutator=mutator)
    values = _environment(config_file, ProductionProcess.APP)

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$") as error:
        ProductionSettings.load(ProductionProcess.APP, values)

    assert str(config_file) not in str(error.value)


@pytest.mark.unit
def test_unknown_or_mismatched_environment_is_rejected_without_value_echo(
    tmp_path: Path,
) -> None:
    config_file = _write_manifests(tmp_path)
    values = _environment(config_file, ProductionProcess.APP)
    private_value = "postgresql://user:SYNTHETIC_PRIVATE_VALUE@db/app"
    values["DATABASE_URL"] = private_value

    with pytest.raises(ProductionConfigurationError, match=r"^PRODUCTION_ENV_UNKNOWN$") as error:
        ProductionSettings.load(ProductionProcess.APP, values)

    assert private_value not in str(error.value)
    assert private_value not in repr(error.value)


@pytest.mark.unit
def test_runtime_app_rejects_interactive_login_inputs(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    values = _environment(config_file, ProductionProcess.APP)
    values["TELEGRAM_PHONE"] = "+10000000000"

    with pytest.raises(ProductionConfigurationError, match=r"^PRODUCTION_ENV_UNKNOWN$"):
        ProductionSettings.load(ProductionProcess.APP, values)


@pytest.mark.unit
def test_secret_manifest_consumers_are_an_exact_contract(tmp_path: Path) -> None:
    def add_wrong_consumer(value: dict[str, object]) -> None:
        files = value["files"]
        assert isinstance(files, list)
        first = files[0]
        assert isinstance(first, dict)
        first["services"] = ["app", "control"]

    config_file = _write_manifests(tmp_path, secret_mutator=add_wrong_consumer)

    with pytest.raises(
        ProductionConfigurationError, match=r"^PRODUCTION_SECRET_CONTRACT_MISMATCH$"
    ):
        ProductionSettings.load(
            ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
        )


@pytest.mark.unit
def test_secret_manifest_is_resolved_only_beside_deployment_config(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    moved_directory = tmp_path / "elsewhere"
    moved_directory.mkdir()
    (tmp_path / "secret-files.json").replace(moved_directory / "secret-files.json")

    with pytest.raises(
        ProductionConfigurationError, match=r"^PRODUCTION_CONFIG_UNREADABLE$"
    ) as error:
        ProductionSettings.load(
            ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
        )

    assert str(moved_directory) not in str(error.value)
    assert str(config_file) not in str(error.value)


@pytest.mark.unit
def test_redis_secret_has_fixed_url_safe_grammar(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.CONTROL, _environment(config_file, ProductionProcess.CONTROL)
    )
    private_value = b"invalid password with spaces"

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del policy
        if path.endswith("/redis_password"):
            return SensitiveValue(private_value)
        if path.endswith("/control_bot_token"):
            return SensitiveValue(b"1000000002:" + b"A" * 32)
        if path.endswith("/control_database_password"):
            return SensitiveValue(b"D" * 40)
        if path.endswith("/export_actor_hmac_key"):
            return SensitiveValue(b"E" * 40)
        return SensitiveValue(b"x" * 40)

    with pytest.raises(
        ProductionConfigurationError, match=r"^PRODUCTION_REDIS_SECRET_INVALID$"
    ) as error:
        settings.load_secrets(reader=reader)

    assert private_value.decode() not in str(error.value)
    assert private_value.decode() not in repr(error.value)


@pytest.mark.unit
def test_control_bot_token_is_bound_to_declared_bot_identity(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.CONTROL, _environment(config_file, ProductionProcess.CONTROL)
    )

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del policy
        if path.endswith("/control_bot_token"):
            return SensitiveValue(b"9999999999:" + b"A" * 32)
        if path.endswith("/redis_password"):
            return SensitiveValue(b"R" * 40)
        if path.endswith("/control_database_password"):
            return SensitiveValue(b"D" * 40)
        return SensitiveValue(b"K" * 40)

    with pytest.raises(
        ProductionConfigurationError, match=r"^PRODUCTION_CONTROL_IDENTITY_MISMATCH$"
    ):
        settings.load_secrets(reader=reader)


@pytest.mark.unit
def test_database_password_uses_same_url_safe_grammar_as_bootstrap(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.MIGRATE, _environment(config_file, ProductionProcess.MIGRATE)
    )
    private_value = b"short"

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del path, policy
        return SensitiveValue(private_value)

    with pytest.raises(
        ProductionConfigurationError, match=r"^PRODUCTION_DATABASE_SECRET_INVALID$"
    ) as error:
        settings.load_secrets(reader=reader)

    assert private_value.decode() not in str(error.value)
    assert private_value.decode() not in repr(error.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda values: values.pop("APP_ENV"), "PRODUCTION_ENV_REQUIRED"),
        (lambda values: values.update(APP_ENV="development"), "PRODUCTION_ENV_INVALID"),
        (
            lambda values: values.update(CONFIG_FILE="deployment.json"),
            "PRODUCTION_CONFIG_REFERENCE_INVALID",
        ),
        (
            lambda values: values.update(BOOTSTRAP_MAINTENANCE="sometimes"),
            "PRODUCTION_ENV_INVALID",
        ),
        (
            lambda values: values.update(DATABASE_PORT="not-a-port"),
            "PRODUCTION_ENDPOINT_INVALID",
        ),
        (
            lambda values: values.update(DATABASE_PORT="65536"),
            "PRODUCTION_ENDPOINT_INVALID",
        ),
        (
            lambda values: values.update(DATABASE_HOST="postgres/service"),
            "PRODUCTION_DATABASE_ENDPOINT_INVALID",
        ),
        (
            lambda values: values.update(DATABASE_RUNTIME_ROLE="telegram_userbot_worker_runtime"),
            "PRODUCTION_DATABASE_ENDPOINT_INVALID",
        ),
        (
            lambda values: values.update(REDIS_HOST="redis_service"),
            "PRODUCTION_REDIS_ENDPOINT_INVALID",
        ),
        (
            lambda values: values.update(
                CONTROL_BOT_TOKEN_FILE="/run/secrets/control_bot_token"  # noqa: S106
            ),
            "PRODUCTION_PROCESS_ENV_MISMATCH",
        ),
        (
            lambda values: values.update(DEPLOYMENT_ID="another-deployment"),
            "PRODUCTION_DEPLOYMENT_BINDING_MISMATCH",
        ),
        (
            lambda values: values.update(
                REDIS_PASSWORD_FILE="/run/secrets/not-the-manifest-file"  # noqa: S106
            ),
            "PRODUCTION_SECRET_REFERENCE_MISMATCH",
        ),
    ],
)
def test_common_environment_failures_are_rejected_before_adapter_composition(
    tmp_path: Path,
    mutation: Callable[[dict[str, str]], object],
    code: str,
) -> None:
    config_file = _write_manifests(tmp_path)
    values = _environment(config_file, ProductionProcess.APP)
    mutation(values)

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$"):
        ProductionSettings.load(ProductionProcess.APP, values)


@pytest.mark.unit
@pytest.mark.parametrize("field", ["WORKER_CONCURRENCY", "IMAGE_STAGE_CONCURRENCY"])
@pytest.mark.parametrize("value", ["not-an-integer", "0"])
def test_worker_resource_limits_are_strict_bounded_integers(
    tmp_path: Path, field: str, value: str
) -> None:
    config_file = _write_manifests(tmp_path)
    values = _environment(config_file, ProductionProcess.WORKER)
    values[field] = value

    with pytest.raises(ProductionConfigurationError, match=r"^PRODUCTION_RESOURCE_INVALID$"):
        ProductionSettings.load(ProductionProcess.WORKER, values)


@pytest.mark.unit
def test_control_public_origin_must_exactly_match_the_deployment_host(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    values = _environment(config_file, ProductionProcess.CONTROL)
    values["PUBLIC_BASE_URL"] = "https://other.example.net"

    with pytest.raises(ProductionConfigurationError, match=r"^PRODUCTION_PUBLIC_ORIGIN_MISMATCH$"):
        ProductionSettings.load(ProductionProcess.CONTROL, values)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "PRODUCTION_CONFIG_INVALID"),
        (b"\xff", "PRODUCTION_CONFIG_INVALID"),
        (b"[]", "PRODUCTION_CONFIG_INVALID"),
        (b'{"schema_version":1,"schema_version":1}', "PRODUCTION_CONFIG_DUPLICATE_KEY"),
        (b'{"schema_version":1}\x00', "PRODUCTION_CONFIG_INVALID"),
    ],
)
def test_deployment_file_parser_fails_closed_on_common_corruption(
    tmp_path: Path, content: bytes, code: str
) -> None:
    config_file = _write_manifests(tmp_path)
    config_file.write_bytes(content)

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$"):
        ProductionSettings.load(
            ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda value: value.update({"deployment_id": "placeholder"}),
            "PRODUCTION_DEPLOYMENT_ID_INVALID",
        ),
        (
            lambda value: value.update({"source_commit": "0" * 40}),
            "PRODUCTION_SOURCE_COMMIT_INVALID",
        ),
        (
            lambda value: value.update({"resource_profile": "larger-is-not-the-contract"}),
            "PRODUCTION_RESOURCE_PROFILE_INVALID",
        ),
        (
            lambda value: value.update({"secret_manifest": "../secret-files.json"}),
            "PRODUCTION_SECRET_MANIFEST_REFERENCE_INVALID",
        ),
        (
            lambda value: cast(
                dict[str, object], cast(dict[str, object], value["images"])["application"]
            ).update({"status": "UNKNOWN"}),
            "PRODUCTION_ARTIFACT_INVALID",
        ),
        (
            lambda value: cast(
                dict[str, object], cast(dict[str, object], value["images"])["application"]
            ).update({"reference": "registry.example/application:latest"}),
            "PRODUCTION_ARTIFACT_INVALID",
        ),
        (
            lambda value: cast(dict[str, object], value["target"]).update({"vcpu": True}),
            "PRODUCTION_TARGET_INVALID",
        ),
    ],
)
def test_release_identity_and_artifact_drift_fail_closed(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], None],
    code: str,
) -> None:
    config_file = _write_manifests(tmp_path, deployment_mutator=mutator)

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$"):
        ProductionSettings.load(
            ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
        )


def _mutate_first_secret(
    value: dict[str, object], mutation: Callable[[dict[str, object]], None]
) -> None:
    files = value["files"]
    assert isinstance(files, list)
    first = files[0]
    assert isinstance(first, dict)
    mutation(first)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda value: value.update({"files": "not-a-list"}),
            "PRODUCTION_SECRET_MANIFEST_INVALID",
        ),
        (
            lambda value: _mutate_first_secret(value, lambda item: item.update({"mode": "0644"})),
            "PRODUCTION_SECRET_ENTRY_INVALID",
        ),
        (
            lambda value: _mutate_first_secret(
                value, lambda item: item.update({"expected_uid": 1000})
            ),
            "PRODUCTION_SECRET_ENTRY_INVALID",
        ),
        (
            lambda value: _mutate_first_secret(value, lambda item: item.update({"max_bytes": 0})),
            "PRODUCTION_SECRET_ENTRY_INVALID",
        ),
        (
            lambda value: cast(list[object], value["files"]).pop(),
            "PRODUCTION_SECRET_CONTRACT_MISMATCH",
        ),
    ],
)
def test_secret_manifest_shape_and_file_policy_drift_fail_closed(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], object],
    code: str,
) -> None:
    def apply_mutation(value: dict[str, object]) -> None:
        mutator(value)

    config_file = _write_manifests(tmp_path, secret_mutator=apply_mutation)

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$"):
        ProductionSettings.load(
            ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
        )


def _valid_secret_bytes(filename: str) -> bytes:
    values = {
        "app_database_password": b"D" * 40,
        "credential_master_keyring": b"private-keyring-value",
        "erasure_hmac_key": b"E" * 32,
        "redis_password": b"R" * 40,
        "telegram_api_hash": b"a" * 32,
        "telegram_api_id": b"123456",
    }
    return values[filename]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "invalid", "code"),
    [
        ("telegram_api_id", b"01234", "PRODUCTION_TELEGRAM_SECRET_INVALID"),
        ("telegram_api_hash", b"not-a-telegram-hash", "PRODUCTION_TELEGRAM_SECRET_INVALID"),
        ("erasure_hmac_key", b"too-short", "PRODUCTION_ERASURE_SECRET_INVALID"),
    ],
)
def test_app_secret_grammars_fail_closed(
    tmp_path: Path, target: str, invalid: bytes, code: str
) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
    )

    def reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del policy
        filename = Path(path).name
        return SensitiveValue(invalid if filename == target else _valid_secret_bytes(filename))

    with pytest.raises(ProductionConfigurationError, match=f"^{code}$"):
        settings.load_secrets(reader=reader)


@pytest.mark.unit
def test_secret_reader_contract_and_bundle_lookup_fail_closed(tmp_path: Path) -> None:
    config_file = _write_manifests(tmp_path)
    settings = ProductionSettings.load(
        ProductionProcess.APP, _environment(config_file, ProductionProcess.APP)
    )

    def invalid_reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del path, policy
        return cast(SensitiveValue[bytes], b"not-a-sensitive-value")

    with pytest.raises(ProductionConfigurationError, match=r"^PRODUCTION_SECRET_READER_INVALID$"):
        settings.load_secrets(reader=invalid_reader)

    def valid_reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
        del policy
        return SensitiveValue(_valid_secret_bytes(Path(path).name))

    bundle = settings.load_secrets(reader=valid_reader)
    with pytest.raises(KeyError, match="not owned"):
        bundle.get("control_bot_token")
