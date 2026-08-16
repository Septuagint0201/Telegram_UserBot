"""Harden M5 cleanup evidence and M6/M7 final review boundaries."""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_m5_m7_review_hardening"
down_revision: str | Sequence[str] | None = "0019_m5_m6_recovery_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; END IF; END $$"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_first_failed_at timestamptz"
    )
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_critical_alerted_at timestamptz"
    )
    op.execute(
        "UPDATE media_objects SET delete_first_failed_at = COALESCE("
        "delete_first_failed_at, delete_requested_at, delete_next_attempt_at, CURRENT_TIMESTAMP) "
        "WHERE status = 'failed' AND delete_requested_at IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_retry_state_match"
    )
    _constraint(
        "ck_media_objects_delete_retry_state_match",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_retry_state_match CHECK "
        "((status = 'failed' AND delete_requested_at IS NOT NULL AND "
        "delete_next_attempt_at IS NOT NULL AND delete_first_failed_at IS NOT NULL) OR "
        "(status = 'failed' AND delete_requested_at IS NULL AND "
        "delete_next_attempt_at IS NULL AND delete_first_failed_at IS NULL) OR "
        "(status <> 'failed' AND delete_next_attempt_at IS NULL))",
    )
    _constraint(
        "ck_media_objects_delete_critical_requires_failure",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_critical_requires_failure CHECK "
        "(delete_critical_alerted_at IS NULL OR delete_first_failed_at IS NOT NULL)",
    )

    for table_name in (
        "memory_input_manifest_items",
        "memory_proposal_evidence",
        "memory_evidence",
    ):
        op.execute(
            f"UPDATE {table_name} SET trust_class = 'user_statement' "  # noqa: S608
            "WHERE trust_class = 'untrusted_user'"
        )
        _constraint(
            f"ck_{table_name}_trust_class_values",
            f"ALTER TABLE {table_name} ADD CONSTRAINT "
            f"ck_{table_name}_trust_class_values CHECK "
            "(trust_class IN ('user_statement','observed','trusted_derived',"
            "'model_inference','external'))",
        )
    for table_name in ("memory_proposal_evidence", "memory_evidence"):
        _constraint(
            f"ck_{table_name}_evidence_role_values",
            f"ALTER TABLE {table_name} ADD CONSTRAINT "
            f"ck_{table_name}_evidence_role_values CHECK "
            "(evidence_role IN ('primary','supporting','contradicting'))",
        )


def downgrade() -> None:
    for table_name in ("memory_evidence", "memory_proposal_evidence"):
        op.execute(
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
            f"ck_{table_name}_evidence_role_values"
        )
    for table_name in (
        "memory_evidence",
        "memory_proposal_evidence",
        "memory_input_manifest_items",
    ):
        op.execute(
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS ck_{table_name}_trust_class_values"
        )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_critical_requires_failure"
    )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_retry_state_match"
    )
    _constraint(
        "ck_media_objects_delete_retry_state_match",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_retry_state_match CHECK "
        "((status = 'failed' AND delete_requested_at IS NOT NULL AND "
        "delete_next_attempt_at IS NOT NULL) OR "
        "(status = 'failed' AND delete_requested_at IS NULL AND "
        "delete_next_attempt_at IS NULL) OR "
        "(status <> 'failed' AND delete_next_attempt_at IS NULL))",
    )
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_critical_alerted_at")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_first_failed_at")
