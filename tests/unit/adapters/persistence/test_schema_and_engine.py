import re
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
    M6_TABLES,
    context_preview_deliveries,
    copilot_drafts,
    memories,
    memory_proposals,
    memory_review_actions,
    metadata,
    model_runs,
    outbound_delivery_groups,
    proactive_budget_reservations,
    proactive_decisions,
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
def test_account_owned_foreign_keys_include_local_account_scope() -> None:
    for table in metadata.tables.values():
        if "account_id" not in table.c:
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if "account_id" not in constraint.referred_table.c:
                continue
            assert "account_id" in constraint.column_keys, (
                f"{table.name}.{constraint.name} omits local account_id"
            )


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
def test_m6_table_inventory_is_topological_for_downgrade() -> None:
    positions = {name: position for position, name in enumerate(M6_TABLES)}
    migrations = "\n".join(
        (Path(__file__).resolve().parents[4] / "alembic" / "versions" / migration).read_text(
            encoding="utf-8"
        )
        for migration in (
            "0007_m6_memory_pipeline.py",
            "0009_m5_m6_account_scope_constraints.py",
            "0010_account_scope_refs.py",
        )
    )
    for table_name in M6_TABLES:
        table = metadata.tables[table_name]
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            target_name = constraint.referred_table.name
            if target_name not in positions or target_name == table_name:
                continue
            if constraint.use_alter:
                assert f'"{constraint.name}"' in migrations
                continue
            assert positions[target_name] < positions[table_name], constraint.name


@pytest.mark.unit
def test_m6_model_run_manifest_column_replay_is_idempotent() -> None:
    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0007_m6_memory_pipeline.py"
    ).read_text(encoding="utf-8")

    assert (
        "ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS memory_input_manifest_id uuid" in migration
    )
    assert "op.add_column" not in migration
    assert '"fk_model_runs_memory_input_manifest"' in migration


@pytest.mark.unit
def test_m5_m6_scope_migration_drops_dependent_fks_before_candidate_keys() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0009_m5_m6_account_scope_constraints.py"
    ).read_text(encoding="utf-8")

    drop_fks = migration.index("for table, names in old_constraints.items():")
    create_keys = migration.index("for table, name, columns in unique_constraints:")
    create_fks = migration.index("foreign_keys = (")

    assert drop_fks < create_keys < create_fks
    assert "_drop_constraints(table, (name,))\n        _create_unique" not in migration


@pytest.mark.unit
def test_m5_m6_scope_migration_uses_postgres_safe_constraint_names() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0009_m5_m6_account_scope_constraints.py"
    ).read_text(encoding="utf-8")

    constraint_names = re.findall(r'"(fk_[^"]+)"', migration)

    assert constraint_names
    assert all(len(name) <= 63 for name in constraint_names)


@pytest.mark.unit
def test_m5_m6_scope_downgrade_restores_m6_external_foreign_keys() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0009_m5_m6_account_scope_constraints.py"
    ).read_text(encoding="utf-8")
    restore_section = migration[
        migration.index("old_fks = (") : migration.index(
            "for name, table, referred_table, local_columns, remote_columns in old_fks:"
        )
    ]

    assert '"fk_context_manifest_items_memory_version"' in restore_section
    assert '"fk_context_manifest_items_summary_version"' in restore_section


@pytest.mark.unit
def test_m5_m6_scope_downgrade_keeps_candidate_keys_for_partial_round_trips() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0009_m5_m6_account_scope_constraints.py"
    ).read_text(encoding="utf-8")
    downgrade = migration[migration.index("def downgrade() -> None:") :]

    assert '("media_objects", "uq_media_objects_id_account")' not in downgrade
    assert '("model_runs", "uq_model_runs_id_account_role")' not in downgrade


@pytest.mark.unit
def test_remaining_scope_migration_is_postgres_safe_and_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0010_account_scope_refs.py"
    ).read_text(encoding="utf-8")
    constraint_names = re.findall(r'"((?:fk|uq)_[^"]+)"', migration)
    downgrade = migration[migration.index("def downgrade() -> None:") :]

    assert constraint_names
    assert all(len(name) <= 63 for name in constraint_names)
    assert '"fk_context_preview_requests_control_command_id_control_commands"' in downgrade
    assert '"fk_embedding_records_space_dimensions"' in downgrade
    assert '("message_events", "uq_message_events_id_account")' not in downgrade
    assert '("embedding_spaces", "uq_embedding_spaces_account_dimensions")' not in downgrade


@pytest.mark.unit
def test_context_erasure_scope_migration_is_safe_and_keeps_candidate_key() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0011_scope_context_erasure.py"
    ).read_text(encoding="utf-8")
    constraint_names = re.findall(r'"((?:fk|uq)_[^"]+)"', migration)
    downgrade = migration[migration.index("def downgrade() -> None:") :]

    assert constraint_names
    assert all(len(name) <= 63 for name in constraint_names)
    assert '"fk_context_manifests_embedding_scope"' in migration
    assert '"fk_erasure_requests_memory_scope"' in migration
    assert '"fk_erasure_requests_contact_scope"' in migration
    assert '"uq_embedding_spaces_id_account"' not in downgrade


@pytest.mark.unit
def test_worker_retry_migration_is_fenced_bounded_and_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0012_worker_lease_retry.py"
    ).read_text(encoding="utf-8")
    constraint_names = re.findall(r'"(ck_[^"]+)"', migration)
    downgrade = migration[migration.index("def downgrade() -> None:") :]

    assert constraint_names
    assert all(len(name) <= 63 for name in constraint_names)
    assert "fencing_token bigint NOT NULL DEFAULT 0" in migration
    assert "attempt_count integer NOT NULL DEFAULT 0" in migration
    assert "dead_letter" in migration
    assert "op.drop_constraint" not in downgrade
    assert "ck_proactive_jobs_lease_fields_match" in downgrade
    assert "ck_proactive_jobs_fencing_token_nonnegative" in downgrade
    assert 'op.drop_column("proactive_jobs", "fencing_token")' in downgrade
    assert 'op.drop_column("memory_jobs", "attempt_count")' in downgrade


@pytest.mark.unit
def test_m7_budget_integrity_backfill_is_postgres_safe_and_idempotent() -> None:
    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0014_m7_budget_integrity.py"
    ).read_text(encoding="utf-8")

    assert "FROM proactive_budget_reservations AS reservation_src" in migration
    assert "WHERE reservation.id = backfill.reservation_id" in migration
    assert ('_constraint(\n        "uq_proactive_budget_reservations_account_key",') in migration
    assert "reservation_key is duplicated across accounts" in migration


@pytest.mark.unit
def test_context_preview_integrity_migration_supports_derived_sources_and_is_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0015_context_preview_integrity.py"
    ).read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION public.context_preview_sources" in migration
    assert "WHEN 'memory_version' THEN mv.rendered_text" in migration
    assert "WHEN 'summary_version' THEN sv.content_text" in migration
    assert "msg.deleted_at IS NULL" in migration
    assert "mo.expires_at > CURRENT_TIMESTAMP" in migration
    assert "uq_context_manifest_omissions_ordinal" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_delivery_integrity_migration_binds_proactive_target_conservatively() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0016_m5_m7_delivery_integrity.py"
    ).read_text(encoding="utf-8")

    assert "WHEN outbound_group_id IS NOT NULL THEN 'auto_send'" in migration
    assert "ELSE 'copilot_draft'" in migration
    assert "ck_proactive_budget_reservations_target_side_effect" in migration
    assert "IF NOT EXISTS (SELECT 1 FROM pg_constraint" in migration
    assert not proactive_budget_reservations.c.target.nullable


@pytest.mark.unit
def test_recovery_binding_migration_fences_delete_and_proactive_side_effects() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0017_m5_m7_recovery_binding.py"
    ).read_text(encoding="utf-8")

    assert "delete_lease_expires_at" in migration
    assert "delete_fencing_token" in migration
    assert "delete_claim_recovered" in migration
    assert "proactive_decision_id" in migration
    assert "budget_reservations_target_side_effect" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "uq_proactive_decisions_full_scope" in migration
    assert "))) NOT VALID" in migration
    assert "VALIDATE CONSTRAINT" in migration
    assert "cannot downgrade M7: proactive side-effect provenance exists" in migration
    assert "delete_claim_downgrade_recovered" in migration
    assert "fk_outbound_groups_proactive_decision_scope" not in {
        constraint.name for constraint in outbound_delivery_groups.foreign_key_constraints
    }
    assert "fk_copilot_drafts_proactive_decision_scope" not in {
        constraint.name for constraint in copilot_drafts.foreign_key_constraints
    }


@pytest.mark.unit
def test_preview_delete_retry_migration_is_bounded_indexed_and_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0018_m5_retry_budget_proof.py"
    ).read_text(encoding="utf-8")

    assert "delete_attempt_count integer NOT NULL DEFAULT 0" in migration
    assert "delete_next_attempt_at timestamptz" in migration
    assert "delete_first_failed_at timestamptz" in migration
    assert "delete_critical_alerted_at timestamptz" in migration
    assert "ix_context_preview_deliveries_delete_due" in migration
    assert "delete_retry_state_match" in migration
    assert "def downgrade() -> None:" in migration
    assert not context_preview_deliveries.c.delete_attempt_count.nullable
    assert {index.name for index in context_preview_deliveries.indexes} >= {
        "ix_context_preview_deliveries_delete_due"
    }


@pytest.mark.unit
def test_media_cleanup_and_review_execution_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0019_m5_m6_recovery_execution.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0018_m5_retry_budget_proof"' in migration
    assert "delete_lease_expires_at timestamptz" in migration
    assert "delete_fencing_token bigint NOT NULL DEFAULT 0" in migration
    assert "delete_attempt_count integer NOT NULL DEFAULT 0" in migration
    assert "delete_retry_recovered" in migration
    assert "ix_media_objects_delete_due" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_m5_m7_review_hardening_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0020_m5_m7_review_hardening.py"
    ).read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0019_m5_m6_recovery_execution"' in migration
    )
    assert "delete_first_failed_at timestamptz" in migration
    assert "delete_critical_alerted_at timestamptz" in migration
    assert '"memory_proposal_evidence"' in migration
    assert '"memory_evidence"' in migration
    assert "trust_class_values" in migration
    assert "evidence_role_values" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_m7_occurrence_evidence_activity_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0021_m7_evidence_activity.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0020_m5_m7_review_hardening"' in migration
    assert "proactive_occurrence_evidence" in migration
    assert "active boolean NOT NULL DEFAULT true" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_m7_job_scope_and_deadline_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0022_m7_job_scope_and_deadline.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0021_m7_evidence_activity"' in migration
    assert "uq_proactive_jobs_idempotency" in migration
    assert "uq_proactive_jobs_account_idempotency" in migration
    assert "DROP CONSTRAINT IF EXISTS window_values" in migration
    assert "IF NOT EXISTS" in migration
    assert "hard_deadline_at >= window_start_at" in migration
    assert "hard_deadline_at <= window_end_at);" in migration
    assert (
        "ALTER TABLE proactive_jobs ADD CONSTRAINT uq_proactive_jobs_idempotency" not in migration
    )
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_m7_proactive_snapshot_closure_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0023_m7_proactive_snapshot.py"
    ).read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0022_m7_job_scope_and_deadline"' in migration
    )
    assert "proactive_occurrences" in migration
    assert "proactive_candidates" in migration
    assert "contact_setting_version" in migration
    assert "relationship_state_version" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_runtime_fencing_and_provenance_migration_is_recoverable() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0024_runtime_fencing_provenance.py"
    ).read_text(encoding="utf-8")

    assert 'revision: str = "0024_runtime_fencing_provenance"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0023_m7_proactive_snapshot"' in migration
    assert "memory_input_manifest_id" in migration
    assert "send_fencing_token" in migration
    assert "send_lease_expires_at" in migration
    assert "ck_outbound_intents_send_lease_matches_state" in migration
    assert "def downgrade() -> None:" in migration


@pytest.mark.unit
def test_m5_m7_consistency_constraints_bind_review_and_decision_identity() -> None:
    assert not memory_review_actions.c.conversation_id.nullable
    assert not memory_proposals.c.review_version.nullable
    review_fks = {
        item.name: tuple(item.column_keys)
        for item in memory_review_actions.constraints
        if isinstance(item, ForeignKeyConstraint)
    }
    assert review_fks["fk_memory_review_actions_proposal_scope"] == (
        "proposal_id",
        "account_id",
        "conversation_id",
    )
    assert review_fks["fk_memory_review_actions_memory_scope"] == (
        "memory_id",
        "account_id",
        "conversation_id",
    )
    decision_uniques = {
        tuple(item.columns.keys())
        for item in proactive_decisions.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert ("candidate_id",) in decision_uniques

    migration = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0013_m5_m7_consistency.py"
    ).read_text(encoding="utf-8")
    assert "review_version integer NOT NULL DEFAULT 1" in migration
    assert "ALTER COLUMN conversation_id SET NOT NULL" in migration
    assert "uq_proactive_decisions_candidate UNIQUE (candidate_id)" in migration
    assert "expected_proposal_version IS NOT NULL" in migration


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
