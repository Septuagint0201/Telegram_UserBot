"""Add M8 runtime status, durable cursors, receipts, and restore gate."""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M8_TABLES, metadata

revision: str = "0025_m8_service_status"
down_revision: str | Sequence[str] | None = "0024_runtime_fencing_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = [metadata.tables[name] for name in M8_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    # This historical revision deliberately snapshots its own producer revision.
    # ``metadata`` follows the current release so an explicit replacement is
    # required when a previous-revision database is built for upgrade testing.
    for table_name in ("service_instances", "service_status_events"):
        op.execute(
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS schema_revision_current; "
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
            f"ck_{table_name}_schema_revision_current"
        )
        op.create_check_constraint(
            "schema_revision_current",
            table_name,
            "schema_revision = '0025_m8_service_status'",
        )
    for table_name in (
        "service_instances",
        "service_status_events",
        "control_bot_cursors",
        "control_bot_update_receipts",
        "telegram_ingest_watermarks",
    ):
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    tables = [metadata.tables[name] for name in reversed(M8_TABLES)]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
