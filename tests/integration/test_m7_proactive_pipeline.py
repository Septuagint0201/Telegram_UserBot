"""PostgreSQL M7 schema and role contracts on the disposable service."""

from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import M7_TABLES

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
async def test_m7_schema_inventory_constraints_and_head(db_session: AsyncSession) -> None:
    rows = await db_session.scalars(
        text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'proactive_%' ORDER BY tablename"
        )
    )
    assert set(rows) == set(M7_TABLES)
    assert await db_session.scalar(text("SELECT version_num FROM alembic_version")) == (
        "0008_m7_proactive_pipeline"
    )
    indexes = set(
        await db_session.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename IN ("
                "'proactive_candidates','proactive_jobs','proactive_budget_buckets')"
            )
        )
    )
    assert {
        "ix_proactive_candidates_due",
        "ix_proactive_jobs_due",
        "uq_proactive_budget_bucket_identity",
    } <= indexes
    constraints = {
        cast(str, row["conname"])
        for row in (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'proactive_occurrences'::regclass"
                )
            )
        ).mappings()
    }
    assert {
        "ck_proactive_occurrences_reason_values",
        "ck_proactive_occurrences_state_values",
        "ck_proactive_occurrences_window_values",
    } <= constraints


@pytest.mark.integration
async def test_m7_roles_keep_control_out_of_candidate_and_decision_truth(
    db_session: AsyncSession,
) -> None:
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_control_runtime', "
                "'proactive_candidates', 'UPDATE')"
            )
        )
        is False
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_control_runtime', "
                "'proactive_policies', 'INSERT')"
            )
        )
        is True
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_worker_runtime', "
                "'proactive_budget_reservations', 'UPDATE')"
            )
        )
        is True
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_backup', "
                "'proactive_decisions', 'UPDATE')"
            )
        )
        is False
    )
