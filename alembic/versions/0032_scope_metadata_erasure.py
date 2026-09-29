"""Scope-bound profile, operational payload and audit metadata erasure."""

# SQL identifiers are exclusively from the frozen rule and routing allowlists.
# ruff: noqa: S608

import json
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.metadata_erasure_rules import (
    METADATA_DELETE_V1,
    METADATA_ERASURE_V1,
)
from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0032_scope_metadata_erasure"
down_revision: str | None = "0031_scope_export_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARENTS = {
    "contact": "contacts",
    "conversation": "conversations",
    "message": "messages",
    "message_revision": "message_revisions",
    "memory": "memories",
    "summary": "summaries",
    "model_run": "model_runs",
    "turn": "conversation_turns",
    "copilot_draft": "copilot_drafts",
    "control_command": "control_commands",
    "erasure_request": "data_erasure_requests",
    "background_job": "background_jobs",
}


def _install_scope_resolution() -> None:
    branches = "\n".join(
        f"""WHEN '{kind}' THEN SELECT to_jsonb(p) INTO parent FROM public.{table} p
          WHERE p.id::text = target AND p.account_id::text = row_data->>'account_id';
          IF parent IS NOT NULL THEN
            RETURN public.scope_metadata_owner('{table}', parent); END IF;"""
        for kind, table in _PARENTS.items()
    )
    op.execute(f"""
      CREATE FUNCTION public.scope_metadata_owner(table_name text, row_data jsonb)
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE owner_data jsonb; parent jsonb; target text; kind text; contact uuid;
      BEGIN
        owner_data := jsonb_build_object('account_id', row_data->>'account_id',
          'contact_id', row_data->>'contact_id', 'conversation_id', row_data->>'conversation_id',
          'scope_erased_at', COALESCE(row_data->>'metadata_erased_at',
                                     row_data->>'scope_erased_at'));
        IF table_name = 'accounts' THEN
          RETURN owner_data || jsonb_build_object('account_id',row_data->>'id');
        ELSIF table_name = 'contacts' THEN
          RETURN owner_data || jsonb_build_object('contact_id',row_data->>'id');
        ELSIF table_name = 'account_peers' THEN
          SELECT c.id INTO contact FROM public.contacts c JOIN public.account_peers p
            ON p.id = c.account_peer_id AND p.account_id = c.account_id
            WHERE c.account_id::text = row_data->>'account_id'
              AND (p.id::text = row_data->>'id' OR p.peer_id::text = row_data->>'peer_id');
          RETURN owner_data || jsonb_build_object('contact_id',contact);
        ELSIF table_name = 'model_run_attempts' THEN
          SELECT to_jsonb(p) INTO parent FROM public.model_runs p
            WHERE p.id::text = row_data->>'model_run_id';
          RETURN public.scope_metadata_owner('model_runs',parent);
        ELSIF table_name IN ('message_media','message_revisions') THEN
          kind := CASE WHEN table_name = 'message_media' THEN 'message_revision' ELSE 'message' END;
          target := CASE WHEN table_name = 'message_media' THEN row_data->>'message_revision_id'
                         ELSE row_data->>'message_id' END;
        ELSIF table_name = 'message_reactions' THEN
          kind := 'message'; target := row_data->>'message_id';
        ELSIF table_name = 'background_jobs' THEN
          IF row_data->'payload'->>'conversation_id' IS NOT NULL THEN
            kind := 'conversation'; target := row_data->'payload'->>'conversation_id';
          ELSIF row_data->'payload'->>'contact_id' IS NOT NULL THEN
            kind := 'contact'; target := row_data->'payload'->>'contact_id';
          ELSIF row_data->'payload'->>'turn_id' IS NOT NULL THEN
            kind := 'turn'; target := row_data->'payload'->>'turn_id';
          ELSIF row_data->'payload'->>'message_id' IS NOT NULL THEN
            kind := 'message'; target := row_data->'payload'->>'message_id';
          ELSIF row_data->'payload'->>'request_id' IS NOT NULL THEN
            kind := 'erasure_request'; target := row_data->'payload'->>'request_id';
          END IF;
        ELSIF table_name IN ('transactional_outbox','audit_log') THEN
          kind := COALESCE(row_data->>'aggregate_type', row_data->>'target_type');
          target := COALESCE(row_data->>'aggregate_id', row_data->>'target_id');
          IF kind = 'conversation_control' THEN kind := 'conversation'; END IF;
        END IF;
        CASE kind
          {branches}
          ELSE NULL;
        END CASE;
        RETURN owner_data;
      END; $$;

      CREATE FUNCTION public.scope_metadata_cleanup_notice(table_name text, row_data jsonb)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE parent jsonb;
      BEGIN
        IF table_name = 'background_jobs' THEN
          RETURN row_data->>'job_type' = 'memory.reconcile_erasure'
            AND row_data->>'queue_name' = 'worker'
            AND row_data->'payload' = jsonb_build_object('request_id',row_data->>'id')
            AND EXISTS (SELECT 1 FROM public.data_erasure_requests r
              WHERE r.id::text = row_data->>'id' AND r.account_id::text = row_data->>'account_id');
        ELSIF table_name = 'transactional_outbox' AND row_data->>'aggregate_type' = 'background_job'
           AND row_data->>'topic' = 'durable_job.available' THEN
          SELECT to_jsonb(j) INTO parent FROM public.background_jobs j
            WHERE j.id::text = row_data->>'aggregate_id'
              AND j.account_id::text = row_data->>'account_id';
          RETURN parent IS NOT NULL
            AND row_data->'payload' = jsonb_build_object('job_id',parent->>'id',
                'dispatch_generation', (row_data->>'aggregate_version')::bigint)
            AND public.scope_metadata_cleanup_notice('background_jobs',parent);
        END IF;
        RETURN false;
      END; $$;

      CREATE FUNCTION public.scope_metadata_blocked(table_name text, row_data jsonb)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE owner_data jsonb;
      BEGIN
        IF public.scope_metadata_cleanup_notice(table_name,row_data) THEN RETURN false; END IF;
        owner_data := public.scope_metadata_owner(table_name,row_data);
        IF owner_data->>'account_id' IS NULL THEN RETURN false; END IF;
        RETURN public.scope_erasure_row_blocked('',owner_data);
      END; $$;
    """)


def _install_guard() -> None:
    op.execute("""
      CREATE FUNCTION public.enforce_metadata_erasure() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE fresh jsonb := to_jsonb(NEW); previous jsonb; col text;
        columns text[] := TG_ARGV[0]::text[]; empty_values jsonb := TG_ARGV[1]::jsonb;
        changed boolean := TG_OP = 'INSERT'; introduces boolean := TG_OP = 'INSERT';
        blocked boolean;
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          previous := to_jsonb(OLD);
          FOREACH col IN ARRAY ARRAY['id','account_id','contact_id','conversation_id','peer_id',
             'message_id','message_revision_id','model_run_id','aggregate_type','aggregate_id',
             'target_type','target_id','job_type'] LOOP
            IF fresh->col IS DISTINCT FROM previous->col THEN
              RAISE EXCEPTION 'ERASURE_METADATA_OWNER_IMMUTABLE'; END IF;
          END LOOP;
          IF previous->>'metadata_erased_at' IS NOT NULL THEN
            IF fresh->'metadata_erased_at' IS DISTINCT FROM previous->'metadata_erased_at' THEN
              RAISE EXCEPTION 'ERASURE_METADATA_IMMUTABLE'; END IF;
            -- Preserve terminal outcomes and accounting from already-running requests.
            IF TG_TABLE_NAME IN ('model_runs','model_run_attempts','control_commands') THEN
              NEW := jsonb_populate_record(NEW, empty_values);
              fresh := to_jsonb(NEW);
            END IF;
            FOREACH col IN ARRAY columns LOOP
              IF fresh->col IS DISTINCT FROM previous->col THEN
                RAISE EXCEPTION 'ERASURE_METADATA_IMMUTABLE'; END IF;
            END LOOP;
            IF TG_TABLE_NAME = 'background_jobs'
              AND fresh->>'state' IN ('pending','leased','retry_wait')
              THEN RAISE EXCEPTION 'ERASURE_METADATA_JOB_CANCELLED'; END IF;
            IF TG_TABLE_NAME = 'transactional_outbox' AND fresh->>'published_at' IS NULL
              THEN RAISE EXCEPTION 'ERASURE_METADATA_OUTBOX_CANCELLED'; END IF;
            RETURN NEW;
          END IF;
          FOREACH col IN ARRAY columns LOOP
            changed := changed OR fresh->col IS DISTINCT FROM previous->col;
            introduces := introduces OR (fresh->col IS DISTINCT FROM previous->col AND
              fresh->col IS DISTINCT FROM empty_values->col);
          END LOOP;
          IF fresh->>'metadata_erased_at' IS NOT NULL THEN
            IF NOT public.scope_metadata_blocked(TG_TABLE_NAME, previous) THEN
              RAISE EXCEPTION 'ERASURE_METADATA_SCOPE_REQUIRED'; END IF;
            IF TG_TABLE_NAME = 'background_jobs' THEN
              columns := columns || ARRAY['state','lease_owner','lease_expires_at','completed_at',
                                           'version','fencing_token','last_error_code'];
            ELSIF TG_TABLE_NAME = 'transactional_outbox' THEN
              columns := columns || ARRAY['published_at','last_error_code'];
            END IF;
            IF fresh - (columns || ARRAY['metadata_erased_at']) IS DISTINCT FROM
               previous - (columns || ARRAY['metadata_erased_at']) THEN
              RAISE EXCEPTION 'ERASURE_METADATA_REDACTION_ONLY'; END IF;
            RETURN NEW;
          END IF;
        ELSIF fresh->>'metadata_erased_at' IS NOT NULL THEN
          RAISE EXCEPTION 'ERASURE_METADATA_REQUIRES_EXISTING_ROW';
        END IF;
        -- A new account cannot yet have an erasure request or an account row to lock.
        -- Existing identities (including INSERT ON CONFLICT) still use the guard below.
        IF TG_OP = 'INSERT' AND TG_TABLE_NAME = 'accounts' AND NOT EXISTS (
          SELECT 1 FROM public.accounts WHERE id::text = fresh->>'id') THEN RETURN NEW; END IF;
        IF TG_TABLE_NAME = 'background_jobs'
          AND fresh->>'state' IN ('pending','leased','retry_wait')
          AND public.scope_metadata_blocked(TG_TABLE_NAME,fresh) THEN
          RAISE EXCEPTION 'ERASURE_METADATA_WRITE_BLOCKED'; END IF;
        IF changed AND introduces THEN
          blocked := public.scope_metadata_blocked(TG_TABLE_NAME,fresh);
          IF TG_OP = 'UPDATE' THEN
            blocked := blocked OR public.scope_metadata_blocked(TG_TABLE_NAME,previous);
          END IF;
          IF blocked THEN
            -- Retain append-only facts and control results without attaching private metadata.
            IF TG_TABLE_NAME IN ('audit_log','message_events','conversation_mode_history',
                'account_control_history','control_commands','model_runs','model_run_attempts') THEN
              NEW := jsonb_populate_record(NEW, empty_values ||
                jsonb_build_object('metadata_erased_at',clock_timestamp()));
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'ERASURE_METADATA_WRITE_BLOCKED';
          END IF;
        END IF;
        RETURN NEW;
      END; $$;

      CREATE FUNCTION public.enforce_metadata_deleted_row() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE col text;
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          FOREACH col IN ARRAY ARRAY['id','account_id','conversation_id','message_id','draft_id']
          LOOP
            IF to_jsonb(NEW)->col IS DISTINCT FROM to_jsonb(OLD)->col THEN
              RAISE EXCEPTION 'ERASURE_METADATA_OWNER_IMMUTABLE'; END IF;
          END LOOP;
          IF public.scope_metadata_blocked(TG_TABLE_NAME,to_jsonb(OLD)) THEN
            RAISE EXCEPTION 'ERASURE_METADATA_WRITE_BLOCKED'; END IF;
        END IF;
        IF public.scope_metadata_blocked(TG_TABLE_NAME,to_jsonb(NEW)) THEN
          RAISE EXCEPTION 'ERASURE_METADATA_WRITE_BLOCKED'; END IF;
        RETURN NEW;
      END; $$;
    """)
    for name, rule in METADATA_ERASURE_V1.items():
        values: dict[str, Any] = dict.fromkeys(rule.null_columns)
        values.update({col: {} for col in rule.empty_objects})
        values.update(dict.fromkeys(rule.false_columns, False))
        op.execute(f"""CREATE TRIGGER trg_metadata_erasure BEFORE INSERT OR UPDATE ON {name}
          FOR EACH ROW EXECUTE FUNCTION public.enforce_metadata_erasure(
            '{{{",".join(rule.columns)}}}', '{json.dumps(values)}')""")
    for name in METADATA_DELETE_V1:
        op.execute(f"""CREATE TRIGGER trg_metadata_deleted_row BEFORE INSERT OR UPDATE ON {name}
          FOR EACH ROW EXECUTE FUNCTION public.enforce_metadata_deleted_row()""")


def _install_redactor() -> None:
    commands = []
    # Resolve audit/outbox ownership before erasing job routing payloads.
    for name in sorted(METADATA_ERASURE_V1, key=lambda value: value == "background_jobs"):
        rule = METADATA_ERASURE_V1[name]
        assignments = [f"{col} = NULL" for col in rule.null_columns]
        assignments += [f"{col} = '{{}}'::jsonb" for col in rule.empty_objects]
        assignments += [f"{col} = false" for col in rule.false_columns]
        assignments += ["metadata_erased_at = erased_at"]
        if name == "background_jobs":
            assignments += [
                "state = CASE WHEN state IN ('pending','leased','retry_wait') "
                "THEN 'cancelled' ELSE state END",
                "lease_owner = NULL",
                "lease_expires_at = NULL",
                "completed_at = COALESCE(completed_at,erased_at)",
                "version = version + 1",
                "fencing_token = fencing_token + 1",
                "last_error_code = 'ERASURE_SCOPE'",
            ]
        elif name == "transactional_outbox":
            assignments += [
                "published_at = COALESCE(published_at,erased_at)",
                "last_error_code = 'ERASURE_SCOPE'",
            ]
        commands.append(f"""UPDATE public.{name} t SET {", ".join(assignments)}
          WHERE t.metadata_erased_at IS NULL AND public.scope_metadata_matches(
            '{name}', to_jsonb(t), req.account_id, req.contact_id);""")
    commands.extend(
        f"""DELETE FROM public.{name} t WHERE public.scope_metadata_matches(
          '{name}',to_jsonb(t),req.account_id,req.contact_id);"""
        for name in METADATA_DELETE_V1
    )
    op.execute(f"""
      CREATE FUNCTION public.scope_metadata_matches(table_name text, row_data jsonb,
        account uuid, contact uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path = pg_catalog, public AS $$
      DECLARE owner_data jsonb;
      BEGIN
        IF public.scope_metadata_cleanup_notice(table_name,row_data) THEN RETURN false; END IF;
        owner_data := public.scope_metadata_owner(table_name,row_data);
        IF owner_data->>'account_id' IS DISTINCT FROM account::text THEN RETURN false; END IF;
        IF contact IS NULL THEN RETURN true; END IF;
        IF table_name IN ('model_runs','model_run_attempts')
          AND owner_data->>'scope_erased_at' IS NOT NULL
          THEN RETURN true; END IF;
        RETURN owner_data->>'contact_id' = contact::text OR EXISTS (
          SELECT 1 FROM public.conversations c WHERE c.id::text = owner_data->>'conversation_id'
            AND c.account_id = account AND c.contact_id = contact);
      END; $$;

      CREATE FUNCTION public.redact_scope_metadata(request uuid, erased_at timestamptz)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE req public.data_erasure_requests%ROWTYPE;
      BEGIN
        SELECT * INTO req FROM public.data_erasure_requests WHERE id = request FOR UPDATE;
        IF req.id IS NULL OR req.scope_type NOT IN ('account','contact') OR
          req.state = 'completed' OR (req.state = 'failed' AND
            req.last_error_code IS DISTINCT FROM 'ERASURE_SCOPE_PIPELINE_UNAVAILABLE')
          OR erased_at IS NULL THEN
          RAISE EXCEPTION 'ERASURE_METADATA_REQUEST_INVALID'; END IF;
        PERFORM 1 FROM public.accounts WHERE id = req.account_id FOR NO KEY UPDATE;
        IF req.scope_type = 'account' AND NOT EXISTS (SELECT 1 FROM public.accounts
          WHERE id = req.account_id AND status = 'deleting') THEN
          RAISE EXCEPTION 'ERASURE_METADATA_QUIESCENCE_REQUIRED'; END IF;
        IF req.scope_type = 'contact' AND NOT EXISTS (SELECT 1 FROM public.contacts
          WHERE id = req.contact_id AND account_id = req.account_id
            AND automation_status = 'deleting') THEN
          RAISE EXCEPTION 'ERASURE_METADATA_QUIESCENCE_REQUIRED'; END IF;
        {" ".join(commands)}
        IF NOT EXISTS (SELECT 1 FROM public.audit_log WHERE request_id = request
                       AND action = 'scope_metadata_redacted') THEN
          INSERT INTO public.audit_log (account_id,actor_type,action,target_type,target_id,
            result,request_id,metadata_schema_version,metadata,occurred_at)
          VALUES (req.account_id,'system','scope_metadata_redacted','erasure_request',request::text,
            'success',request,1,'{{}}',erased_at);
        END IF;
      END; $$;
    """)


_FUNCTIONS = {
    "scope_metadata_owner": "text,jsonb",
    "scope_metadata_cleanup_notice": "text,jsonb",
    "scope_metadata_blocked": "text,jsonb",
    "enforce_metadata_erasure": "",
    "enforce_metadata_deleted_row": "",
    "scope_metadata_matches": "text,jsonb,uuid,uuid",
    "redact_scope_metadata": "uuid,timestamptz",
}


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
    for name, rule in METADATA_ERASURE_V1.items():
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
    _install_scope_resolution()
    _install_guard()
    _install_redactor()
    for name, args in _FUNCTIONS.items():
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}({args}) FROM PUBLIC")
    _schema_revision(revision)


def downgrade() -> None:
    names = ", ".join(METADATA_ERASURE_V1)
    op.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
    for name in METADATA_ERASURE_V1:
        if op.get_bind().scalar(
            text(f"SELECT EXISTS(SELECT 1 FROM {name} WHERE metadata_erased_at IS NOT NULL)")
        ):
            raise RuntimeError("MIGRATION_0032_DOWNGRADE_REQUIRES_UNERASED_METADATA")
    for name, args in reversed(tuple(_FUNCTIONS.items())):
        op.execute(f"DROP FUNCTION public.{name}({args}) CASCADE")
    for name, rule in METADATA_ERASURE_V1.items():
        op.drop_constraint(op.f(f"ck_{name}_metadata_erased_empty"), name, type_="check")
        if rule.originally_required:
            op.drop_constraint(op.f(f"ck_{name}_metadata_live_required"), name, type_="check")
        for col in rule.originally_required:
            op.alter_column(name, col, nullable=False)
        op.drop_column(name, "metadata_erased_at")
    _schema_revision("0031_scope_export_budget")
