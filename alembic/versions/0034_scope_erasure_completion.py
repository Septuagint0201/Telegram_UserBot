"""Retention redaction, filesystem evidence and resumable restore overlays."""

# SQL identifiers come only from the frozen retention contract.
# ruff: noqa: S608
import json
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.retention_erasure_rules import RETENTION_ERASURE_V1
from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0034_scope_erasure_completion"
down_revision: str | None = "0033_media_upload_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema_revision(value: str) -> None:
    op.execute(
        "ALTER TABLE service_instances DROP CONSTRAINT ck_service_instances_schema_revision_current"
    )
    op.execute(f"UPDATE service_instances SET schema_revision = '{value}'")
    op.create_check_constraint(
        "schema_revision_current", "service_instances", f"schema_revision = '{value}'"
    )
    op.execute(
        "ALTER TABLE service_status_events DROP CONSTRAINT "
        "ck_service_status_events_schema_revision_current"
    )
    condition = next(
        c.sqltext
        for c in metadata.tables["service_status_events"].constraints
        if isinstance(c, CheckConstraint)
        and c.name == "ck_service_status_events_schema_revision_current"
    )
    op.create_check_constraint("schema_revision_current", "service_status_events", str(condition))


def upgrade() -> None:
    guard = str(
        op.get_bind().scalar(
            text("SELECT pg_get_functiondef('public.enforce_metadata_erasure()'::regprocedure)")
        )
    )
    op.execute(
        guard.replace(
            "'control_commands','model_runs','model_run_attempts') THEN",
            "'control_commands','model_runs','model_run_attempts','data_erasure_requests') THEN",
        )
    )
    for name in ("erasure_media_checks", "erasure_restore_replays"):
        metadata.tables[name].create(op.get_bind(), checkfirst=True)
    op.execute("""
      CREATE FUNCTION public.erasure_live_media(account uuid) RETURNS TABLE(id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        WITH RECURSIVE seeds(id) AS (
          SELECT o.id FROM public.media_objects o JOIN public.message_revisions r
            ON r.id=o.source_revision_id AND r.account_id=o.account_id
            JOIN public.messages m ON m.id=r.message_id AND m.account_id=r.account_id
            WHERE o.account_id=account AND r.redacted_at IS NULL AND NOT m.is_tombstone
          UNION
          SELECT a.media_object_id FROM public.message_media a JOIN public.message_revisions r
            ON r.id=a.message_revision_id AND r.account_id=a.account_id
            JOIN public.messages m ON m.id=r.message_id AND m.account_id=r.account_id
            WHERE a.account_id=account AND r.redacted_at IS NULL AND NOT m.is_tombstone
          UNION
          SELECT i.media_object_id FROM public.context_manifest_items i
            JOIN public.context_manifests m ON m.id=i.manifest_id AND m.account_id=i.account_id
            WHERE i.account_id=account AND i.scope_erased_at IS NULL
              AND m.scope_erased_at IS NULL
          UNION
          SELECT i.media_object_id FROM public.memory_input_manifest_items i
            JOIN public.memory_input_manifests m ON m.id=i.manifest_id
              AND m.account_id=i.account_id
            WHERE i.account_id=account AND i.scope_erased_at IS NULL
              AND m.scope_erased_at IS NULL
          UNION
          SELECT e.media_object_id FROM public.memory_evidence e
            JOIN public.memory_versions v ON v.id=e.memory_version_id
              AND v.account_id=e.account_id
            WHERE e.account_id=account AND e.scope_erased_at IS NULL AND v.redacted_at IS NULL
          UNION
          SELECT e.media_object_id FROM public.memory_proposal_evidence e
            JOIN public.memory_proposals p ON p.id=e.proposal_id AND p.account_id=e.account_id
            WHERE e.account_id=account AND e.scope_erased_at IS NULL
              AND p.scope_erased_at IS NULL
        ), family(id,parent_object_id) AS (
          SELECT o.id,o.parent_object_id FROM public.media_objects o
            WHERE o.account_id=account AND o.id IN (SELECT seeds.id FROM seeds)
          UNION
          SELECT o.id,o.parent_object_id FROM public.media_objects o JOIN family f
            ON o.parent_object_id=f.id OR o.id=f.parent_object_id WHERE o.account_id=account
        ) SELECT family.id FROM family;
      $$;
      REVOKE ALL ON FUNCTION public.erasure_live_media(uuid) FROM PUBLIC;
    """)
    commands = []
    for name, rule in RETENTION_ERASURE_V1.items():
        op.execute(f"ALTER TABLE {name} ADD COLUMN IF NOT EXISTS metadata_erased_at timestamptz")
        for col in rule.originally_required:
            op.alter_column(name, col, nullable=True)
        for constraint in metadata.tables[name].constraints:
            if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith(
                ("metadata_erased_empty", "metadata_live_required")
            ):
                op.execute(f"ALTER TABLE {name} DROP CONSTRAINT IF EXISTS {constraint.name}")
                op.create_check_constraint(
                    op.f(str(constraint.name)), name, str(constraint.sqltext)
                )
        values: dict[str, Any] = dict.fromkeys(rule.null_columns)
        values.update({col: {} for col in rule.empty_objects})
        values.update(dict.fromkeys(rule.false_columns, False))
        op.execute(f"""CREATE TRIGGER trg_metadata_erasure BEFORE INSERT OR UPDATE ON {name}
          FOR EACH ROW EXECUTE FUNCTION public.enforce_metadata_erasure(
            '{{{",".join(rule.columns)}}}', '{json.dumps(values)}')""")
        assignments = [f"{col} = NULL" for col in rule.null_columns]
        assignments += [f"{col} = '{{}}'::jsonb" for col in rule.empty_objects]
        assignments += [f"{col} = false" for col in rule.false_columns]
        commands.append(f"""UPDATE public.{name} t SET {", ".join(assignments)},
          metadata_erased_at = erased_at WHERE t.metadata_erased_at IS NULL
          AND public.scope_metadata_matches('{name}',to_jsonb(t),req.account_id,req.contact_id);""")
    op.execute(f"""
      CREATE FUNCTION public.redact_scope_retention(request uuid, erased_at timestamptz)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE req public.data_erasure_requests%ROWTYPE;
      BEGIN
        SELECT * INTO req FROM public.data_erasure_requests WHERE id = request FOR UPDATE;
        IF req.id IS NULL OR req.scope_type NOT IN ('account','contact') OR
           req.state = 'completed' OR erased_at IS NULL THEN
          RAISE EXCEPTION 'ERASURE_RETENTION_REQUEST_INVALID'; END IF;
        PERFORM 1 FROM public.accounts WHERE id = req.account_id FOR NO KEY UPDATE;
        IF (req.scope_type = 'account' AND NOT EXISTS (SELECT 1 FROM public.accounts
              WHERE id = req.account_id AND status = 'deleting')) OR
           (req.scope_type = 'contact' AND NOT EXISTS (SELECT 1 FROM public.contacts
              WHERE id = req.contact_id AND account_id = req.account_id
                AND automation_status = 'deleting')) THEN
          RAISE EXCEPTION 'ERASURE_RETENTION_QUIESCENCE_REQUIRED'; END IF;
        {" ".join(commands)}
      END; $$;
      REVOKE ALL ON FUNCTION public.redact_scope_retention(uuid,timestamptz) FROM PUBLIC;
    """)
    _schema_revision(revision)


def downgrade() -> None:
    names = ", ".join((*RETENTION_ERASURE_V1, "erasure_media_checks", "erasure_restore_replays"))
    op.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
    for name in RETENTION_ERASURE_V1:
        if op.get_bind().scalar(
            text(f"SELECT EXISTS(SELECT 1 FROM {name} WHERE metadata_erased_at IS NOT NULL)")
        ):
            raise RuntimeError("MIGRATION_0034_DOWNGRADE_REQUIRES_NO_COMPLETION")
    for name in ("erasure_media_checks", "erasure_restore_replays"):
        if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {name})")):
            raise RuntimeError("MIGRATION_0034_DOWNGRADE_REQUIRES_NO_COMPLETION")
    op.execute("DROP FUNCTION public.redact_scope_retention(uuid,timestamptz)")
    op.execute("DROP FUNCTION public.erasure_live_media(uuid)")
    guard = str(
        op.get_bind().scalar(
            text("SELECT pg_get_functiondef('public.enforce_metadata_erasure()'::regprocedure)")
        )
    )
    op.execute(
        guard.replace(
            "'control_commands','model_runs','model_run_attempts','data_erasure_requests') THEN",
            "'control_commands','model_runs','model_run_attempts') THEN",
        )
    )
    for name, rule in RETENTION_ERASURE_V1.items():
        op.execute(f"DROP TRIGGER trg_metadata_erasure ON {name}")
        op.drop_constraint(op.f(f"ck_{name}_metadata_erased_empty"), name, type_="check")
        if rule.originally_required:
            op.drop_constraint(op.f(f"ck_{name}_metadata_live_required"), name, type_="check")
        for col in rule.originally_required:
            op.alter_column(name, col, nullable=False)
        op.drop_column(name, "metadata_erased_at")
    op.drop_table("erasure_restore_replays")
    op.drop_table("erasure_media_checks")
    _schema_revision("0033_media_upload_erasure")
