"""Bound preview deletion retries and prove proactive Telegram side effects."""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_m5_retry_budget_proof"
down_revision: str | Sequence[str] | None = "0017_m5_m7_recovery_binding"
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
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_attempt_count integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_next_attempt_at timestamptz"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_first_failed_at timestamptz"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_critical_alerted_at timestamptz"
    )
    op.execute(
        "UPDATE context_preview_deliveries SET "
        "delete_attempt_count = GREATEST(delete_attempt_count, 1), "
        "delete_first_failed_at = COALESCE("
        "delete_first_failed_at, delete_after, CURRENT_TIMESTAMP), "
        "delete_next_attempt_at = COALESCE(delete_next_attempt_at, CURRENT_TIMESTAMP) "
        "WHERE state = 'delete_failed'"
    )
    op.execute(
        "UPDATE context_preview_deliveries SET delete_next_attempt_at = NULL "
        "WHERE state <> 'delete_failed'"
    )
    _constraint(
        "ck_context_preview_deliveries_delete_attempt_count_nonnegative",
        "ALTER TABLE context_preview_deliveries ADD CONSTRAINT "
        "ck_context_preview_deliveries_delete_attempt_count_nonnegative "
        "CHECK (delete_attempt_count >= 0)",
    )
    _constraint(
        "ck_context_preview_deliveries_delete_retry_state_match",
        "ALTER TABLE context_preview_deliveries ADD CONSTRAINT "
        "ck_context_preview_deliveries_delete_retry_state_match CHECK "
        "((state = 'delete_failed' AND delete_next_attempt_at IS NOT NULL AND "
        "delete_first_failed_at IS NOT NULL) OR "
        "(state <> 'delete_failed' AND delete_next_attempt_at IS NULL))",
    )
    _constraint(
        "ck_context_preview_deliveries_delete_critical_requires_failure",
        "ALTER TABLE context_preview_deliveries ADD CONSTRAINT "
        "ck_context_preview_deliveries_delete_critical_requires_failure CHECK "
        "(delete_critical_alerted_at IS NULL OR delete_first_failed_at IS NOT NULL)",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_context_preview_deliveries_delete_due "
        "ON context_preview_deliveries "
        "(bot_identity, state, delete_next_attempt_at, delete_after, id) "
        "WHERE bot_message_id IS NOT NULL AND "
        "state IN ('sent','delete_pending','delete_failed')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_context_preview_deliveries_delete_due")
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP CONSTRAINT IF EXISTS "
        "ck_context_preview_deliveries_delete_critical_requires_failure"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP CONSTRAINT IF EXISTS "
        "ck_context_preview_deliveries_delete_retry_state_match"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP CONSTRAINT IF EXISTS "
        "ck_context_preview_deliveries_delete_attempt_count_nonnegative"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_critical_alerted_at"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_first_failed_at"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_next_attempt_at"
    )
    op.execute("ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_attempt_count")
