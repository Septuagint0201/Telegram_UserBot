"""Persist proactive occurrence evidence activity state."""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_m7_evidence_activity"
down_revision: str | Sequence[str] | None = "0020_m5_m7_review_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE proactive_occurrence_evidence ADD COLUMN IF NOT EXISTS "
        "active boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE proactive_occurrence_evidence DROP COLUMN IF EXISTS active")
