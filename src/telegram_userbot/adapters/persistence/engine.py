"""PostgreSQL connection construction and fail-closed production readiness."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from telegram_userbot.domain.shared.redaction import SensitiveValue


class DurableStateConfigurationError(ValueError):
    """Raised without echoing a potentially credential-bearing value."""


_ROLE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})


def _safe_component(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized or any(character in normalized for character in ("\x00", "\r", "\n")):
        raise DurableStateConfigurationError(f"{field} is invalid")
    return normalized


def _safe_role(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not _ROLE_NAME.fullmatch(normalized):
        raise DurableStateConfigurationError(f"{field} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class PostgresConnectionSettings:
    """Component-wise database settings with an explicitly wrapped password.

    Production configuration reads the password from a mounted secret file and constructs
    this value object. A credential-bearing DSN is supported only by :meth:`from_test_dsn`
    for disposable development and integration environments.
    """

    host: str
    port: int
    database: str
    login_role: str
    password: SensitiveValue[str]
    runtime_role: str | None = None
    sslmode: str = "prefer"
    application_name: str = "telegram_userbot"

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _safe_component(self.host, field="database host"))
        object.__setattr__(self, "database", _safe_component(self.database, field="database name"))
        login_role = _safe_role(self.login_role, field="database login role")
        runtime_role = _safe_role(self.runtime_role, field="database runtime role")
        if login_role is None:
            raise DurableStateConfigurationError("database login role is invalid")
        object.__setattr__(self, "login_role", login_role)
        object.__setattr__(self, "runtime_role", runtime_role)
        object.__setattr__(
            self,
            "application_name",
            _safe_component(self.application_name, field="database application name"),
        )
        if not 1 <= self.port <= 65535:
            raise DurableStateConfigurationError("database port is invalid")
        if self.sslmode not in _SSL_MODES:
            raise DurableStateConfigurationError("database sslmode is invalid")
        password = self.password.reveal_for_use()
        if not password or any(character in password for character in ("\x00", "\r", "\n")):
            raise DurableStateConfigurationError("database password secret is invalid")

    @classmethod
    def from_test_dsn(
        cls,
        dsn: str,
        *,
        runtime_role: str | None = None,
        application_name: str = "telegram_userbot_test",
    ) -> PostgresConnectionSettings:
        """Parse a disposable test DSN; production composition must not call this method."""

        try:
            url = make_url(dsn)
        except Exception:
            raise DurableStateConfigurationError("test database DSN is invalid") from None
        if url.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise DurableStateConfigurationError("test database DSN must use PostgreSQL")
        if url.host is None or url.database is None or url.username is None or url.password is None:
            raise DurableStateConfigurationError("test database DSN is incomplete")
        unknown_query = set(url.query) - {"sslmode"}
        if unknown_query:
            raise DurableStateConfigurationError("test database DSN has unsupported options")
        sslmode = url.query.get("sslmode", "prefer")
        if not isinstance(sslmode, str):
            raise DurableStateConfigurationError("test database sslmode is invalid")
        return cls(
            host=url.host,
            port=url.port or 5432,
            database=url.database,
            login_role=url.username,
            password=SensitiveValue(url.password),
            runtime_role=runtime_role,
            sslmode=sslmode,
            application_name=application_name,
        )

    def sqlalchemy_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.login_role,
            password=self.password.reveal_for_use(),
            host=self.host,
            port=self.port,
            database=self.database,
            query={
                "application_name": self.application_name,
                "sslmode": self.sslmode,
            },
        )

    def safe_log_fields(self) -> dict[str, str | int]:
        return {
            "database": "configured",
            "database_port": self.port,
            "database_role": self.runtime_role or "login_only",
        }


@dataclass(frozen=True, slots=True)
class DurableStateSettings:
    """Legacy non-production aggregate retained for disposable test composition."""

    database: PostgresConnectionSettings
    redis_url: str = field(repr=False)
    expected_revision: str
    expected_vector_version: str = "0.8.6"

    def __post_init__(self) -> None:
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise DurableStateConfigurationError("Redis endpoint must use Redis")
        if not self.expected_revision or not self.expected_revision.replace("_", "").isalnum():
            raise DurableStateConfigurationError("schema revision is invalid")
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.expected_vector_version):
            raise DurableStateConfigurationError("pgvector version is invalid")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DurableStateSettings:
        """Compatibility parser for non-production tests only.

        Production must pass :class:`PostgresConnectionSettings` built from individual
        non-secret fields and a mounted password file. This rejects the legacy complete
        DSN before inspecting or logging it when ``TUDT_ENVIRONMENT=production``.
        """

        environment = values.get("TUDT_ENVIRONMENT", "").strip().lower()
        if environment not in {"development", "test"}:
            raise DurableStateConfigurationError(
                "complete database DSN parsing is restricted to explicit non-production tests"
            )
        database_dsn = values.get("TUDT_DATABASE_DSN", "").strip()
        redis_url = values.get("TUDT_REDIS_URL", "").strip()
        expected_revision = values.get("TUDT_SCHEMA_REVISION", "").strip()
        expected_vector_version = values.get("TUDT_PGVECTOR_VERSION", "0.8.6").strip()
        return cls(
            PostgresConnectionSettings.from_test_dsn(database_dsn),
            redis_url,
            expected_revision,
            expected_vector_version,
        )

    def safe_log_fields(self) -> dict[str, str]:
        return {
            "database": "configured",
            "redis": "configured",
            "schema": self.expected_revision,
        }


@dataclass(frozen=True, slots=True)
class DatabaseReadinessPolicy:
    """Required production identity and ownership contract for one process."""

    expected_runtime_role: str
    expected_login_role: str
    expected_table_owner: str
    expected_vector_version: str = "0.8.6"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.expected_vector_version):
            raise DurableStateConfigurationError("pgvector version is invalid")
        for attribute, value, field_name in (
            ("expected_runtime_role", self.expected_runtime_role, "database runtime role"),
            ("expected_login_role", self.expected_login_role, "database login role"),
            ("expected_table_owner", self.expected_table_owner, "database table owner"),
        ):
            normalized = _safe_role(value, field=field_name)
            if normalized is None:
                raise DurableStateConfigurationError(f"{field_name} is invalid")
            object.__setattr__(self, attribute, normalized)

    @classmethod
    def for_production_process(
        cls,
        process: Literal["app", "control", "worker", "migrate"],
    ) -> Self:
        """Return the closed M8 role/owner policy; unknown process names fail closed."""

        try:
            login_role, runtime_role = _PRODUCTION_DATABASE_IDENTITIES[process]
        except KeyError:
            raise DurableStateConfigurationError("database process identity is invalid") from None
        return cls(
            expected_runtime_role=runtime_role,
            expected_login_role=login_role,
            expected_table_owner="telegram_userbot_migrator",
            expected_vector_version="0.8.6",
        )


_PRODUCTION_DATABASE_IDENTITIES = {
    "app": ("telegram_userbot_app_login", "telegram_userbot_app_runtime"),
    "control": ("telegram_userbot_control_login", "telegram_userbot_control_runtime"),
    "worker": ("telegram_userbot_worker_login", "telegram_userbot_worker_runtime"),
    "migrate": ("telegram_userbot_migrator_login", "telegram_userbot_migrator"),
}


def _install_runtime_role(engine: AsyncEngine | Engine, role: str | None) -> None:
    if role is None:
        return
    quoted_role = f'"{role}"'
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine

    @event.listens_for(sync_engine, "connect")
    def set_runtime_role(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET ROLE {quoted_role}")
        finally:
            cursor.close()


def create_postgres_engine(
    settings: DurableStateSettings | PostgresConnectionSettings,
) -> AsyncEngine:
    database = settings.database if isinstance(settings, DurableStateSettings) else settings
    engine = create_async_engine(
        database.sqlalchemy_url(),
        echo=False,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
    )
    _install_runtime_role(engine, database.runtime_role)
    return engine


def create_sync_postgres_engine(
    settings: PostgresConnectionSettings,
    *,
    null_pool: bool = True,
) -> Engine:
    options: dict[str, object] = {
        "echo": False,
        "isolation_level": "READ COMMITTED",
        "pool_pre_ping": True,
    }
    if null_pool:
        options["poolclass"] = NullPool
    engine = create_engine(settings.sqlalchemy_url(), **options)
    _install_runtime_role(engine, settings.runtime_role)
    return engine


async def schema_is_ready(
    engine: AsyncEngine,
    expected_revision: str,
    *,
    policy: DatabaseReadinessPolicy,
) -> bool:
    """Fail closed on multiple heads, extension drift, role drift, or ownership drift."""

    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT CASE WHEN COUNT(*) = 1 THEN min(version_num) END FROM alembic_version")
            )
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            if revision != expected_revision or vector_version != policy.expected_vector_version:
                return False
            current_role = await connection.scalar(text("SELECT current_user"))
            if current_role != policy.expected_runtime_role:
                return False
            login_role = await connection.scalar(text("SELECT session_user"))
            if login_role != policy.expected_login_role:
                return False
            wrong_owner_count = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tableowner <> :owner"
                ),
                {"owner": policy.expected_table_owner},
            )
            if wrong_owner_count != 0:
                return False
    except Exception:
        return False
    return True


__all__ = [
    "DatabaseReadinessPolicy",
    "DurableStateConfigurationError",
    "DurableStateSettings",
    "PostgresConnectionSettings",
    "create_postgres_engine",
    "create_sync_postgres_engine",
    "schema_is_ready",
]
