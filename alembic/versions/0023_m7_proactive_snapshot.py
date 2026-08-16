"""Persist contact and relationship snapshots for proactive generations."""

# ruff: noqa: S608 - identifiers and expressions are migration-owned constants

from collections.abc import Sequence

from alembic import op

revision: str = "0023_m7_proactive_snapshot"
down_revision: str | Sequence[str] | None = "0022_m7_job_scope_and_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_constraint(table: str, name: str, expression: str) -> None:
    op.execute(
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = '{table}'::regclass "
        f"AND conname = '{name}') THEN ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({expression}); END IF; END $$;"
    )


def upgrade() -> None:
    for table in ("proactive_occurrences", "proactive_candidates"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS contact_setting_version integer")
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS relationship_state_version integer"
        )
        _add_constraint(
            table,
            f"ck_{table}_contact_setting_version_positive",
            "contact_setting_version IS NULL OR contact_setting_version > 0",
        )
        _add_constraint(
            table,
            f"ck_{table}_relationship_state_version_positive",
            "relationship_state_version IS NULL OR relationship_state_version > 0",
        )


def downgrade() -> None:
    for table in ("proactive_candidates", "proactive_occurrences"):
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
            f"ck_{table}_relationship_state_version_positive, DROP CONSTRAINT IF EXISTS "
            f"ck_{table}_contact_setting_version_positive"
        )
        op.execute(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS relationship_state_version, "
            "DROP COLUMN IF EXISTS contact_setting_version"
        )
