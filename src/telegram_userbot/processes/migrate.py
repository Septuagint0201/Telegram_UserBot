"""Production-only, one-shot PostgreSQL migration and role-closure process."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from alembic.config import Config

from telegram_userbot.adapters.persistence.engine import (
    DurableStateConfigurationError,
    PostgresConnectionSettings,
    create_sync_postgres_engine,
)
from telegram_userbot.adapters.persistence.migration import (
    MigrationError,
    MigrationStatus,
    migrate_to_head,
)
from telegram_userbot.adapters.persistence.role_closure import (
    RoleClosureError,
    load_role_closure_plan,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.compatibility import (
    EXPECTED_PGVECTOR_VERSION,
    EXPECTED_SCHEMA_REVISION,
)
from telegram_userbot.platform.config.production import (
    ProductionConfigurationError,
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)

EXPECTED_REVISION = EXPECTED_SCHEMA_REVISION
EXPECTED_VECTOR_VERSION = EXPECTED_PGVECTOR_VERSION
ROLE_CLOSURE_DIRECTORY = Path("/opt/app/deploy/postgres")
ALEMBIC_CONFIG_PATH = Path("/opt/app/alembic.ini")

_FORBIDDEN_CREDENTIAL_ENV = frozenset(
    {
        "DATABASE_URL",
        "PGAPPNAME",
        "PGDATABASE",
        "PGHOST",
        "PGOPTIONS",
        "PGPASSFILE",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSSLMODE",
        "PGUSER",
        "POSTGRES_DSN",
        "POSTGRES_URL",
        "SQLALCHEMY_URL",
        "TUDT_DATABASE_DSN",
    }
)


def _load_settings(values: Mapping[str, str]) -> ProductionSettings:
    return ProductionSettings.load(ProductionProcess.MIGRATE, values)


def _connection_settings(
    settings: ProductionSettings,
    secrets: SecretBundle,
) -> PostgresConnectionSettings:
    endpoint = settings.database
    if (
        settings.process is not ProductionProcess.MIGRATE
        or not settings.bootstrap_maintenance
        or endpoint.database != "telegram_userbot"
    ):
        raise ProductionConfigurationError("PRODUCTION_MIGRATION_CONTRACT_INVALID")
    try:
        raw_password = secrets.get(endpoint.password_secret_id).reveal_for_use()
    except KeyError:
        raise ProductionConfigurationError("PRODUCTION_PROCESS_SECRET_MISMATCH") from None
    try:
        password = raw_password.decode("ascii")
    except UnicodeDecodeError:
        raise ProductionConfigurationError("PRODUCTION_DATABASE_SECRET_INVALID") from None
    return PostgresConnectionSettings(
        host=endpoint.host,
        port=endpoint.port,
        database=endpoint.database,
        login_role=endpoint.login_role,
        password=SensitiveValue(password),
        runtime_role=endpoint.runtime_role,
        sslmode=endpoint.sslmode,
        application_name="telegram_userbot_migrate",
    )


def _credential_environment_is_clean(values: Mapping[str, str]) -> bool:
    return not any(
        key in _FORBIDDEN_CREDENTIAL_ENV or key.startswith("TUDT_DATABASE_") for key in values
    )


def run(  # noqa: PLR0911 - stable process exit contracts are intentionally explicit
    argv: Sequence[str],
    values: Mapping[str, str],
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run exactly ``upgrade head`` with stable, content-free status output."""

    if tuple(argv) != ("upgrade", "head"):
        stderr.write("MIGRATION_ARGUMENT_INVALID\n")
        return 2
    if not _credential_environment_is_clean(values):
        stderr.write("MIGRATION_CONFIGURATION_REJECTED\n")
        return 2

    engine = None
    try:
        settings = _load_settings(values)
        secrets = settings.load_secrets()
        connection_settings = _connection_settings(settings, secrets)
        role_closure = load_role_closure_plan(ROLE_CLOSURE_DIRECTORY)
        engine = create_sync_postgres_engine(connection_settings)
        result = migrate_to_head(
            engine,
            Config(str(ALEMBIC_CONFIG_PATH)),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=role_closure,
        )
    except (
        DurableStateConfigurationError,
        ProductionConfigurationError,
        RoleClosureError,
    ):
        stderr.write("MIGRATION_CONFIGURATION_REJECTED\n")
        return 2
    except MigrationError as error:
        stderr.write(f"MIGRATION_FAILED:{error.code}\n")
        return 1
    except Exception:
        stderr.write("MIGRATION_FAILED\n")
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if result.status is MigrationStatus.BUSY:
        stdout.write("MIGRATION_BUSY\n")
        return 3
    stdout.write("MIGRATION_APPLIED\n")
    return 0


def main() -> int:
    return run(sys.argv[1:], os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
