"""Fail-closed, content-free production configuration composition.

This module deliberately does not import adapters.  It validates the deployment and
secret manifests, exposes neutral endpoint values, and loads only the secret files owned
by the selected process.  Adapter-specific connection objects are built at a later
composition boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import RFC_4122, UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.compatibility import (
    EXPECTED_PGVECTOR_VERSION,
    EXPECTED_SCHEMA_REVISION,
    RESOURCE_PROFILE,
)
from telegram_userbot.platform.config.secrets import SecretFilePolicy, read_secret_file

type JsonObject = dict[str, object]

_MAX_CONFIG_BYTES = 256 * 1024
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_REFERENCE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")
_DEPLOYMENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_INTERNAL_HOST = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_PUBLIC_HOST = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
_DATABASE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SECRET_ID = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SECRET_FILENAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_URL_SAFE_PASSWORD = re.compile(rb"[A-Za-z0-9._~-]{32,128}\Z")
_TELEGRAM_API_ID = re.compile(rb"[1-9][0-9]{4,19}\Z")
_TELEGRAM_API_HASH = re.compile(rb"[0-9a-fA-F]{32}\Z")
_CONTROL_BOT_TOKEN = re.compile(rb"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,100}\Z")
_CONTROL_BOT_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*[Bb][Oo][Tt]\Z")
_PLACEHOLDERS = frozenset(
    {"changeme", "change-me", "default", "example", "example-not-deployable", "placeholder"}
)
_ZERO_COMMIT = "0" * 40
_ZERO_DIGEST_SUFFIX = "@sha256:" + "0" * 64
_SECRET_MANIFEST_FILENAME = "secret-files.json"  # noqa: S105 - non-secret filename
_SECRET_SOURCE_ROOT = "/etc/telegram-userbot/secrets"  # noqa: S105 - directory, not a secret
_ARTIFACT_NAMES = (
    "application",
    "database",
    "gateway",
    "redis",
    "session-backup",
    "data-export",
)
_DATABASE_PASSWORD_IDS = frozenset(
    {
        "app_database_password",
        "control_database_password",
        "worker_database_password",
        "migrator_database_password",
        "postgres_database_password",
        "export_database_password",
        "monitor_database_password",
    }
)


class ProductionConfigurationError(ValueError):
    """Stable production configuration failure that never contains rejected data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductionProcess(StrEnum):
    APP = "app"
    CONTROL = "control"
    WORKER = "worker"
    MIGRATE = "migrate"


class ArtifactStatus(StrEnum):
    BUILT = "BUILT"
    NOT_BUILT = "NOT_BUILT"


class SessionProvisioningMode(StrEnum):
    """The production app consumes an already-authorized Session only."""

    PREPROVISIONED_ONE_SHOT = "preprovisioned_one_shot"


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    status: ArtifactStatus
    reference: str


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    ubuntu: str
    os: str
    architecture: str
    vcpu: int
    memory_gib: int
    minimum_disk_gib: int
    validation_disk_gib: int


@dataclass(frozen=True, slots=True)
class DatabaseCompatibility:
    schema_revision: str
    pgvector_version: str


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Stable, non-secret identities bound to one deployment."""

    account_id: UUID
    telegram_user_id: int
    control_bot_user_id: int
    control_bot_username: str
    control_admin_user_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StartupPolicy:
    """Fail-closed startup contract; the mutable restore state lives in PostgreSQL."""

    session_provisioning: SessionProvisioningMode
    restore_gate_required: bool

    @property
    def app_interactive_login_allowed(self) -> bool:
        """Production app login is never interactive; authorization is one-shot only."""

        return False


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    deployment_id: str
    source_commit: str
    public_host: str
    timezone: str
    target: DeploymentTarget
    database_compatibility: DatabaseCompatibility
    runtime_identity: RuntimeIdentity
    startup_policy: StartupPolicy
    artifacts: tuple[tuple[str, ImageArtifact], ...]
    secret_manifest: str

    def artifact(self, name: str) -> ImageArtifact:
        for artifact_name, artifact in self.artifacts:
            if artifact_name == name:
                return artifact
        raise ProductionConfigurationError("PRODUCTION_ARTIFACT_MISSING")


@dataclass(frozen=True, slots=True)
class DeploymentContract:
    """Deployment and complete content-free secret inventory for host preflight."""

    deployment: DeploymentManifest
    secret_source_root: str
    secrets: tuple[SecretReference, ...]


@dataclass(frozen=True, slots=True)
class SecretReference:
    id: str
    filename: str
    expected_uid: int
    expected_gid: int
    expected_mode: int
    max_bytes: int

    def policy(self) -> SecretFilePolicy:
        return SecretFilePolicy(
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
            expected_mode=self.expected_mode,
            max_bytes=self.max_bytes,
            enforce_posix=True,
        )


@dataclass(frozen=True, slots=True)
class DatabaseEndpoint:
    host: str
    port: int
    database: str
    login_role: str
    runtime_role: str
    password_secret_id: str
    sslmode: str


@dataclass(frozen=True, slots=True)
class RedisEndpoint:
    host: str
    port: int
    password_secret_id: str


@dataclass(frozen=True, slots=True, repr=False)
class SecretBundle:
    _items: tuple[tuple[str, SensitiveValue[bytes]], ...]

    def get(self, secret_id: str) -> SensitiveValue[bytes]:
        for item_id, value in self._items:
            if item_id == secret_id:
                return value
        raise KeyError("secret id is not owned by this process")

    def ids(self) -> tuple[str, ...]:
        return tuple(item_id for item_id, _ in self._items)

    def __repr__(self) -> str:
        return "SecretBundle(<redacted>)"


class ProductionSecretReader(Protocol):
    def __call__(self, path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]: ...


_EXPECTED_SECRET_CONSUMERS: dict[str, frozenset[str]] = {
    "telegram_api_id": frozenset({"app"}),
    "telegram_api_hash": frozenset({"app"}),
    "control_bot_token": frozenset({"control"}),
    "credential_master_keyring": frozenset({"app", "control", "worker"}),
    "app_database_password": frozenset({"app", "postgres-bootstrap"}),
    "control_database_password": frozenset({"control", "postgres-bootstrap"}),
    "worker_database_password": frozenset({"worker", "postgres-bootstrap"}),
    "migrator_database_password": frozenset({"migrate", "postgres-bootstrap"}),
    "postgres_database_password": frozenset({"postgres-bootstrap"}),
    "redis_password": frozenset({"app", "control", "worker", "redis"}),
    "erasure_hmac_key": frozenset({"app", "worker", "erasure-ledger-export"}),
    "export_database_password": frozenset(
        {"data-export", "erasure-ledger-export", "postgres-bootstrap"}
    ),
    "export_actor_hmac_key": frozenset({"control"}),
    "pgbackrest_s3_access_key": frozenset({"postgres"}),
    "pgbackrest_s3_secret_key": frozenset({"postgres"}),
    "pgbackrest_repo_cipher_pass": frozenset({"postgres"}),
    "session_restic_password": frozenset({"session-backup"}),
    "session_s3_access_key": frozenset({"session-backup"}),
    "session_s3_secret_key": frozenset({"session-backup"}),
    "monitor_database_password": frozenset({"ops-monitor", "postgres-bootstrap"}),
    "erasure_restic_password": frozenset({"erasure-ledger-export"}),
    "erasure_s3_access_key": frozenset({"erasure-ledger-export"}),
    "erasure_s3_secret_key": frozenset({"erasure-ledger-export"}),
}

_PROCESS_SECRETS: dict[ProductionProcess, frozenset[str]] = {
    process: frozenset(
        secret_id
        for secret_id, consumers in _EXPECTED_SECRET_CONSUMERS.items()
        if process.value in consumers
    )
    for process in ProductionProcess
}

_DATABASE_IDENTITIES: dict[ProductionProcess, tuple[str, str, str]] = {
    ProductionProcess.APP: (
        "telegram_userbot_app_login",
        "telegram_userbot_app_runtime",
        "app_database_password",
    ),
    ProductionProcess.CONTROL: (
        "telegram_userbot_control_login",
        "telegram_userbot_control_runtime",
        "control_database_password",
    ),
    ProductionProcess.WORKER: (
        "telegram_userbot_worker_login",
        "telegram_userbot_worker_runtime",
        "worker_database_password",
    ),
    ProductionProcess.MIGRATE: (
        "telegram_userbot_migrator_login",
        "telegram_userbot_migrator",
        "migrator_database_password",
    ),
}

_SECRET_FILE_ENV: dict[str, str] = {
    "telegram_api_id": "TELEGRAM_API_ID_FILE",
    "telegram_api_hash": "TELEGRAM_API_HASH_FILE",
    "control_bot_token": "CONTROL_BOT_TOKEN_FILE",
    "credential_master_keyring": "CREDENTIAL_KEYRING_FILE",
    "app_database_password": "DATABASE_PASSWORD_FILE",
    "control_database_password": "DATABASE_PASSWORD_FILE",
    "worker_database_password": "DATABASE_PASSWORD_FILE",
    "migrator_database_password": "DATABASE_PASSWORD_FILE",
    "redis_password": "REDIS_PASSWORD_FILE",
    "erasure_hmac_key": "ERASURE_HMAC_KEY_FILE",
    "export_actor_hmac_key": "EXPORT_ACTOR_HMAC_KEY_FILE",
}

_COMMON_ENV_KEYS = frozenset(
    {
        "APP_ENV",
        "BOOTSTRAP_MAINTENANCE",
        "CONFIG_FILE",
        "DEPLOYMENT_ID",
        "PUBLIC_HOST",
        "SOURCE_COMMIT",
        "TZ",
        "DATABASE_HOST",
        "DATABASE_PORT",
        "DATABASE_NAME",
        "DATABASE_USER",
        "DATABASE_RUNTIME_ROLE",
        "DATABASE_PASSWORD_FILE",
        "DATABASE_SSLMODE",
    }
)
_KNOWN_ENV_KEYS = _COMMON_ENV_KEYS | frozenset(
    {
        "PUBLIC_BASE_URL",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD_FILE",
        "TELEGRAM_API_ID_FILE",
        "TELEGRAM_API_HASH_FILE",
        "CONTROL_BOT_TOKEN_FILE",
        "CREDENTIAL_KEYRING_FILE",
        "ERASURE_HMAC_KEY_FILE",
        "EXPORT_ACTOR_HMAC_KEY_FILE",
        "WORKER_CONCURRENCY",
        "IMAGE_STAGE_CONCURRENCY",
    }
)
_OWNED_ENV_PREFIXES = (
    "APP_",
    "DATABASE_",
    "REDIS_",
    "TELEGRAM_",
    "CONTROL_",
    "CREDENTIAL_",
    "ERASURE_",
    "PUBLIC_",
    "WORKER_",
    "IMAGE_STAGE_",
)
_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})


def _fail(code: str) -> ProductionConfigurationError:
    return ProductionConfigurationError(code)


def _unique_json_object(pairs: list[tuple[str, object]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise _fail("PRODUCTION_CONFIG_DUPLICATE_KEY")
        result[key] = value
    return result


def _read_json(path: Path) -> JsonObject:
    try:
        with path.open("rb") as stream:
            content = stream.read(_MAX_CONFIG_BYTES + 1)
    except OSError:
        raise _fail("PRODUCTION_CONFIG_UNREADABLE") from None
    if not content or len(content) > _MAX_CONFIG_BYTES or b"\x00" in content:
        raise _fail("PRODUCTION_CONFIG_INVALID")
    try:
        value = json.loads(content, object_pairs_hook=_unique_json_object)
    except ProductionConfigurationError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError:
        raise _fail("PRODUCTION_CONFIG_INVALID") from None
    if not isinstance(value, dict):
        raise _fail("PRODUCTION_CONFIG_INVALID")
    return value


def _object(value: object, *, code: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _fail(code)
    return value


def _exact_keys(value: JsonObject, expected: frozenset[str], *, code: str) -> None:
    if frozenset(value) != expected:
        raise _fail(code)


def _string(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(code)
    return value


def _integer(value: object, *, code: str) -> int:
    if type(value) is not int:
        raise _fail(code)
    return value


def _positive_telegram_id(value: object, *, code: str) -> int:
    result = _integer(value, code=code)
    if not 1 <= result <= 2**63 - 1:
        raise _fail(code)
    return result


def _canonical_uuid(value: object, *, code: str) -> UUID:
    raw = _string(value, code=code)
    try:
        parsed = UUID(raw)
    except ValueError:
        raise _fail(code) from None
    if (
        parsed.int == 0
        or str(parsed) != raw
        or parsed.variant != RFC_4122
        or parsed.version not in range(1, 9)
    ):
        raise _fail(code)
    return parsed


def _parse_runtime_identity(value: object) -> RuntimeIdentity:
    identity = _object(value, code="PRODUCTION_RUNTIME_IDENTITY_INVALID")
    _exact_keys(
        identity,
        frozenset(
            {
                "account_id",
                "telegram_user_id",
                "control_bot_user_id",
                "control_bot_username",
                "control_admin_user_ids",
            }
        ),
        code="PRODUCTION_RUNTIME_IDENTITY_INVALID",
    )
    telegram_user_id = _positive_telegram_id(
        identity["telegram_user_id"], code="PRODUCTION_RUNTIME_IDENTITY_INVALID"
    )
    control_bot_user_id = _positive_telegram_id(
        identity["control_bot_user_id"], code="PRODUCTION_RUNTIME_IDENTITY_INVALID"
    )
    control_bot_username = _string(
        identity["control_bot_username"], code="PRODUCTION_RUNTIME_IDENTITY_INVALID"
    )
    if (
        not 5 <= len(control_bot_username) <= 32
        or _CONTROL_BOT_USERNAME.fullmatch(control_bot_username) is None
    ):
        raise _fail("PRODUCTION_RUNTIME_IDENTITY_INVALID")
    raw_admin_ids = identity["control_admin_user_ids"]
    if not isinstance(raw_admin_ids, list) or not 1 <= len(raw_admin_ids) <= 16:
        raise _fail("PRODUCTION_RUNTIME_IDENTITY_INVALID")
    admin_ids = tuple(
        _positive_telegram_id(item, code="PRODUCTION_RUNTIME_IDENTITY_INVALID")
        for item in raw_admin_ids
    )
    if (
        len(set(admin_ids)) != len(admin_ids)
        or control_bot_user_id == telegram_user_id
        or control_bot_user_id in admin_ids
    ):
        raise _fail("PRODUCTION_RUNTIME_IDENTITY_INVALID")
    return RuntimeIdentity(
        account_id=_canonical_uuid(
            identity["account_id"], code="PRODUCTION_RUNTIME_IDENTITY_INVALID"
        ),
        telegram_user_id=telegram_user_id,
        control_bot_user_id=control_bot_user_id,
        control_bot_username=control_bot_username,
        control_admin_user_ids=admin_ids,
    )


def _parse_startup_policy(value: object) -> StartupPolicy:
    policy = _object(value, code="PRODUCTION_STARTUP_POLICY_INVALID")
    _exact_keys(
        policy,
        frozenset({"session_provisioning", "restore_gate_required"}),
        code="PRODUCTION_STARTUP_POLICY_INVALID",
    )
    try:
        session_provisioning = SessionProvisioningMode(
            _string(policy["session_provisioning"], code="PRODUCTION_STARTUP_POLICY_INVALID")
        )
    except ValueError:
        raise _fail("PRODUCTION_STARTUP_POLICY_INVALID") from None
    if policy["restore_gate_required"] is not True:
        raise _fail("PRODUCTION_STARTUP_POLICY_INVALID")
    return StartupPolicy(session_provisioning, restore_gate_required=True)


def _validate_public_host(value: str) -> None:
    if (
        _PUBLIC_HOST.fullmatch(value) is None
        or value.endswith(".invalid")
        or value in {"localhost", "example.com"}
    ):
        raise _fail("PRODUCTION_PUBLIC_HOST_INVALID")


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ValueError, ZoneInfoNotFoundError:
        raise _fail("PRODUCTION_TIMEZONE_INVALID") from None


def _parse_artifacts(value: object, *, deployable: bool) -> tuple[tuple[str, ImageArtifact], ...]:
    artifacts = _object(value, code="PRODUCTION_ARTIFACTS_INVALID")
    _exact_keys(artifacts, frozenset(_ARTIFACT_NAMES), code="PRODUCTION_ARTIFACTS_INVALID")
    result: list[tuple[str, ImageArtifact]] = []
    for name in _ARTIFACT_NAMES:
        item = _object(artifacts[name], code="PRODUCTION_ARTIFACT_INVALID")
        _exact_keys(item, frozenset({"status", "reference"}), code="PRODUCTION_ARTIFACT_INVALID")
        try:
            status = ArtifactStatus(_string(item["status"], code="PRODUCTION_ARTIFACT_INVALID"))
        except ValueError:
            raise _fail("PRODUCTION_ARTIFACT_INVALID") from None
        reference = _string(item["reference"], code="PRODUCTION_ARTIFACT_INVALID")
        if _IMAGE_REFERENCE.fullmatch(reference) is None:
            raise _fail("PRODUCTION_ARTIFACT_INVALID")
        if deployable and (
            status is not ArtifactStatus.BUILT or reference.endswith(_ZERO_DIGEST_SUFFIX)
        ):
            raise _fail("PRODUCTION_ARTIFACT_NOT_BUILT")
        result.append((name, ImageArtifact(status, reference)))
    parsed = tuple(result)
    references = {name: artifact.reference for name, artifact in parsed}
    # M8 builds one least-privilege operations image and scopes capabilities with
    # service-specific secrets, mounts, commands, and database roles.  Compose has
    # one OPS_IMAGE input, so accepting divergent manifest references would make a
    # rendered deployment impossible to preflight deterministically.
    if references["session-backup"] != references["data-export"]:
        raise _fail("PRODUCTION_OPERATIONS_IMAGE_MISMATCH")
    return parsed


def _parse_deployment(value: JsonObject) -> DeploymentManifest:
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "deployable",
                "deployment_id",
                "source_commit",
                "target",
                "database_compatibility",
                "runtime_identity",
                "startup_policy",
                "images",
                "public_host",
                "timezone",
                "resource_profile",
                "secret_manifest",
            }
        ),
        code="PRODUCTION_DEPLOYMENT_FIELDS_INVALID",
    )
    if value["schema_version"] != 1 or value["deployable"] is not True:
        raise _fail("PRODUCTION_DEPLOYMENT_NOT_DEPLOYABLE")
    deployment_id = _string(value["deployment_id"], code="PRODUCTION_DEPLOYMENT_ID_INVALID")
    if _DEPLOYMENT_ID.fullmatch(deployment_id) is None or deployment_id in _PLACEHOLDERS:
        raise _fail("PRODUCTION_DEPLOYMENT_ID_INVALID")
    source_commit = _string(value["source_commit"], code="PRODUCTION_SOURCE_COMMIT_INVALID")
    if _SOURCE_COMMIT.fullmatch(source_commit) is None or source_commit == _ZERO_COMMIT:
        raise _fail("PRODUCTION_SOURCE_COMMIT_INVALID")
    public_host = _string(value["public_host"], code="PRODUCTION_PUBLIC_HOST_INVALID")
    _validate_public_host(public_host)
    timezone = _string(value["timezone"], code="PRODUCTION_TIMEZONE_INVALID")
    _validate_timezone(timezone)

    target_value = _object(value["target"], code="PRODUCTION_TARGET_INVALID")
    _exact_keys(
        target_value,
        frozenset(
            {
                "ubuntu",
                "os",
                "architecture",
                "vcpu",
                "memory_gib",
                "minimum_disk_gib",
                "validation_disk_gib",
            }
        ),
        code="PRODUCTION_TARGET_INVALID",
    )
    target = DeploymentTarget(
        ubuntu=_string(target_value["ubuntu"], code="PRODUCTION_TARGET_INVALID"),
        os=_string(target_value["os"], code="PRODUCTION_TARGET_INVALID"),
        architecture=_string(target_value["architecture"], code="PRODUCTION_TARGET_INVALID"),
        vcpu=_integer(target_value["vcpu"], code="PRODUCTION_TARGET_INVALID"),
        memory_gib=_integer(target_value["memory_gib"], code="PRODUCTION_TARGET_INVALID"),
        minimum_disk_gib=_integer(
            target_value["minimum_disk_gib"], code="PRODUCTION_TARGET_INVALID"
        ),
        validation_disk_gib=_integer(
            target_value["validation_disk_gib"], code="PRODUCTION_TARGET_INVALID"
        ),
    )
    if target != DeploymentTarget("26.04", "linux", "amd64", 2, 4, 40, 64):
        raise _fail("PRODUCTION_TARGET_INVALID")
    if value["resource_profile"] != RESOURCE_PROFILE:
        raise _fail("PRODUCTION_RESOURCE_PROFILE_INVALID")
    compatibility_value = _object(
        value["database_compatibility"], code="PRODUCTION_DATABASE_COMPATIBILITY_INVALID"
    )
    _exact_keys(
        compatibility_value,
        frozenset({"schema_revision", "pgvector_version"}),
        code="PRODUCTION_DATABASE_COMPATIBILITY_INVALID",
    )
    compatibility = DatabaseCompatibility(
        schema_revision=_string(
            compatibility_value["schema_revision"],
            code="PRODUCTION_DATABASE_COMPATIBILITY_INVALID",
        ),
        pgvector_version=_string(
            compatibility_value["pgvector_version"],
            code="PRODUCTION_DATABASE_COMPATIBILITY_INVALID",
        ),
    )
    if compatibility != DatabaseCompatibility(EXPECTED_SCHEMA_REVISION, EXPECTED_PGVECTOR_VERSION):
        raise _fail("PRODUCTION_DATABASE_COMPATIBILITY_INVALID")
    secret_manifest = _string(
        value["secret_manifest"], code="PRODUCTION_SECRET_MANIFEST_REFERENCE_INVALID"
    )
    if secret_manifest != _SECRET_MANIFEST_FILENAME:
        raise _fail("PRODUCTION_SECRET_MANIFEST_REFERENCE_INVALID")
    return DeploymentManifest(
        deployment_id=deployment_id,
        source_commit=source_commit,
        public_host=public_host,
        timezone=timezone,
        target=target,
        database_compatibility=compatibility,
        runtime_identity=_parse_runtime_identity(value["runtime_identity"]),
        startup_policy=_parse_startup_policy(value["startup_policy"]),
        artifacts=_parse_artifacts(value["images"], deployable=True),
        secret_manifest=secret_manifest,
    )


def _parse_secret_manifest(value: JsonObject) -> tuple[SecretReference, ...]:
    _exact_keys(
        value,
        frozenset({"schema_version", "content_free", "source_root", "files"}),
        code="PRODUCTION_SECRET_MANIFEST_FIELDS_INVALID",
    )
    if (
        value["schema_version"] != 1
        or value["content_free"] is not True
        or value["source_root"] != _SECRET_SOURCE_ROOT
    ):
        raise _fail("PRODUCTION_SECRET_MANIFEST_INVALID")
    files = value["files"]
    if not isinstance(files, list):
        raise _fail("PRODUCTION_SECRET_MANIFEST_INVALID")
    references: list[SecretReference] = []
    consumers_by_id: dict[str, frozenset[str]] = {}
    filenames: set[str] = set()
    for raw_entry in files:
        entry = _object(raw_entry, code="PRODUCTION_SECRET_ENTRY_INVALID")
        _exact_keys(
            entry,
            frozenset(
                {
                    "id",
                    "filename",
                    "services",
                    "expected_uid",
                    "expected_gid",
                    "mode",
                    "max_bytes",
                }
            ),
            code="PRODUCTION_SECRET_ENTRY_INVALID",
        )
        secret_id = _string(entry["id"], code="PRODUCTION_SECRET_ENTRY_INVALID")
        filename = _string(entry["filename"], code="PRODUCTION_SECRET_ENTRY_INVALID")
        services = entry["services"]
        if (
            _SECRET_ID.fullmatch(secret_id) is None
            or _SECRET_FILENAME.fullmatch(filename) is None
            or secret_id in consumers_by_id
            or filename in filenames
            or not isinstance(services, list)
            or not services
            or not all(isinstance(service, str) for service in services)
            or len(set(services)) != len(services)
        ):
            raise _fail("PRODUCTION_SECRET_ENTRY_INVALID")
        consumers = frozenset(services)
        expected_consumers = _EXPECTED_SECRET_CONSUMERS.get(secret_id)
        if expected_consumers is None or consumers != expected_consumers:
            raise _fail("PRODUCTION_SECRET_CONTRACT_MISMATCH")
        mode_value = _string(entry["mode"], code="PRODUCTION_SECRET_ENTRY_INVALID")
        if re.fullmatch(r"0[0-7]{3}", mode_value) is None:
            raise _fail("PRODUCTION_SECRET_ENTRY_INVALID")
        reference = SecretReference(
            id=secret_id,
            filename=filename,
            expected_uid=_integer(entry["expected_uid"], code="PRODUCTION_SECRET_ENTRY_INVALID"),
            expected_gid=_integer(entry["expected_gid"], code="PRODUCTION_SECRET_ENTRY_INVALID"),
            expected_mode=int(mode_value, 8),
            max_bytes=_integer(entry["max_bytes"], code="PRODUCTION_SECRET_ENTRY_INVALID"),
        )
        try:
            reference.policy()
        except ValueError:
            raise _fail("PRODUCTION_SECRET_ENTRY_INVALID") from None
        if reference.expected_uid != 0 or reference.expected_gid <= 0:
            raise _fail("PRODUCTION_SECRET_ENTRY_INVALID")
        references.append(reference)
        consumers_by_id[secret_id] = consumers
        filenames.add(filename)
    if consumers_by_id.keys() != _EXPECTED_SECRET_CONSUMERS.keys():
        raise _fail("PRODUCTION_SECRET_CONTRACT_MISMATCH")
    # Keep the reviewed manifest order.  Besides making the reader calls
    # deterministic, this gives foundational transport/database secrets a
    # stable validation priority before optional control-plane secrets.
    return tuple(references)


def load_deployment_contract(config_file: Path) -> DeploymentContract:
    """Load the full content-free host contract without reading any secret value."""

    if not config_file.is_absolute():
        raise _fail("PRODUCTION_CONFIG_REFERENCE_INVALID")
    deployment = _parse_deployment(_read_json(config_file))
    references = _parse_secret_manifest(_read_json(config_file.parent / deployment.secret_manifest))
    return DeploymentContract(deployment, _SECRET_SOURCE_ROOT, references)


def _required_env(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or not value or value != value.strip():
        raise _fail("PRODUCTION_ENV_REQUIRED")
    return value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise _fail("PRODUCTION_ENV_INVALID")


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise _fail("PRODUCTION_ENDPOINT_INVALID") from None
    if not 1 <= port <= 65535:
        raise _fail("PRODUCTION_ENDPOINT_INVALID")
    return port


def _parse_bounded_int(value: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise _fail("PRODUCTION_RESOURCE_INVALID") from None
    if not minimum <= parsed <= maximum:
        raise _fail("PRODUCTION_RESOURCE_INVALID")
    return parsed


def _process_allowed_keys(process: ProductionProcess) -> frozenset[str]:
    keys = set(_COMMON_ENV_KEYS)
    for secret_id in _PROCESS_SECRETS[process]:
        env_key = _SECRET_FILE_ENV.get(secret_id)
        if env_key is not None:
            keys.add(env_key)
    if process is not ProductionProcess.MIGRATE:
        keys.update({"REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD_FILE"})
    if process is ProductionProcess.CONTROL:
        keys.add("PUBLIC_BASE_URL")
    if process is ProductionProcess.WORKER:
        keys.update({"WORKER_CONCURRENCY", "IMAGE_STAGE_CONCURRENCY"})
    return frozenset(keys)


def _validate_environment_keys(process: ProductionProcess, values: Mapping[str, str]) -> None:
    unknown_owned = {
        key
        for key in values
        if any(key.startswith(prefix) for prefix in _OWNED_ENV_PREFIXES)
        and key not in _KNOWN_ENV_KEYS
    }
    if unknown_owned:
        raise _fail("PRODUCTION_ENV_UNKNOWN")
    unexpected = (frozenset(values) & _KNOWN_ENV_KEYS) - _process_allowed_keys(process)
    if unexpected:
        raise _fail("PRODUCTION_PROCESS_ENV_MISMATCH")


def _database_endpoint(process: ProductionProcess, values: Mapping[str, str]) -> DatabaseEndpoint:
    expected_login, expected_runtime, password_secret_id = _DATABASE_IDENTITIES[process]
    host = _required_env(values, "DATABASE_HOST")
    database = _required_env(values, "DATABASE_NAME")
    login_role = _required_env(values, "DATABASE_USER")
    runtime_role = _required_env(values, "DATABASE_RUNTIME_ROLE")
    sslmode = values.get("DATABASE_SSLMODE", "prefer").strip()
    if (
        _INTERNAL_HOST.fullmatch(host) is None
        or _DATABASE_IDENTIFIER.fullmatch(database) is None
        or login_role != expected_login
        or runtime_role != expected_runtime
        or sslmode not in _SSL_MODES
    ):
        raise _fail("PRODUCTION_DATABASE_ENDPOINT_INVALID")
    return DatabaseEndpoint(
        host=host,
        port=_parse_port(_required_env(values, "DATABASE_PORT")),
        database=database,
        login_role=login_role,
        runtime_role=runtime_role,
        password_secret_id=password_secret_id,
        sslmode=sslmode,
    )


def _redis_endpoint(process: ProductionProcess, values: Mapping[str, str]) -> RedisEndpoint | None:
    if process is ProductionProcess.MIGRATE:
        return None
    host = _required_env(values, "REDIS_HOST")
    if _INTERNAL_HOST.fullmatch(host) is None:
        raise _fail("PRODUCTION_REDIS_ENDPOINT_INVALID")
    return RedisEndpoint(
        host=host,
        port=_parse_port(_required_env(values, "REDIS_PORT")),
        password_secret_id="redis_password",  # noqa: S106 - non-secret manifest id
    )


def _secret_refs_for_process(
    process: ProductionProcess, references: tuple[SecretReference, ...]
) -> tuple[SecretReference, ...]:
    required = _PROCESS_SECRETS[process]
    selected = tuple(reference for reference in references if reference.id in required)
    if frozenset(reference.id for reference in selected) != required:
        raise _fail("PRODUCTION_PROCESS_SECRET_MISMATCH")
    return selected


def _default_secret_reader(path: str, policy: SecretFilePolicy) -> SensitiveValue[bytes]:
    return read_secret_file(path, policy)


def _validate_secret_content(secret_id: str, value: bytes) -> None:
    if secret_id in _DATABASE_PASSWORD_IDS and _URL_SAFE_PASSWORD.fullmatch(value) is None:
        raise _fail("PRODUCTION_DATABASE_SECRET_INVALID")
    if (
        secret_id == "redis_password"  # noqa: S105 - non-secret manifest id
        and _URL_SAFE_PASSWORD.fullmatch(value) is None
    ):
        raise _fail("PRODUCTION_REDIS_SECRET_INVALID")
    if (
        secret_id == "telegram_api_id"  # noqa: S105 - non-secret manifest id
        and _TELEGRAM_API_ID.fullmatch(value) is None
    ):
        raise _fail("PRODUCTION_TELEGRAM_SECRET_INVALID")
    if (
        secret_id == "telegram_api_hash"  # noqa: S105 - non-secret manifest id
        and _TELEGRAM_API_HASH.fullmatch(value) is None
    ):
        raise _fail("PRODUCTION_TELEGRAM_SECRET_INVALID")
    if (
        secret_id == "control_bot_token"  # noqa: S105 - non-secret manifest id
        and _CONTROL_BOT_TOKEN.fullmatch(value) is None
    ):
        raise _fail("PRODUCTION_CONTROL_SECRET_INVALID")
    if secret_id == "erasure_hmac_key" and len(value) < 32:  # noqa: S105
        raise _fail("PRODUCTION_ERASURE_SECRET_INVALID")
    if secret_id == "export_actor_hmac_key" and len(value) != 32:  # noqa: S105
        raise _fail("PRODUCTION_EXPORT_ACTOR_SECRET_INVALID")


def _validate_deployment_binding(
    process: ProductionProcess,
    values: Mapping[str, str],
    deployment: DeploymentManifest,
) -> None:
    if (
        _required_env(values, "DEPLOYMENT_ID") != deployment.deployment_id
        or _required_env(values, "SOURCE_COMMIT") != deployment.source_commit
        or _required_env(values, "PUBLIC_HOST") != deployment.public_host
        or _required_env(values, "TZ") != deployment.timezone
    ):
        raise _fail("PRODUCTION_DEPLOYMENT_BINDING_MISMATCH")
    if (
        process is ProductionProcess.CONTROL
        and _required_env(values, "PUBLIC_BASE_URL") != f"https://{deployment.public_host}"
    ):
        raise _fail("PRODUCTION_PUBLIC_ORIGIN_MISMATCH")


def _validate_secret_environment(
    values: Mapping[str, str], references: tuple[SecretReference, ...]
) -> None:
    for reference in references:
        env_key = _SECRET_FILE_ENV.get(reference.id)
        if env_key is not None and env_key in values:
            expected_path = str(PurePosixPath("/run/secrets") / reference.filename)
            if values[env_key] != expected_path:
                raise _fail("PRODUCTION_SECRET_REFERENCE_MISMATCH")


def _process_resources(process: ProductionProcess, values: Mapping[str, str]) -> tuple[int, int]:
    if process is not ProductionProcess.WORKER:
        return 1, 1
    return (
        _parse_bounded_int(values.get("WORKER_CONCURRENCY", "2"), minimum=1, maximum=2),
        _parse_bounded_int(values.get("IMAGE_STAGE_CONCURRENCY", "1"), minimum=1, maximum=1),
    )


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    process: ProductionProcess
    deployment: DeploymentManifest
    bootstrap_maintenance: bool
    database: DatabaseEndpoint
    redis: RedisEndpoint | None
    secrets: tuple[SecretReference, ...]
    worker_concurrency: int
    image_stage_concurrency: int

    @classmethod
    def load(
        cls,
        process: ProductionProcess,
        values: Mapping[str, str],
    ) -> ProductionSettings:
        if not isinstance(process, ProductionProcess):
            raise _fail("PRODUCTION_PROCESS_INVALID")
        _validate_environment_keys(process, values)
        if _required_env(values, "APP_ENV") != "production":
            raise _fail("PRODUCTION_ENV_INVALID")
        config_file = Path(_required_env(values, "CONFIG_FILE"))
        if not config_file.is_absolute():
            raise _fail("PRODUCTION_CONFIG_REFERENCE_INVALID")
        contract = load_deployment_contract(config_file)
        deployment = contract.deployment
        _validate_deployment_binding(process, values, deployment)
        references = contract.secrets
        process_refs = _secret_refs_for_process(process, references)
        _validate_secret_environment(values, process_refs)

        database = _database_endpoint(process, values)
        redis = _redis_endpoint(process, values)
        if database.password_secret_id not in {item.id for item in process_refs}:
            raise _fail("PRODUCTION_PROCESS_SECRET_MISMATCH")
        if redis is not None and redis.password_secret_id not in {item.id for item in process_refs}:
            raise _fail("PRODUCTION_PROCESS_SECRET_MISMATCH")

        worker_concurrency, image_stage_concurrency = _process_resources(process, values)
        return cls(
            process=process,
            deployment=deployment,
            bootstrap_maintenance=_parse_bool(values.get("BOOTSTRAP_MAINTENANCE", "1")),
            database=database,
            redis=redis,
            secrets=process_refs,
            worker_concurrency=worker_concurrency,
            image_stage_concurrency=image_stage_concurrency,
        )

    def load_secrets(
        self,
        *,
        reader: ProductionSecretReader = _default_secret_reader,
    ) -> SecretBundle:
        loaded: list[tuple[str, SensitiveValue[bytes]]] = []
        for reference in self.secrets:
            path = str(PurePosixPath("/run/secrets") / reference.filename)
            value = reader(path, reference.policy())
            if not isinstance(value, SensitiveValue):
                raise _fail("PRODUCTION_SECRET_READER_INVALID")
            revealed = value.reveal_for_use()
            if not isinstance(revealed, bytes):
                raise _fail("PRODUCTION_SECRET_READER_INVALID")
            _validate_secret_content(reference.id, revealed)
            if reference.id == "control_bot_token" and int(revealed.split(b":", 1)[0]) != (
                self.deployment.runtime_identity.control_bot_user_id
            ):
                raise _fail("PRODUCTION_CONTROL_IDENTITY_MISMATCH")
            loaded.append((reference.id, value))
        return SecretBundle(tuple(loaded))

    def safe_log_fields(self) -> dict[str, str | bool]:
        return {
            "process": self.process.value,
            "deployment": "configured",
            "source_commit": self.deployment.source_commit[:12],
            "bootstrap_maintenance": self.bootstrap_maintenance,
        }


__all__ = [
    "ArtifactStatus",
    "DatabaseCompatibility",
    "DatabaseEndpoint",
    "DeploymentContract",
    "DeploymentManifest",
    "DeploymentTarget",
    "ImageArtifact",
    "ProductionConfigurationError",
    "ProductionProcess",
    "ProductionSecretReader",
    "ProductionSettings",
    "RedisEndpoint",
    "RuntimeIdentity",
    "SecretBundle",
    "SecretReference",
    "SessionProvisioningMode",
    "StartupPolicy",
    "load_deployment_contract",
]
