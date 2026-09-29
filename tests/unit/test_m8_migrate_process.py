from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Engine

from telegram_userbot.adapters.persistence.engine import PostgresConnectionSettings
from telegram_userbot.adapters.persistence.migration import (
    MigrationError,
    MigrationResult,
    MigrationStatus,
)
from telegram_userbot.adapters.persistence.role_closure import (
    ROLE_CLOSURE_FILENAMES,
    RoleClosurePlan,
    RoleClosureScript,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.config.production import (
    ProductionConfigurationError,
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.processes import migrate


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _settings(*, maintenance: bool = True) -> ProductionSettings:
    endpoint = SimpleNamespace(
        host="postgres",
        port=5432,
        database="telegram_userbot",
        login_role="telegram_userbot_migrator_login",
        runtime_role="telegram_userbot_migrator",
        password_secret_id="migrator_database_password",  # noqa: S106 - manifest id
        sslmode="disable",
    )
    settings = SimpleNamespace(
        process=ProductionProcess.MIGRATE,
        bootstrap_maintenance=maintenance,
        database=endpoint,
        load_secrets=_secrets,
    )
    return cast(ProductionSettings, settings)


def _secrets() -> SecretBundle:
    return SecretBundle((("migrator_database_password", SensitiveValue(b"M" * 40)),))


def _plan() -> RoleClosurePlan:
    return RoleClosurePlan(
        tuple(RoleClosureScript(name, "SELECT 1;") for name in ROLE_CLOSURE_FILENAMES)
    )


def _install_success_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: MigrationStatus = MigrationStatus.APPLIED,
) -> _Engine:
    engine = _Engine()

    def load_settings(values: object) -> ProductionSettings:
        del values
        return _settings()

    def create_engine(settings: PostgresConnectionSettings) -> Engine:
        assert settings.login_role == "telegram_userbot_migrator_login"
        assert settings.runtime_role == "telegram_userbot_migrator"
        assert settings.password.reveal_for_use() == "M" * 40
        assert "M" * 40 not in repr(settings)
        return cast(Engine, engine)

    def run_migration(*args: object, **kwargs: object) -> MigrationResult:
        del args
        assert kwargs["expected_revision"] == migrate.EXPECTED_REVISION
        assert kwargs["expected_vector_version"] == migrate.EXPECTED_VECTOR_VERSION
        assert kwargs["role_closure"] == _plan()
        return MigrationResult(status, migrate.EXPECTED_REVISION, migrate.EXPECTED_REVISION)

    monkeypatch.setattr(migrate, "_load_settings", load_settings)
    monkeypatch.setattr(migrate, "load_role_closure_plan", lambda directory: _plan())
    monkeypatch.setattr(migrate, "create_sync_postgres_engine", create_engine)
    monkeypatch.setattr(migrate, "migrate_to_head", run_migration)
    return engine


@pytest.mark.unit
def test_migrate_process_composes_component_engine_and_emits_stable_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_success_dependencies(monkeypatch)
    stdout = StringIO()
    stderr = StringIO()

    exit_code = migrate.run(
        ("upgrade", "head"),
        {},
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "MIGRATION_APPLIED\n"
    assert stderr.getvalue() == ""
    assert engine.disposed is True


@pytest.mark.unit
def test_migrate_process_busy_is_nonzero_and_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_success_dependencies(monkeypatch, status=MigrationStatus.BUSY)
    stdout = StringIO()

    exit_code = migrate.run(("upgrade", "head"), {}, stdout=stdout, stderr=StringIO())

    assert exit_code == 3
    assert stdout.getvalue() == "MIGRATION_BUSY\n"
    assert "APPLIED" not in stdout.getvalue()


@pytest.mark.unit
@pytest.mark.parametrize(
    "values",
    [
        {"TUDT_DATABASE_DSN": "postgresql://user:private@postgres/app"},
        {"PGPASSWORD": "private"},
        {"DATABASE_URL": "postgresql://user:private@postgres/app"},
    ],
)
def test_migrate_process_rejects_credential_bearing_environment_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
) -> None:
    def forbidden(values: object) -> ProductionSettings:
        del values
        raise AssertionError("settings must not load")

    monkeypatch.setattr(migrate, "_load_settings", forbidden)
    stdout = StringIO()
    stderr = StringIO()

    exit_code = migrate.run(("upgrade", "head"), values, stdout=stdout, stderr=stderr)

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "MIGRATION_CONFIGURATION_REJECTED\n"
    assert "private" not in stderr.getvalue()


@pytest.mark.unit
def test_migrate_process_rejects_any_other_command_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrate,
        "_load_settings",
        lambda values: pytest.fail("settings must not load"),
    )

    stderr = StringIO()
    exit_code = migrate.run(("downgrade", "base"), {}, stderr=stderr)

    assert exit_code == 2
    assert stderr.getvalue() == "MIGRATION_ARGUMENT_INVALID\n"


@pytest.mark.unit
def test_migrate_process_redacts_migration_failure_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_success_dependencies(monkeypatch)
    private_value = "SYNTHETIC_PRIVATE_DSN"

    def fail(*args: object, **kwargs: object) -> MigrationResult:
        del args, kwargs
        raise MigrationError("MIGRATION_ROLE_CONTRACT_INVALID")

    monkeypatch.setattr(migrate, "migrate_to_head", fail)
    stdout = StringIO()
    stderr = StringIO()

    exit_code = migrate.run(("upgrade", "head"), {}, stdout=stdout, stderr=stderr)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "MIGRATION_FAILED:MIGRATION_ROLE_CONTRACT_INVALID\n"
    assert private_value not in stderr.getvalue()
    assert engine.disposed is True


@pytest.mark.unit
def test_migrate_connection_contract_requires_maintenance() -> None:
    with pytest.raises(
        ProductionConfigurationError,
        match=r"^PRODUCTION_MIGRATION_CONTRACT_INVALID$",
    ):
        migrate._connection_settings(_settings(maintenance=False), _secrets())
