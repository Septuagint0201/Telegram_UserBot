"""Make media cleanup reclaimable and bind M6 review execution evidence."""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_m5_m6_recovery_execution"
down_revision: str | Sequence[str] | None = "0018_m5_retry_budget_proof"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; END IF; END $$"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_claimed_at timestamptz")
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_lease_expires_at timestamptz"
    )
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS "
        "delete_fencing_token bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS "
        "delete_attempt_count integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_next_attempt_at timestamptz"
    )
    op.execute("ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS delete_error_code text")
    op.execute(
        "UPDATE media_objects SET status = 'failed', "
        "delete_attempt_count = GREATEST(delete_attempt_count, 1), "
        "delete_next_attempt_at = COALESCE(delete_next_attempt_at, CURRENT_TIMESTAMP), "
        "delete_error_code = COALESCE(delete_error_code, 'delete_claim_recovered'), "
        "delete_claimed_at = NULL, delete_lease_expires_at = NULL "
        "WHERE status = 'delete_pending'"
    )
    op.execute("UPDATE media_objects SET delete_next_attempt_at = NULL WHERE status <> 'failed'")
    op.execute(
        "UPDATE media_objects SET "
        "delete_attempt_count = GREATEST(delete_attempt_count, 1), "
        "delete_next_attempt_at = COALESCE(delete_next_attempt_at, CURRENT_TIMESTAMP), "
        "delete_error_code = COALESCE(delete_error_code, 'delete_retry_recovered') "
        "WHERE status = 'failed' AND delete_requested_at IS NOT NULL"
    )
    _constraint(
        "ck_media_objects_delete_fencing_token_nonnegative",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_fencing_token_nonnegative "
        "CHECK (delete_fencing_token >= 0)",
    )
    _constraint(
        "ck_media_objects_delete_attempt_count_nonnegative",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_attempt_count_nonnegative "
        "CHECK (delete_attempt_count >= 0)",
    )
    _constraint(
        "ck_media_objects_delete_lease_state_match",
        "ALTER TABLE media_objects ADD CONSTRAINT "
        "ck_media_objects_delete_lease_state_match CHECK "
        "((status = 'delete_pending' AND delete_claimed_at IS NOT NULL AND "
        "delete_lease_expires_at IS NOT NULL) OR "
        "(status <> 'delete_pending' AND delete_claimed_at IS NULL AND "
        "delete_lease_expires_at IS NULL))",
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_media_objects_delete_due ON media_objects "
        "(status, delete_next_attempt_at, delete_lease_expires_at, expires_at, id) "
        "WHERE status IN ('ready','delete_pending','failed')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_media_objects_delete_due")
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_retry_state_match"
    )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_lease_state_match"
    )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_attempt_count_nonnegative"
    )
    op.execute(
        "ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS "
        "ck_media_objects_delete_fencing_token_nonnegative"
    )
    op.execute(
        "UPDATE media_objects SET status = 'delete_pending', "
        "delete_requested_at = COALESCE(delete_requested_at, CURRENT_TIMESTAMP) "
        "WHERE status = 'failed' AND delete_error_code = 'delete_claim_recovered'"
    )
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_error_code")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_next_attempt_at")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_attempt_count")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_fencing_token")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_lease_expires_at")
    op.execute("ALTER TABLE media_objects DROP COLUMN IF EXISTS delete_claimed_at")
