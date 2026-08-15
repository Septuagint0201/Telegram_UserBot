from collections.abc import AsyncIterator, Iterator
from os import environ
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.6-pg17-bookworm@"
    "sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38"
)
REDIS_IMAGE = (
    "redis:8.2.8-bookworm@sha256:2f7462b9e93e0a7ae2edf3a0a0babc8a4d29f8bfc50849b906b7caaef925edc1"
)
ROOT = Path(__file__).resolve().parents[2]


def _async_dsn(raw: str) -> str:
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    configured = environ.get("TEST_POSTGRES_DSN", "").strip()
    if configured:
        yield _async_dsn(configured)
        return

    try:
        container = PostgresContainer(
            image=POSTGRES_IMAGE,
            username="postgres",
            password="postgres",  # noqa: S106 - disposable test container only
            dbname="telegram_userbot_test",
            driver="psycopg",
        )
        container.start()
    except Exception as error:
        pytest.skip(f"disposable PostgreSQL unavailable: {type(error).__name__}")
    try:
        yield _async_dsn(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    configured = environ.get("TEST_REDIS_URL", "").strip()
    if configured:
        yield configured
        return

    try:
        container = (
            DockerContainer(REDIS_IMAGE)
            .with_exposed_ports(6379)
            .with_command("redis-server --appendonly yes --maxmemory-policy noeviction")
        )
        container.start()
    except Exception as error:
        pytest.skip(f"disposable Redis unavailable: {type(error).__name__}")
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_database(postgres_dsn: str) -> Iterator[str]:
    previous = environ.get("TUDT_DATABASE_DSN")
    environ["TUDT_DATABASE_DSN"] = postgres_dsn
    config = Config(ROOT / "alembic.ini")
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.downgrade(config, "0001_m1_durable_state")
        command.upgrade(config, "head")
        command.downgrade(config, "0002_m2_model_control")
        command.upgrade(config, "head")
        command.downgrade(config, "0003_m3_telegram_lifecycle")
        command.upgrade(config, "head")
        command.downgrade(config, "0004_m4_orchestrator")
        command.upgrade(config, "head")
        command.downgrade(config, "0005_m4_control_result")
        command.upgrade(config, "head")
        command.downgrade(config, "0006_m5_media_context")
        command.upgrade(config, "head")

        sync_dsn = postgres_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        role_scripts = (
            (ROOT / "deploy" / "postgres" / "m1_roles.sql").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "postgres" / "m2_roles.sql").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "postgres" / "m3_roles.sql").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "postgres" / "m4_roles.sql").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "postgres" / "m5_roles.sql").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "postgres" / "m6_roles.sql").read_text(encoding="utf-8"),
        )
        with psycopg.connect(sync_dsn, autocommit=True) as connection:
            for role_script in role_scripts:
                connection.execute(role_script)
        yield postgres_dsn
    finally:
        if previous is None:
            environ.pop("TUDT_DATABASE_DSN", None)
        else:
            environ["TUDT_DATABASE_DSN"] = previous


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgres_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(postgres_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await postgres_engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(connection, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
