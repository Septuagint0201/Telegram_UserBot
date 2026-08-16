"""Fence preview deletion recovery and bind proactive side effects."""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_m5_m7_recovery_binding"
down_revision: str | Sequence[str] | None = "0016_m5_m7_delivery_integrity"
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
        "delete_claimed_at timestamptz"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_lease_expires_at timestamptz"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries ADD COLUMN IF NOT EXISTS "
        "delete_fencing_token bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "UPDATE context_preview_deliveries SET state = 'delete_failed', "
        "last_error_code = 'delete_claim_recovered' WHERE state = 'delete_pending'"
    )
    _constraint(
        "ck_context_preview_deliveries_delete_fencing_token_nonnegative",
        "ALTER TABLE context_preview_deliveries ADD CONSTRAINT "
        "ck_context_preview_deliveries_delete_fencing_token_nonnegative "
        "CHECK (delete_fencing_token >= 0)",
    )
    _constraint(
        "ck_context_preview_deliveries_delete_lease_state_match",
        "ALTER TABLE context_preview_deliveries ADD CONSTRAINT "
        "ck_context_preview_deliveries_delete_lease_state_match CHECK "
        "((state = 'delete_pending' AND delete_claimed_at IS NOT NULL AND "
        "delete_lease_expires_at IS NOT NULL AND delete_fencing_token > 0) OR "
        "(state <> 'delete_pending' AND delete_claimed_at IS NULL AND "
        "delete_lease_expires_at IS NULL))",
    )

    op.execute(
        "ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS proactive_decision_id uuid"
    )
    op.execute("ALTER TABLE copilot_drafts ADD COLUMN IF NOT EXISTS proactive_decision_id uuid")
    op.execute(
        "ALTER TABLE copilot_drafts DROP CONSTRAINT IF EXISTS ck_copilot_drafts_draft_kind_v1"
    )
    _constraint(
        "ck_copilot_drafts_draft_kind_provenance",
        "ALTER TABLE copilot_drafts ADD CONSTRAINT "
        "ck_copilot_drafts_draft_kind_provenance CHECK "
        "((draft_kind = 'reactive' AND proactive_decision_id IS NULL) OR "
        "(draft_kind = 'proactive' AND proactive_decision_id IS NOT NULL))",
    )
    _constraint(
        "uq_proactive_decisions_full_scope",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT "
        "uq_proactive_decisions_full_scope UNIQUE (id, account_id, conversation_id)",
    )
    _constraint(
        "fk_outbound_groups_proactive_decision_scope",
        "ALTER TABLE outbound_delivery_groups ADD CONSTRAINT "
        "fk_outbound_groups_proactive_decision_scope FOREIGN KEY "
        "(proactive_decision_id, account_id, conversation_id) REFERENCES "
        "proactive_decisions (id, account_id, conversation_id) DEFERRABLE INITIALLY DEFERRED",
    )
    _constraint(
        "fk_copilot_drafts_proactive_decision_scope",
        "ALTER TABLE copilot_drafts ADD CONSTRAINT "
        "fk_copilot_drafts_proactive_decision_scope FOREIGN KEY "
        "(proactive_decision_id, account_id, conversation_id) REFERENCES "
        "proactive_decisions (id, account_id, conversation_id) DEFERRABLE INITIALLY DEFERRED",
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_groups_proactive_decision "
        "ON outbound_delivery_groups (proactive_decision_id) "
        "WHERE proactive_decision_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_copilot_drafts_proactive_decision "
        "ON copilot_drafts (proactive_decision_id) WHERE proactive_decision_id IS NOT NULL"
    )
    _constraint(
        "ck_outbound_groups_proactive_source",
        "ALTER TABLE outbound_delivery_groups ADD CONSTRAINT "
        "ck_outbound_groups_proactive_source CHECK "
        "((source <> 'proactive_ai' OR proactive_decision_id IS NOT NULL) AND "
        "(proactive_decision_id IS NULL OR source IN ('proactive_ai','copilot_approved')))",
    )
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_reservations_target_side_effect"
    )
    _constraint(
        "ck_proactive_budget_reservations_target_side_effect",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_target_side_effect CHECK "
        "(((target = 'auto_send' AND copilot_draft_id IS NULL) OR "
        "(target = 'copilot_draft' AND outbound_group_id IS NULL)) AND "
        "(state NOT IN ('committed','send_unknown') OR "
        "(target = 'auto_send' AND outbound_group_id IS NOT NULL) OR "
        "(target = 'copilot_draft' AND copilot_draft_id IS NOT NULL))) NOT VALID",
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM proactive_budget_reservations WHERE "
        "state IN ('committed','send_unknown') AND "
        "((target = 'auto_send' AND outbound_group_id IS NULL) OR "
        "(target = 'copilot_draft' AND copilot_draft_id IS NULL))) THEN "
        "ALTER TABLE proactive_budget_reservations VALIDATE CONSTRAINT "
        "ck_proactive_budget_reservations_target_side_effect; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_reservations_target_side_effect"
    )
    _constraint(
        "ck_proactive_budget_reservations_target_side_effect",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_target_side_effect CHECK "
        "((target = 'auto_send' AND copilot_draft_id IS NULL) OR "
        "(target = 'copilot_draft' AND outbound_group_id IS NULL))",
    )
    op.execute(
        "ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS "
        "ck_outbound_groups_proactive_source"
    )
    op.execute("DROP INDEX IF EXISTS uq_copilot_drafts_proactive_decision")
    op.execute("DROP INDEX IF EXISTS uq_outbound_groups_proactive_decision")
    op.execute(
        "ALTER TABLE copilot_drafts DROP CONSTRAINT IF EXISTS "
        "fk_copilot_drafts_proactive_decision_scope"
    )
    op.execute(
        "ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS "
        "fk_outbound_groups_proactive_decision_scope"
    )
    op.execute(
        "ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS "
        "uq_proactive_decisions_full_scope"
    )
    op.execute(
        "ALTER TABLE copilot_drafts DROP CONSTRAINT IF EXISTS "
        "ck_copilot_drafts_draft_kind_provenance"
    )
    _constraint(
        "ck_copilot_drafts_draft_kind_v1",
        "ALTER TABLE copilot_drafts ADD CONSTRAINT "
        "ck_copilot_drafts_draft_kind_v1 CHECK (draft_kind = 'reactive')",
    )
    op.execute("ALTER TABLE copilot_drafts DROP COLUMN IF EXISTS proactive_decision_id")
    op.execute("ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS proactive_decision_id")

    op.execute(
        "ALTER TABLE context_preview_deliveries DROP CONSTRAINT IF EXISTS "
        "ck_context_preview_deliveries_delete_lease_state_match"
    )
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP CONSTRAINT IF EXISTS "
        "ck_context_preview_deliveries_delete_fencing_token_nonnegative"
    )
    op.execute("ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_fencing_token")
    op.execute(
        "ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_lease_expires_at"
    )
    op.execute("ALTER TABLE context_preview_deliveries DROP COLUMN IF EXISTS delete_claimed_at")
