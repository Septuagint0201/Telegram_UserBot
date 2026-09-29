"""Bind proactive output to a delivery turn without changing model-run ownership."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0036_worker_complete"
down_revision: str | None = "0035_memory_period_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DRAFT_BINDING = """
            IF TG_TABLE_NAME = 'copilot_drafts' AND col = 'model_run_id'
              AND previous->>'model_run_id' IS NULL
              AND previous->>'metadata_erased_at' IS NULL
              AND previous->>'state' = 'collecting' AND fresh->>'state' = 'generating'
              AND NOT public.scope_metadata_blocked(TG_TABLE_NAME, fresh)
              THEN CONTINUE; END IF;
"""


def _draft_binding(*, enabled: bool) -> None:
    definition = str(
        op.get_bind().scalar(
            text("SELECT pg_get_functiondef('public.enforce_metadata_erasure()'::regprocedure)")
        )
    )
    anchor = (
        "            IF fresh->col IS DISTINCT FROM previous->col THEN\n"
        "              RAISE EXCEPTION 'ERASURE_METADATA_OWNER_IMMUTABLE'; END IF;"
    )
    if enabled:
        if definition.count(anchor) != 1:
            raise RuntimeError("metadata owner guard definition changed")
        definition = definition.replace(anchor, _DRAFT_BINDING + anchor)
    else:
        definition = definition.replace(_DRAFT_BINDING, "")
    op.execute(definition)


def upgrade() -> None:
    _draft_binding(enabled=True)
    op.execute("ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS delivery_turn_id uuid")
    op.execute("UPDATE model_runs SET delivery_turn_id=turn_id WHERE turn_id IS NOT NULL")
    op.execute("""
      DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='model_runs'::regclass AND
            conname='uq_model_runs_delivery_scope') THEN
          ALTER TABLE model_runs ADD CONSTRAINT uq_model_runs_delivery_scope UNIQUE
            (id,account_id,conversation_id,delivery_turn_id,logical_role);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='model_runs'::regclass AND
            conname='fk_model_runs_delivery_turn') THEN
          ALTER TABLE model_runs ADD CONSTRAINT fk_model_runs_delivery_turn FOREIGN KEY
            (delivery_turn_id,account_id,conversation_id) REFERENCES
            conversation_turns(id,account_id,conversation_id) DEFERRABLE INITIALLY DEFERRED;
        END IF;
      END $$;
      CREATE FUNCTION bind_model_delivery_turn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.turn_id IS NOT NULL THEN NEW.delivery_turn_id := NEW.turn_id;
        ELSIF NEW.delivery_turn_id IS NOT NULL AND (NEW.proactive_job_id IS NULL OR NEW.purpose <>
            'proactive_final') THEN
          RAISE EXCEPTION 'invalid delivery owner';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER model_delivery_turn BEFORE INSERT OR UPDATE ON model_runs FOR EACH ROW
            EXECUTE FUNCTION bind_model_delivery_turn();
    """)
    for table, constraint in (
        ("outbound_delivery_groups", "fk_outbound_groups_model_run_scope"),
        ("outbound_intents", "fk_outbound_intents_model_run_scope"),
        ("copilot_drafts", "fk_copilot_drafts_model_run_scope"),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(
            constraint,
            table,
            "model_runs",
            ["model_run_id", "account_id", "conversation_id", "turn_id", "model_role"],
            ["id", "account_id", "conversation_id", "delivery_turn_id", "logical_role"],
            deferrable=True,
            initially="DEFERRED",
        )
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='telegram_userbot_worker_runtime') THEN
          GRANT SELECT ON accounts, contacts, conversations, account_orchestrator_states,
            conversation_turns, message_events, outbound_delivery_groups, outbound_intents,
            copilot_drafts, copilot_draft_revisions TO telegram_userbot_worker_runtime;
          GRANT INSERT ON conversation_turns, outbound_delivery_groups, outbound_intents,
            copilot_drafts, copilot_draft_revisions TO telegram_userbot_worker_runtime;
          GRANT SELECT, UPDATE(delivery_turn_id) ON model_runs TO telegram_userbot_worker_runtime;
          GRANT UPDATE (updated_at) ON account_orchestrator_states, conversations TO
            telegram_userbot_worker_runtime;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='telegram_userbot_app_runtime') THEN
          GRANT SELECT (id,account_id,conversation_id,status,current_version_no)
            ON memories TO telegram_userbot_app_runtime;
        END IF;
      END $$;
    """)
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='telegram_userbot_worker_runtime') THEN
          GRANT UPDATE (lease_owner,lease_expires_at,state,terminal_reason,completed_at)
            ON conversation_turns TO telegram_userbot_worker_runtime;
          GRANT UPDATE (completed_at) ON outbound_delivery_groups
            TO telegram_userbot_worker_runtime;
        END IF;
      END $$;
    """)
    _revision(revision)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE model_runs, outbound_delivery_groups, outbound_intents, "
        "copilot_drafts IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM model_runs WHERE proactive_job_id IS NOT NULL "
            "AND delivery_turn_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("published proactive output prevents downgrade")
    for table, constraint in (
        ("outbound_delivery_groups", "fk_outbound_groups_model_run_scope"),
        ("outbound_intents", "fk_outbound_intents_model_run_scope"),
        ("copilot_drafts", "fk_copilot_drafts_model_run_scope"),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(
            constraint,
            table,
            "model_runs",
            ["model_run_id", "account_id", "conversation_id", "turn_id", "model_role"],
            ["id", "account_id", "conversation_id", "turn_id", "logical_role"],
            deferrable=True,
            initially="DEFERRED",
        )
    op.execute("DROP TRIGGER model_delivery_turn ON model_runs")
    op.execute("DROP FUNCTION bind_model_delivery_turn()")
    op.drop_constraint("fk_model_runs_delivery_turn", "model_runs", type_="foreignkey")
    op.drop_constraint("uq_model_runs_delivery_scope", "model_runs", type_="unique")
    op.drop_column("model_runs", "delivery_turn_id")
    _draft_binding(enabled=False)
    _revision("0035_memory_period_summaries")


def _revision(value: str) -> None:
    op.drop_constraint(
        op.f("ck_service_instances_schema_revision_current"), "service_instances", type_="check"
    )
    op.execute(
        text("UPDATE service_instances SET schema_revision = :value").bindparams(value=value)
    )
    op.create_check_constraint(
        "schema_revision_current", "service_instances", f"schema_revision = '{value}'"
    )
    op.drop_constraint(
        op.f("ck_service_status_events_schema_revision_current"),
        "service_status_events",
        type_="check",
    )
    condition = next(
        c.sqltext
        for c in metadata.tables["service_status_events"].constraints
        if isinstance(c, CheckConstraint)
        and c.name == "ck_service_status_events_schema_revision_current"
    )
    op.create_check_constraint("schema_revision_current", "service_status_events", str(condition))
