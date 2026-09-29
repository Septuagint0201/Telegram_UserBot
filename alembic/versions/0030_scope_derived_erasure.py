"""One-way derived payload erasure and database admission fencing."""

# All interpolated SQL identifiers come from the frozen migration allowlists.
# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.schema import metadata
from telegram_userbot.adapters.persistence.scope_erasure_rules import PAYLOAD_ERASURE_V1

revision: str = "0030_scope_derived_erasure"
down_revision: str | None = "0029_unsupported_peer_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_FUNCTIONS = (
    "enforce_memory_input_manifest_immutability",
    "enforce_proactive_input_manifest_immutability",
    "enforce_copilot_revision_redaction",
)
_REDACTION_BRANCH = """
          -- The separate scope guard and payload CHECKs validate this transition.
          IF TG_OP = 'UPDATE' AND OLD.scope_erased_at IS NULL
             AND NEW.scope_erased_at IS NOT NULL THEN RETURN NEW; END IF;
"""

# Resolve a row's owner through its existing account-bound foreign keys.
_PARENTS = {
    "memory_versions": ("memories", "memory_id", "id"),
    "memory_evidence": ("memory_versions", "memory_version_id", "id"),
    "memory_proposal_evidence": ("memory_proposals", "proposal_id", "id"),
    "summary_versions": ("summaries", "summary_id", "id"),
    "summary_version_sources": ("summary_versions", "summary_version_id", "id"),
    "memory_input_manifest_items": ("memory_input_manifests", "manifest_id", "id"),
    "context_manifest_items": ("context_manifests", "manifest_id", "id"),
    "proactive_input_manifest_items": ("proactive_input_manifests", "manifest_id", "id"),
    "proactive_occurrence_evidence": ("proactive_occurrences", "occurrence_id", "id"),
    "message_revisions": ("messages", "message_id", "id"),
}
_REFERENCES = {
    "message_revision_id": "message_revisions",
    "memory_version_id": "memory_versions",
    "other_memory_version_id": "memory_versions",
    "accepted_memory_version_id": "memory_versions",
    "summary_version_id": "summary_versions",
    "prior_summary_version_id": "summary_versions",
    "memory_input_manifest_id": "memory_input_manifests",
    "proactive_input_manifest_id": "proactive_input_manifests",
    "context_manifest_id": "context_manifests",
    "model_run_id": "model_runs",
}


def _install_fences() -> None:
    parents = "\n".join(
        f"""WHEN '{name}' THEN
          SELECT to_jsonb(p) INTO parent FROM public.{table} p
          WHERE p.{target} = (row_data->>'{source}')::uuid
            AND p.account_id = (row_data->>'account_id')::uuid;
          RETURN public.scope_erasure_row_blocked('{table}', parent);"""
        for name, (table, source, target) in _PARENTS.items()
    )
    references = "\n".join(
        f"""IF row_data->>'{column}' IS NOT NULL THEN
          SELECT to_jsonb(p) INTO parent FROM public.{table} p
          WHERE p.id = (row_data->>'{column}')::uuid
            AND p.account_id = (row_data->>'account_id')::uuid;
          IF parent IS NOT NULL AND public.scope_erasure_row_blocked('{table}', parent)
            THEN RETURN true; END IF;
        END IF;"""
        for column, table in _REFERENCES.items()
    )
    op.execute(f"""
        CREATE FUNCTION public.scope_erasure_row_blocked(table_name text, row_data jsonb)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE account_status text; target_contact uuid; parent jsonb;
        BEGIN
          IF row_data IS NULL THEN RETURN true; END IF;
          IF row_data->>'scope_erased_at' IS NOT NULL OR
             row_data->>'redacted_at' IS NOT NULL THEN RETURN true; END IF;
          -- Same serialization point as the erasure transaction and Telegram ingress.
          SELECT status INTO account_status FROM public.accounts
            WHERE id = (row_data->>'account_id')::uuid FOR NO KEY UPDATE;
          IF account_status IS NULL OR account_status IN ('deleting','deleted') THEN
            RETURN true;
          END IF;
          target_contact := (row_data->>'contact_id')::uuid;
          IF row_data->>'conversation_id' IS NOT NULL THEN
            SELECT contact_id INTO target_contact FROM public.conversations
              WHERE id = (row_data->>'conversation_id')::uuid
                AND account_id = (row_data->>'account_id')::uuid;
          END IF;
          IF EXISTS (SELECT 1 FROM public.contacts WHERE id = target_contact
                     AND automation_status = 'deleting') OR EXISTS (
            SELECT 1 FROM public.data_erasure_requests
            WHERE account_id = (row_data->>'account_id')::uuid
              AND (scope_type = 'account' OR
                   (scope_type = 'contact' AND contact_id = target_contact))
          ) THEN RETURN true; END IF;
          {references}
          IF row_data->>'source_type' = 'message_revision'
             AND row_data->>'source_id' IS NOT NULL THEN
            SELECT to_jsonb(p) INTO parent FROM public.message_revisions p
              WHERE p.id = (row_data->>'source_id')::uuid
                AND p.account_id = (row_data->>'account_id')::uuid;
            IF public.scope_erasure_row_blocked('message_revisions', parent)
              THEN RETURN true; END IF;
          END IF;
          CASE table_name
            {parents}
            ELSE RETURN false;
          END CASE;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.scope_erasure_row_blocked(text, jsonb) FROM PUBLIC;

        CREATE FUNCTION public.enforce_scope_payload_erasure()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE fresh jsonb := to_jsonb(NEW); previous jsonb;
          payload_columns text[] := TG_ARGV[0]::text[];
          column_name text; changed boolean := TG_OP = 'INSERT';
          introduces_payload boolean := TG_OP = 'INSERT';
          mutable_columns text[] := payload_columns || ARRAY[
            'scope_erased_at', 'redacted_at', 'redaction_reason'];
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            previous := to_jsonb(OLD);
            FOREACH column_name IN ARRAY ARRAY[
              'account_id','contact_id','conversation_id','memory_id','summary_id',
              'manifest_id','occurrence_id','proposal_id','draft_id','message_revision_id',
              'memory_version_id','summary_version_id'
            ] LOOP
              IF fresh->column_name IS DISTINCT FROM previous->column_name THEN
                RAISE EXCEPTION 'ERASURE_OWNER_IMMUTABLE';
              END IF;
            END LOOP;
            IF previous->>'scope_erased_at' IS NOT NULL THEN
              IF fresh->'scope_erased_at' IS DISTINCT FROM previous->'scope_erased_at'
              THEN RAISE EXCEPTION 'ERASURE_PAYLOAD_IMMUTABLE'; END IF;
              FOREACH column_name IN ARRAY mutable_columns LOOP
                IF fresh->column_name IS DISTINCT FROM previous->column_name THEN
                  RAISE EXCEPTION 'ERASURE_PAYLOAD_IMMUTABLE';
                END IF;
              END LOOP;
              RETURN NEW;
            END IF;
            IF fresh->>'scope_erased_at' IS NOT NULL THEN
              IF fresh - mutable_columns IS DISTINCT FROM previous - mutable_columns THEN
                RAISE EXCEPTION 'ERASURE_REDACTION_ONLY';
              END IF;
              RETURN NEW;
            END IF;
            FOREACH column_name IN ARRAY payload_columns LOOP
              changed := changed OR fresh->column_name IS DISTINCT FROM previous->column_name;
              introduces_payload := introduces_payload OR (
                fresh->column_name IS DISTINCT FROM previous->column_name AND
                fresh->column_name IS NOT NULL AND fresh->column_name <> 'null'::jsonb AND
                fresh->column_name <> '{{}}'::jsonb
              );
            END LOOP;
          ELSIF fresh->>'scope_erased_at' IS NOT NULL THEN
            RAISE EXCEPTION 'ERASURE_REQUIRES_EXISTING_ROW';
          END IF;
          IF introduces_payload AND fresh->>'redacted_at' IS NOT NULL THEN
            RAISE EXCEPTION 'ERASURE_PAYLOAD_IMMUTABLE';
          END IF;
          IF changed AND introduces_payload AND
             public.scope_erasure_row_blocked(TG_TABLE_NAME, fresh - 'redacted_at') THEN
            RAISE EXCEPTION 'ERASURE_SCOPE_WRITE_BLOCKED';
          END IF;
          RETURN NEW;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.enforce_scope_payload_erasure() FROM PUBLIC;
    """)
    guarded = {name: rule.columns for name, rule in PAYLOAD_ERASURE_V1.items()}
    guarded["embedding_records"] = ("vector_payload", "source_sha256")
    for name, columns in guarded.items():
        column_array = "{" + ",".join(columns) + "}"
        op.execute(f"""
            CREATE TRIGGER trg_scope_payload_erasure BEFORE INSERT OR UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION public.enforce_scope_payload_erasure('{column_array}')
        """)


def _schema_revision(projection: str) -> None:
    for name in ("service_instances", "service_status_events"):
        op.drop_constraint(op.f(f"ck_{name}_schema_revision_current"), name, type_="check")
    op.get_bind().execute(
        text("UPDATE service_instances SET schema_revision = :revision"), {"revision": projection}
    )
    op.create_check_constraint(
        "schema_revision_current", "service_instances", f"schema_revision = '{projection}'"
    )
    op.create_check_constraint(
        "schema_revision_current",
        "service_status_events",
        "schema_revision IN ('0025_m8_service_status','0026_m8_data_export',"
        "'0027_m8_model_run_claim','0028_m8_background_model_runtime',"
        "'0029_unsupported_peer_events','0030_scope_derived_erasure','0031_scope_export_budget',"
        "'0032_scope_metadata_erasure','0033_media_upload_erasure','0034_scope_erasure_completion','0035_memory_period_summaries','0036_worker_complete')",
    )


def upgrade() -> None:
    # Content hashes cannot remain part of a primary key after redaction.
    op.execute("""
        ALTER TABLE memory_evidence ADD COLUMN IF NOT EXISTS id uuid
          NOT NULL DEFAULT gen_random_uuid();
        ALTER TABLE memory_evidence DROP CONSTRAINT pk_memory_evidence;
        ALTER TABLE memory_evidence ADD CONSTRAINT pk_memory_evidence PRIMARY KEY (id);
        ALTER TABLE memory_evidence DROP CONSTRAINT IF EXISTS uq_memory_evidence_source;
        ALTER TABLE memory_evidence ADD CONSTRAINT uq_memory_evidence_source
          UNIQUE (memory_version_id, evidence_role, source_content_sha256);
    """)
    for name, rule in PAYLOAD_ERASURE_V1.items():
        op.execute(f"ALTER TABLE {name} ADD COLUMN IF NOT EXISTS scope_erased_at timestamptz")
        for column in rule.originally_required:
            op.alter_column(name, column, nullable=True)
        for constraint in metadata.tables[name].constraints:
            if isinstance(constraint, CheckConstraint) and "scope_" in str(constraint.name):
                op.execute(f"ALTER TABLE {name} DROP CONSTRAINT IF EXISTS {constraint.name}")
                op.create_check_constraint(
                    op.f(str(constraint.name)), name, str(constraint.sqltext)
                )
    for suffix in ("seal_hash_match", "role_purpose_decision_match"):
        name = f"ck_proactive_input_manifests_{suffix}"
        constraint = next(
            c for c in metadata.tables["proactive_input_manifests"].constraints if c.name == name
        )
        assert isinstance(constraint, CheckConstraint)
        op.drop_constraint(op.f(name), "proactive_input_manifests", type_="check")
        op.create_check_constraint(op.f(name), "proactive_input_manifests", str(constraint.sqltext))
    for function in _IMMUTABLE_FUNCTIONS:
        definition = op.get_bind().scalar(
            text(f"SELECT pg_get_functiondef('{function}()'::regprocedure)")
        )
        op.execute(str(definition).replace("BEGIN", "BEGIN" + _REDACTION_BRANCH, 1))
    _install_fences()
    _schema_revision(revision)


def downgrade() -> None:
    # An erased payload cannot be reconstructed, so rollback must stop atomically.
    names = ", ".join(PAYLOAD_ERASURE_V1)
    op.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
    for name in PAYLOAD_ERASURE_V1:
        if op.get_bind().scalar(
            text(f"SELECT EXISTS (SELECT 1 FROM {name} WHERE scope_erased_at IS NOT NULL)")
        ):
            raise RuntimeError("MIGRATION_0030_DOWNGRADE_REQUIRES_UNERASED_PAYLOADS")
    op.execute("DROP FUNCTION public.enforce_scope_payload_erasure() CASCADE")
    op.execute("DROP FUNCTION public.scope_erasure_row_blocked(text, jsonb)")
    for function in _IMMUTABLE_FUNCTIONS:
        definition = op.get_bind().scalar(
            text(f"SELECT pg_get_functiondef('{function}()'::regprocedure)")
        )
        op.execute(str(definition).replace(_REDACTION_BRANCH, "", 1))
    for suffix in ("seal_hash_match", "role_purpose_decision_match"):
        name = f"ck_proactive_input_manifests_{suffix}"
        definition = str(
            next(
                c.sqltext
                for c in metadata.tables["proactive_input_manifests"].constraints
                if isinstance(c, CheckConstraint) and c.name == name
            )
        )
        definition = definition.removeprefix(
            "(scope_erased_at IS NOT NULL AND manifest_sha256 IS NULL) OR ("
        )
        if suffix == "seal_hash_match":
            definition = definition[:-1]
        else:
            definition = definition.replace(
                "(scope_erased_at IS NOT NULL OR (decision_snapshot IS NOT NULL "
                "AND decision_snapshot_sha256 IS NOT NULL))",
                "decision_snapshot IS NOT NULL AND decision_snapshot_sha256 IS NOT NULL",
            )
        op.drop_constraint(op.f(name), "proactive_input_manifests", type_="check")
        op.create_check_constraint(op.f(name), "proactive_input_manifests", definition)
    for name, rule in PAYLOAD_ERASURE_V1.items():
        op.drop_constraint(op.f(f"ck_{name}_scope_erased_payload_empty"), name, type_="check")
        if rule.originally_required:
            op.drop_constraint(op.f(f"ck_{name}_scope_live_payload_required"), name, type_="check")
        for column in rule.originally_required:
            op.alter_column(name, column, nullable=False)
        op.drop_column(name, "scope_erased_at")
    op.execute("""
        ALTER TABLE memory_evidence DROP CONSTRAINT pk_memory_evidence;
        ALTER TABLE memory_evidence DROP CONSTRAINT uq_memory_evidence_source;
        ALTER TABLE memory_evidence DROP COLUMN id;
        ALTER TABLE memory_evidence ADD CONSTRAINT pk_memory_evidence
          PRIMARY KEY (memory_version_id, evidence_role, source_content_sha256);
    """)
    _schema_revision("0029_unsupported_peer_events")
