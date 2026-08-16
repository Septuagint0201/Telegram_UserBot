"""Bind M5-M7 state transitions to durable identities and terminal proof."""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_m5_m7_consistency"
down_revision: str | Sequence[str] | None = "0012_worker_lease_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_constraint_if_missing(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - inputs are migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; "
        "END IF; END $$"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_proposals ADD COLUMN IF NOT EXISTS "
        "review_version integer NOT NULL DEFAULT 1"
    )
    op.execute(
        "UPDATE memory_review_actions AS action SET conversation_id = COALESCE("
        "(SELECT proposal.conversation_id FROM memory_proposals AS proposal "
        "WHERE proposal.id = action.proposal_id AND proposal.account_id = action.account_id), "
        "(SELECT memory.conversation_id FROM memories AS memory "
        "WHERE memory.id = action.memory_id AND memory.account_id = action.account_id))"
    )
    op.execute(
        "UPDATE memory_review_actions AS action SET expected_proposal_version = "
        "proposal.review_version FROM memory_proposals AS proposal "
        "WHERE action.proposal_id = proposal.id AND action.account_id = proposal.account_id"
    )
    op.execute(
        "UPDATE memory_review_actions AS action SET expected_memory_version = "
        "memory.current_version_no FROM memories AS memory "
        "WHERE action.memory_id = memory.id AND action.account_id = memory.account_id"
    )
    _add_constraint_if_missing(
        "uq_memories_id_conversation_scope",
        "ALTER TABLE memories ADD CONSTRAINT uq_memories_id_conversation_scope "
        "UNIQUE (id, account_id, conversation_id)",
    )
    _add_constraint_if_missing(
        "uq_memory_proposals_id_conversation_scope",
        "ALTER TABLE memory_proposals ADD CONSTRAINT "
        "uq_memory_proposals_id_conversation_scope UNIQUE (id, account_id, conversation_id)",
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "fk_memory_review_actions_proposal_scope"
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "fk_memory_review_actions_memory_scope"
    )
    op.execute("ALTER TABLE memory_review_actions ALTER COLUMN conversation_id SET NOT NULL")
    _add_constraint_if_missing(
        "fk_memory_review_actions_proposal_scope",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "fk_memory_review_actions_proposal_scope FOREIGN KEY "
        "(proposal_id, account_id, conversation_id) REFERENCES "
        "memory_proposals (id, account_id, conversation_id)",
    )
    _add_constraint_if_missing(
        "fk_memory_review_actions_memory_scope",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "fk_memory_review_actions_memory_scope FOREIGN KEY "
        "(memory_id, account_id, conversation_id) REFERENCES "
        "memories (id, account_id, conversation_id)",
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "ck_memory_review_actions_target_required"
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "ck_memory_review_actions_target_matches_action"
    )
    _add_constraint_if_missing(
        "ck_memory_review_actions_target_matches_action",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "ck_memory_review_actions_target_matches_action CHECK ("
        "(action IN ('accept','reject') AND proposal_id IS NOT NULL AND memory_id IS NULL "
        "AND expected_proposal_version IS NOT NULL AND expected_memory_version IS NULL) OR "
        "(action = 'forget' AND proposal_id IS NULL AND memory_id IS NOT NULL "
        "AND expected_proposal_version IS NULL AND expected_memory_version IS NOT NULL))",
    )
    _add_constraint_if_missing(
        "ck_memory_proposals_review_version_positive",
        "ALTER TABLE memory_proposals ADD CONSTRAINT "
        "ck_memory_proposals_review_version_positive CHECK (review_version > 0)",
    )
    _add_constraint_if_missing(
        "uq_proactive_decisions_candidate",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT "
        "uq_proactive_decisions_candidate UNIQUE (candidate_id)",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS uq_proactive_decisions_candidate"
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "ck_memory_review_actions_target_matches_action"
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "fk_memory_review_actions_proposal_scope"
    )
    op.execute(
        "ALTER TABLE memory_review_actions DROP CONSTRAINT IF EXISTS "
        "fk_memory_review_actions_memory_scope"
    )
    op.execute("ALTER TABLE memory_review_actions ALTER COLUMN conversation_id DROP NOT NULL")
    _add_constraint_if_missing(
        "fk_memory_review_actions_proposal_scope",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "fk_memory_review_actions_proposal_scope FOREIGN KEY (proposal_id, account_id) "
        "REFERENCES memory_proposals (id, account_id)",
    )
    _add_constraint_if_missing(
        "fk_memory_review_actions_memory_scope",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "fk_memory_review_actions_memory_scope FOREIGN KEY (memory_id, account_id) "
        "REFERENCES memories (id, account_id)",
    )
    _add_constraint_if_missing(
        "ck_memory_review_actions_target_required",
        "ALTER TABLE memory_review_actions ADD CONSTRAINT "
        "ck_memory_review_actions_target_required CHECK "
        "(proposal_id IS NOT NULL OR memory_id IS NOT NULL)",
    )
    op.execute(
        "ALTER TABLE memory_proposals DROP CONSTRAINT IF EXISTS "
        "ck_memory_proposals_review_version_positive"
    )
    op.execute(
        "ALTER TABLE memory_proposals DROP CONSTRAINT IF EXISTS "
        "uq_memory_proposals_id_conversation_scope"
    )
    op.execute("ALTER TABLE memories DROP CONSTRAINT IF EXISTS uq_memories_id_conversation_scope")
    op.execute("ALTER TABLE memory_proposals DROP COLUMN IF EXISTS review_version")
