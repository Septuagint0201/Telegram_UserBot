"""Session-locked PostgreSQL migration library for the production one-shot process."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text

from telegram_userbot.adapters.persistence.role_closure import RoleClosurePlan

# A literal signed-bigint key remains stable across PostgreSQL hash implementation changes.
# Hex decodes to ``TGUBOTM8`` and is reserved solely for the global migration process.
MIGRATION_LOCK_KEY = 6_072_916_365_819_530_552

# M8 role contract v2 includes the dedicated export and monitor identities. Any later
# identity must deliberately version this inventory and its bootstrap/closure tests instead
# of being silently tolerated here.
_EXPECTED_ROLES = (
    ("telegram_userbot_app_login", False, False, False, False, True, False, False),
    ("telegram_userbot_app_runtime", False, False, False, False, False, False, False),
    ("telegram_userbot_backup", False, False, False, False, False, False, False),
    ("telegram_userbot_control_login", False, False, False, False, True, False, False),
    ("telegram_userbot_control_runtime", False, False, False, False, False, False, False),
    ("telegram_userbot_export_runtime", False, False, False, False, False, False, False),
    ("telegram_userbot_exporter_login", False, False, False, False, True, False, False),
    ("telegram_userbot_maintenance", False, False, False, False, False, False, False),
    ("telegram_userbot_migrator", False, False, False, False, False, False, False),
    ("telegram_userbot_migrator_login", False, False, False, False, True, False, False),
    ("telegram_userbot_monitor_login", False, False, False, False, True, False, False),
    ("telegram_userbot_monitor_runtime", False, False, False, False, False, False, False),
    ("telegram_userbot_worker_login", False, False, False, False, True, False, False),
    ("telegram_userbot_worker_runtime", False, False, False, False, False, False, False),
)
_EXPECTED_MEMBERSHIPS = (
    (
        "telegram_userbot_app_runtime",
        "telegram_userbot_app_login",
        False,
        False,
        True,
    ),
    (
        "telegram_userbot_control_runtime",
        "telegram_userbot_control_login",
        False,
        False,
        True,
    ),
    (
        "telegram_userbot_export_runtime",
        "telegram_userbot_exporter_login",
        False,
        False,
        True,
    ),
    (
        "telegram_userbot_migrator",
        "telegram_userbot_migrator_login",
        False,
        False,
        True,
    ),
    (
        "telegram_userbot_monitor_runtime",
        "telegram_userbot_monitor_login",
        False,
        False,
        True,
    ),
    (
        "telegram_userbot_worker_runtime",
        "telegram_userbot_worker_login",
        False,
        False,
        True,
    ),
)


class MigrationStatus(StrEnum):
    APPLIED = "applied"
    BUSY = "busy"


class MigrationError(RuntimeError):
    """A stable, content-free migration failure safe for operations logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: MigrationStatus
    from_revision: str | None = None
    to_revision: str | None = None


def _source_head(config: Config, expected_revision: str) -> str:
    try:
        heads = tuple(ScriptDirectory.from_config(config).get_heads())
    except Exception:
        raise MigrationError("MIGRATION_SOURCE_UNAVAILABLE") from None
    if heads != (expected_revision,):
        raise MigrationError("MIGRATION_SOURCE_HEAD_INVALID")
    return heads[0]


def _database_heads(connection: Connection) -> tuple[str, ...]:
    exists = connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
    if exists is not True:
        return ()
    return tuple(
        connection.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        )
        .scalars()
        .all()
    )


def _single_database_revision(connection: Connection, *, allow_empty: bool) -> str | None:
    heads = _database_heads(connection)
    if not heads and allow_empty:
        return None
    if len(heads) != 1:
        raise MigrationError("MIGRATION_DATABASE_HEAD_INVALID")
    return heads[0]


def _ensure_vector_extension(connection: Connection, expected_version: str) -> None:
    try:
        actual_version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    except Exception:
        raise MigrationError("MIGRATION_EXTENSION_UNAVAILABLE") from None
    if actual_version is None:
        raise MigrationError("MIGRATION_EXTENSION_UNAVAILABLE")
    if actual_version != expected_version:
        raise MigrationError("MIGRATION_EXTENSION_VERSION_MISMATCH")


def _verify_role_contract(connection: Connection) -> None:
    identity = tuple(connection.execute(text("SELECT session_user, current_user")).one())
    if identity != (
        "telegram_userbot_migrator_login",
        "telegram_userbot_migrator",
    ):
        raise MigrationError("MIGRATION_IDENTITY_INVALID")

    ownership = tuple(
        connection.execute(
            text(
                "SELECT current_database(), pg_get_userbyid(database.datdba), "
                "pg_get_userbyid(namespace.nspowner) "
                "FROM pg_database database CROSS JOIN pg_namespace namespace "
                "WHERE database.datname = current_database() AND namespace.nspname = 'public'"
            )
        ).one()
    )
    if ownership != (
        "telegram_userbot",
        "telegram_userbot_migrator",
        "telegram_userbot_migrator",
    ):
        raise MigrationError("MIGRATION_OWNERSHIP_INVALID")

    roles = tuple(
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolcanlogin, rolreplication, rolbypassrls FROM pg_roles "
                "WHERE rolname LIKE 'telegram_userbot_%' ORDER BY rolname"
            )
        ).all()
    )
    if roles != _EXPECTED_ROLES:
        raise MigrationError("MIGRATION_ROLE_CONTRACT_INVALID")

    memberships = tuple(
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT granted_role.rolname, member_role.rolname, membership.admin_option, "
                "membership.inherit_option, membership.set_option "
                "FROM pg_auth_members membership "
                "JOIN pg_roles granted_role ON granted_role.oid = membership.roleid "
                "JOIN pg_roles member_role ON member_role.oid = membership.member "
                "WHERE granted_role.rolname LIKE 'telegram_userbot_%' "
                "OR member_role.rolname LIKE 'telegram_userbot_%' "
                "ORDER BY granted_role.rolname, member_role.rolname"
            )
        ).all()
    )
    if memberships != _EXPECTED_MEMBERSHIPS:
        raise MigrationError("MIGRATION_ROLE_CONTRACT_INVALID")


def _run_role_closure(connection: Connection, plan: RoleClosurePlan) -> None:
    driver_connection = connection.connection.driver_connection
    if driver_connection is None:
        raise MigrationError("MIGRATION_DRIVER_INVALID")
    cursor = driver_connection.cursor()
    try:
        for script in plan.scripts:
            cursor.execute(script.sql, prepare=False)
    finally:
        cursor.close()


def _run_alembic_upgrade(config: Config, connection: Connection) -> None:
    sentinel = object()
    previous = config.attributes.get("connection", sentinel)
    config.attributes["connection"] = connection
    try:
        command.upgrade(config, "head")
    finally:
        if previous is sentinel:
            config.attributes.pop("connection", None)
        else:
            config.attributes["connection"] = previous


def _rollback_safely(connection: Connection) -> None:
    try:
        if connection.in_transaction():
            connection.rollback()
    except Exception:
        return


def _unlock_safely(connection: Connection) -> None:
    _rollback_safely(connection)
    try:
        connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": MIGRATION_LOCK_KEY},
        )
        connection.commit()
    except Exception:
        _rollback_safely(connection)


def _verify_post_migration(
    connection: Connection,
    *,
    expected_revision: str,
    expected_vector_version: str,
) -> str:
    to_revision = _single_database_revision(connection, allow_empty=False)
    vector_version = connection.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    if to_revision != expected_revision:
        raise MigrationError("MIGRATION_DATABASE_HEAD_INVALID")
    if vector_version != expected_vector_version:
        raise MigrationError("MIGRATION_EXTENSION_VERSION_MISMATCH")
    wrong_owner_count = connection.scalar(
        text(
            "SELECT COUNT(*) FROM pg_tables "
            "WHERE schemaname = 'public' AND tableowner <> 'telegram_userbot_migrator'"
        )
    )
    if wrong_owner_count != 0:
        raise MigrationError("MIGRATION_OWNERSHIP_INVALID")
    _verify_role_contract(connection)
    return to_revision


def _migrate_with_connection(
    connection: Connection,
    config: Config,
    *,
    expected_revision: str,
    expected_vector_version: str,
    role_closure: RoleClosurePlan,
) -> MigrationResult:
    acquired = False
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": MIGRATION_LOCK_KEY},
            )
        )
        if not acquired:
            return MigrationResult(MigrationStatus.BUSY)
        # Session-level advisory locks survive transaction boundaries. End the implicit
        # transaction opened by the lock query before starting the bounded DDL checks.
        connection.commit()

        with connection.begin():
            _ensure_vector_extension(connection, expected_vector_version)
            _verify_role_contract(connection)
            from_revision = _single_database_revision(connection, allow_empty=True)

        _run_alembic_upgrade(config, connection)
        _rollback_safely(connection)

        with connection.begin():
            _run_role_closure(connection, role_closure)
            to_revision = _verify_post_migration(
                connection,
                expected_revision=expected_revision,
                expected_vector_version=expected_vector_version,
            )
        return MigrationResult(MigrationStatus.APPLIED, from_revision, to_revision)
    finally:
        if acquired:
            _unlock_safely(connection)
        else:
            _rollback_safely(connection)


def migrate_to_head(
    engine: Engine,
    config: Config,
    *,
    expected_revision: str,
    expected_vector_version: str,
    role_closure: RoleClosurePlan,
) -> MigrationResult:
    """Upgrade once while holding the fixed PostgreSQL session advisory lock.

    ``BUSY`` is returned before any extension or Alembic operation when another process owns
    the lock. All other failures use stable codes, retain the database for a later forward
    retry, and never invoke Alembic downgrade.
    """

    if not expected_revision or not expected_revision.replace("_", "").isalnum():
        raise MigrationError("MIGRATION_POLICY_INVALID")
    if re.fullmatch(r"\d+\.\d+\.\d+", expected_vector_version) is None:
        raise MigrationError("MIGRATION_POLICY_INVALID")
    try:
        _source_head(config, expected_revision)
        with engine.connect() as connection:
            return _migrate_with_connection(
                connection,
                config,
                expected_revision=expected_revision,
                expected_vector_version=expected_vector_version,
                role_closure=role_closure,
            )
    except MigrationError:
        raise
    except Exception:
        # Driver errors may include a credential-bearing URL. Expose only a stable code.
        raise MigrationError("MIGRATION_FAILED") from None


__all__ = [
    "MIGRATION_LOCK_KEY",
    "MigrationError",
    "MigrationResult",
    "MigrationStatus",
    "migrate_to_head",
]
