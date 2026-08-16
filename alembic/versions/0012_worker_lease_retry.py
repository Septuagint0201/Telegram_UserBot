"""Fence proactive leases and bound M6/M7 worker retries."""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_worker_lease_retry"
down_revision: str | Sequence[str] | None = "0011_scope_context_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_jobs ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE proactive_jobs ADD COLUMN IF NOT EXISTS "
        "fencing_token bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "UPDATE proactive_jobs SET state = 'pending', lease_owner = NULL, "
        "lease_expires_at = NULL WHERE state = 'leased'"
    )
    op.execute(
        "ALTER TABLE memory_jobs DROP CONSTRAINT IF EXISTS ck_memory_jobs_estimates_nonnegative"
    )
    op.create_check_constraint(
        "estimates_nonnegative",
        "memory_jobs",
        "eligible_revision_count >= 0 AND estimated_input_tokens >= 0 AND attempt_count >= 0",
    )
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS ck_proactive_jobs_state_values"
    )
    op.create_check_constraint(
        "state_values",
        "proactive_jobs",
        "state IN ('pending','leased','retry_wait','succeeded','expired','dead_letter')",
    )
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS "
        "ck_proactive_jobs_fencing_token_nonnegative"
    )
    op.create_check_constraint(
        "fencing_token_nonnegative",
        "proactive_jobs",
        "fencing_token >= 0",
    )
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS ck_proactive_jobs_lease_fields_match"
    )
    op.create_check_constraint(
        "lease_fields_match",
        "proactive_jobs",
        "(state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE proactive_jobs SET state = 'pending', lease_owner = NULL, "
        "lease_expires_at = NULL WHERE state IN ('leased','retry_wait')"
    )
    op.execute("UPDATE proactive_jobs SET state = 'expired' WHERE state = 'dead_letter'")
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS ck_proactive_jobs_lease_fields_match"
    )
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS "
        "ck_proactive_jobs_fencing_token_nonnegative"
    )
    op.execute(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS ck_proactive_jobs_state_values"
    )
    op.create_check_constraint(
        "state_values",
        "proactive_jobs",
        "state IN ('pending','leased','succeeded','expired')",
    )
    op.execute(
        "ALTER TABLE memory_jobs DROP CONSTRAINT IF EXISTS ck_memory_jobs_estimates_nonnegative"
    )
    op.create_check_constraint(
        "estimates_nonnegative",
        "memory_jobs",
        "eligible_revision_count >= 0 AND estimated_input_tokens >= 0",
    )
    op.drop_column("proactive_jobs", "fencing_token")
    op.drop_column("memory_jobs", "attempt_count")
