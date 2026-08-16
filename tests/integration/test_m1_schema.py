from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from telegram_userbot.adapters.persistence.engine import schema_is_ready
from telegram_userbot.adapters.persistence.schema import (
    M1_TABLES,
    M2_TABLES,
    M3_TABLES,
    M4_TABLES,
    M5_TABLES,
    M6_TABLES,
    M7_TABLES,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_empty_base_round_trip_reaches_exact_head_and_vector(
    postgres_engine: AsyncEngine,
) -> None:
    assert await schema_is_ready(postgres_engine, "0023_m7_proactive_snapshot")
    async with postgres_engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        version = await connection.scalar(text("SHOW server_version"))
        vector = await connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    assert set(M1_TABLES) <= tables
    assert set(M2_TABLES) <= tables
    assert set(M3_TABLES) <= tables
    assert set(M4_TABLES) <= tables
    assert set(M5_TABLES) <= tables
    assert set(M6_TABLES) <= tables
    assert set(M7_TABLES) <= tables
    assert str(version).startswith("17.")
    assert vector == "0.8.6"


@pytest.mark.integration
async def test_schema_has_no_unnamed_constraints_or_indexes(postgres_engine: AsyncEngine) -> None:
    query = text(
        """
        SELECT COUNT(*)
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'public' AND c.conname IS NULL
        """
    )
    async with postgres_engine.connect() as connection:
        assert await connection.scalar(query) == 0
        owners = set(
            (
                await connection.execute(
                    text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
    assert owners == {"telegram_userbot_migrator"}

    role_sql = (ROOT / "deploy" / "postgres" / "m1_roles.sql").read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE ON audit_log" in role_sql
