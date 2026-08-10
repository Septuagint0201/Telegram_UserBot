"""PostgreSQL engine construction and schema-readiness checks."""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DurableStateConfigurationError(ValueError):
    """Raised without echoing a potentially credential-bearing DSN."""


@dataclass(frozen=True, slots=True)
class DurableStateSettings:
    database_dsn: str
    redis_url: str
    expected_revision: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DurableStateSettings:
        database_dsn = values.get("TUDT_DATABASE_DSN", "").strip()
        redis_url = values.get("TUDT_REDIS_URL", "").strip()
        expected_revision = values.get("TUDT_SCHEMA_REVISION", "").strip()
        if not database_dsn.startswith(("postgresql+psycopg://", "postgresql://")):
            raise DurableStateConfigurationError("TUDT_DATABASE_DSN must use PostgreSQL")
        if not redis_url.startswith(("redis://", "rediss://")):
            raise DurableStateConfigurationError("TUDT_REDIS_URL must use Redis")
        if not expected_revision or not expected_revision.replace("_", "").isalnum():
            raise DurableStateConfigurationError("TUDT_SCHEMA_REVISION is invalid")
        return cls(database_dsn, redis_url, expected_revision)

    def safe_log_fields(self) -> dict[str, str]:
        return {"database": "configured", "redis": "configured", "schema": self.expected_revision}


def create_postgres_engine(settings: DurableStateSettings) -> AsyncEngine:
    dsn = settings.database_dsn
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_async_engine(
        dsn,
        echo=False,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
    )


async def schema_is_ready(engine: AsyncEngine, expected_revision: str) -> bool:
    """Fail closed when the database is absent or not at the exact expected head."""

    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
    except Exception:
        return False
    return revision == expected_revision and isinstance(vector_version, str)
