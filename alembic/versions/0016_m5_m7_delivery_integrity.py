"""Bind proactive reservations to a durable delivery target."""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_m5_m7_delivery_integrity"
down_revision: str | Sequence[str] | None = "0015_context_preview_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; END IF; END $$"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE proactive_budget_reservations ADD COLUMN IF NOT EXISTS target text")
    op.execute(
        "UPDATE proactive_budget_reservations SET target = CASE "
        "WHEN outbound_group_id IS NOT NULL THEN 'auto_send' "
        "ELSE 'copilot_draft' END WHERE target IS NULL"
    )
    op.execute("ALTER TABLE proactive_budget_reservations ALTER COLUMN target SET NOT NULL")
    _constraint(
        "ck_proactive_budget_reservations_target",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_target CHECK "
        "(target IN ('auto_send','copilot_draft'))",
    )
    _constraint(
        "ck_proactive_budget_reservations_target_side_effect",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_target_side_effect CHECK "
        "((target = 'auto_send' AND copilot_draft_id IS NULL) OR "
        "(target = 'copilot_draft' AND outbound_group_id IS NULL))",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_reservations_target_side_effect"
    )
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_reservations_target"
    )
    op.execute("ALTER TABLE proactive_budget_reservations DROP COLUMN IF EXISTS target")
