"""Scope proactive job idempotency and keep deadlines inside occurrence windows."""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_m7_job_scope_and_deadline"
down_revision: str | Sequence[str] | None = "0021_m7_evidence_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS uq_proactive_jobs_idempotency;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'proactive_jobs'::regclass
                  AND conname = 'uq_proactive_jobs_account_idempotency'
            ) THEN
                ALTER TABLE proactive_jobs ADD CONSTRAINT uq_proactive_jobs_account_idempotency
                    UNIQUE (account_id, idempotency_key);
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE proactive_occurrences
            DROP CONSTRAINT IF EXISTS window_values,
            DROP CONSTRAINT IF EXISTS ck_proactive_occurrences_window_values;
        ALTER TABLE proactive_occurrences ADD CONSTRAINT ck_proactive_occurrences_window_values
            CHECK (window_start_at < window_end_at AND hard_deadline_at >= window_start_at
                   AND hard_deadline_at <= window_end_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE proactive_occurrences
            DROP CONSTRAINT IF EXISTS ck_proactive_occurrences_window_values,
            DROP CONSTRAINT IF EXISTS window_values;
        ALTER TABLE proactive_occurrences ADD CONSTRAINT window_values
            CHECK (window_start_at < window_end_at AND hard_deadline_at <= window_end_at);
        """
    )
