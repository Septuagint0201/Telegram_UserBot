from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, PrimaryKeyConstraint, Table, UniqueConstraint

from telegram_userbot.adapters.persistence.engine import (
    DurableStateConfigurationError,
    DurableStateSettings,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.schema import (
    M1_TABLES,
    M5_TABLES,
    memories,
    metadata,
    model_runs,
    summaries,
)


class FakeConnection:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    async def scalar(self, statement: object) -> object:
        return self.values.pop(0)


class FakeContext(AbstractAsyncContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection, error: Exception | None = None) -> None:
        self.connection = connection
        self.error = error

    async def __aenter__(self) -> FakeConnection:
        if self.error is not None:
            raise self.error
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, values: list[object], error: Exception | None = None) -> None:
        self.context = FakeContext(FakeConnection(values), error)

    def connect(self) -> FakeContext:
        return self.context


@pytest.mark.unit
def test_m1_schema_inventory_and_constraint_names() -> None:
    expected = {
        "accounts",
        "telegram_peers",
        "account_peers",
        "contacts",
        "conversations",
        "message_events",
        "messages",
        "message_revisions",
        "media_objects",
        "message_media",
        "message_reactions",
        "account_orchestrator_states",
        "conversation_mode_history",
        "account_control_history",
        "conversation_turns",
        "background_jobs",
        "transactional_outbox",
        "audit_log",
        "data_erasure_requests",
        "erasure_progress",
        "erasure_ledger",
        "migration_progress",
    }
    assert set(M1_TABLES) == expected
    assert set(M5_TABLES) == {
        "context_policies",
        "context_policy_versions",
        "retrieval_policies",
        "retrieval_policy_versions",
        "context_manifests",
        "context_manifest_items",
        "context_manifest_item_reasons",
        "context_manifest_omissions",
        "context_preview_requests",
        "context_preview_tokens",
        "context_preview_deliveries",
    }
    for table in metadata.tables.values():
        assert all(constraint.name for constraint in table.constraints)
        assert all(index.name for index in table.indexes)

    # The actual database constraint is added by 0006. Attaching it to the
    # shared MetaData would contaminate the historical 0004 partial create.
    assert "fk_model_runs_context_manifest_scope" not in {
        constraint.name for constraint in model_runs.constraints
    }
    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0006_m5_media_context.py"
    ).read_text(encoding="utf-8")
    assert '"fk_model_runs_context_manifest_scope"' in migration


@pytest.mark.unit
@pytest.mark.parametrize(
    ("table", "foreign_key_name"),
    [
        (memories, "fk_memories_current_version"),
        (summaries, "fk_summaries_current_version"),
    ],
)
def test_m6_current_version_foreign_keys_reference_exact_candidate_keys(
    table: Table,
    foreign_key_name: str,
) -> None:
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, ForeignKeyConstraint) and item.name == foreign_key_name
    )
    target_columns = tuple(element.column.name for element in constraint.elements)
    candidate_keys = {
        tuple(column.name for column in item.columns)
        for item in constraint.referred_table.constraints
        if isinstance(item, (PrimaryKeyConstraint, UniqueConstraint))
    }
    assert target_columns in candidate_keys


@pytest.mark.unit
def test_durable_settings_are_strict_and_safe() -> None:
    settings = DurableStateSettings.from_mapping(
        {
            "TUDT_DATABASE_DSN": "postgresql://user:password@db/app",
            "TUDT_REDIS_URL": "redis://redis:6379/0",
            "TUDT_SCHEMA_REVISION": "0001_m1_durable_state",
        }
    )
    assert settings.safe_log_fields() == {
        "database": "configured",
        "redis": "configured",
        "schema": "0001_m1_durable_state",
    }
    assert "password" not in repr(settings.safe_log_fields())

    invalid: tuple[Mapping[str, str], ...] = (
        {
            "TUDT_DATABASE_DSN": "sqlite:///bad",
            "TUDT_REDIS_URL": "redis://redis",
            "TUDT_SCHEMA_REVISION": "head",
        },
        {
            "TUDT_DATABASE_DSN": "postgresql://db/app",
            "TUDT_REDIS_URL": "http://redis",
            "TUDT_SCHEMA_REVISION": "head",
        },
        {
            "TUDT_DATABASE_DSN": "postgresql://db/app",
            "TUDT_REDIS_URL": "redis://redis",
            "TUDT_SCHEMA_REVISION": "not valid",
        },
    )
    for values in invalid:
        with pytest.raises(DurableStateConfigurationError):
            DurableStateSettings.from_mapping(values)


@pytest.mark.unit
async def test_engine_normalizes_driver_and_readiness_fails_closed() -> None:
    settings = DurableStateSettings("postgresql://db/app", "redis://redis", "0001_m1_durable_state")
    engine = create_postgres_engine(settings)
    assert engine.url.drivername == "postgresql+psycopg"
    await engine.dispose()
    direct = create_postgres_engine(
        DurableStateSettings(
            "postgresql+psycopg://db/app", "redis://redis", "0001_m1_durable_state"
        )
    )
    assert direct.url.drivername == "postgresql+psycopg"
    await direct.dispose()

    ready_engine = FakeEngine(["0001_m1_durable_state", "0.8.6"])
    assert await schema_is_ready(
        ready_engine,  # type: ignore[arg-type]
        "0001_m1_durable_state",
    )
    assert not await schema_is_ready(
        FakeEngine(["old", "0.8.6"]),  # type: ignore[arg-type]
        "0001_m1_durable_state",
    )
    assert not await schema_is_ready(
        FakeEngine([], RuntimeError("offline")),  # type: ignore[arg-type]
        "0001_m1_durable_state",
    )
