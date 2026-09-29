"""Fence overlapping exports and budget authorization after erasure intent."""

# Identifiers and revisions are fixed migration constants.
# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0031_scope_export_budget"
down_revision: str | None = "0030_scope_derived_erasure"
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
    history = (
        "'0025_m8_service_status','0026_m8_data_export','0027_m8_model_run_claim',"
        "'0028_m8_background_model_runtime','0029_unsupported_peer_events',"
        "'0030_scope_derived_erasure','0031_scope_export_budget','0032_scope_metadata_erasure','0033_media_upload_erasure','0034_scope_erasure_completion','0035_memory_period_summaries','0036_worker_complete'"
    )
    op.create_check_constraint(
        "schema_revision_current", "service_status_events", f"schema_revision IN ({history})"
    )


def upgrade() -> None:
    op.execute("""
      ALTER TABLE data_export_requests ADD COLUMN IF NOT EXISTS erasure_requested_at timestamptz;
      ALTER TABLE data_export_requests ADD COLUMN IF NOT EXISTS erasure_cleaned_at timestamptz;
      ALTER TABLE data_export_requests DROP CONSTRAINT IF EXISTS
        ck_data_export_requests_erasure_cleanup_order;
      ALTER TABLE data_export_requests ADD CONSTRAINT ck_data_export_requests_erasure_cleanup_order
        CHECK (erasure_cleaned_at IS NULL OR
          (erasure_requested_at IS NOT NULL AND erasure_cleaned_at >= erasure_requested_at));

      CREATE FUNCTION public.export_scope_erasure_blocked(row_data jsonb)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE account_status text;
      BEGIN
        SELECT status INTO account_status FROM public.accounts
          WHERE id = (row_data->>'account_id')::uuid FOR NO KEY UPDATE;
        RETURN account_status IS NULL OR account_status = 'deleting' OR EXISTS (
          SELECT 1 FROM public.data_erasure_requests r
          WHERE r.account_id = (row_data->>'account_id')::uuid
          AND (r.scope_type = 'account' OR (r.scope_type = 'contact' AND
            (row_data->>'contact_id' IS NULL OR r.contact_id = (row_data->>'contact_id')::uuid)))
        );
      END; $$;

      CREATE FUNCTION public.enforce_export_erasure() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          IF (NEW.id, NEW.account_id, NEW.contact_id, NEW.attempt_count < OLD.attempt_count)
             IS DISTINCT FROM (OLD.id, OLD.account_id, OLD.contact_id, false) THEN
            RAISE EXCEPTION 'EXPORT_IDENTITY_IMMUTABLE';
          END IF;
          IF (OLD.erasure_requested_at IS NOT NULL AND
              NEW.erasure_requested_at IS DISTINCT FROM OLD.erasure_requested_at) OR
             (OLD.erasure_cleaned_at IS NOT NULL AND
              NEW.erasure_cleaned_at IS DISTINCT FROM OLD.erasure_cleaned_at) THEN
            RAISE EXCEPTION 'EXPORT_ERASURE_IMMUTABLE';
          END IF;
        ELSIF NEW.erasure_requested_at IS NOT NULL OR NEW.erasure_cleaned_at IS NOT NULL THEN
          RAISE EXCEPTION 'EXPORT_ERASURE_REQUIRES_EXISTING_ROW';
        END IF;
        IF NEW.erasure_requested_at IS NOT NULL AND NEW.state IN ('requested','claimed') THEN
          RAISE EXCEPTION 'EXPORT_ERASURE_REQUIRES_TERMINAL';
        END IF;
        IF NEW.state IN ('requested','claimed') OR
           (NEW.state = 'completed' AND (TG_OP = 'INSERT' OR OLD.state <> 'completed')) THEN
          IF NEW.erasure_requested_at IS NOT NULL OR
             public.export_scope_erasure_blocked(to_jsonb(NEW)) THEN
            RAISE EXCEPTION 'ERASURE_EXPORT_BLOCKED';
          END IF;
        END IF;
        RETURN NEW;
      END; $$;
      CREATE TRIGGER trg_export_erasure BEFORE INSERT OR UPDATE ON data_export_requests
        FOR EACH ROW EXECUTE FUNCTION public.enforce_export_erasure();

      CREATE FUNCTION public.serialize_scope_erasure_intent() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      BEGIN
        IF TG_OP = 'UPDATE' AND (NEW.account_id, NEW.scope_type, NEW.contact_id, NEW.memory_id)
          IS DISTINCT FROM (OLD.account_id, OLD.scope_type, OLD.contact_id, OLD.memory_id) THEN
          RAISE EXCEPTION 'ERASURE_IDENTITY_IMMUTABLE';
        END IF;
        IF NEW.scope_type IN ('account','contact') THEN
          PERFORM 1 FROM public.accounts WHERE id = NEW.account_id FOR NO KEY UPDATE;
        END IF;
        RETURN NEW;
      END; $$;
      CREATE TRIGGER trg_serialize_scope_erasure BEFORE INSERT OR UPDATE ON data_erasure_requests
        FOR EACH ROW EXECUTE FUNCTION public.serialize_scope_erasure_intent();

      CREATE FUNCTION public.revoke_scope_exports() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      BEGIN
        IF NEW.scope_type IN ('account','contact') THEN
          UPDATE public.data_export_requests SET
            erasure_requested_at = clock_timestamp(), version = version + 1,
            state = CASE WHEN state IN ('requested','claimed') THEN 'failed' ELSE state END,
            completed_at = CASE WHEN state IN ('requested','claimed') THEN clock_timestamp()
              ELSE completed_at END,
            last_error_code = CASE WHEN state IN ('requested','claimed') THEN 'ERASURE_SCOPE'
              ELSE last_error_code END,
            owner_instance_id = NULL, lease_expires_at = NULL
          WHERE account_id = NEW.account_id AND erasure_requested_at IS NULL
            AND (NEW.scope_type = 'account' OR contact_id IS NULL OR contact_id = NEW.contact_id);
        END IF;
        RETURN NEW;
      END; $$;
      CREATE TRIGGER trg_revoke_scope_exports AFTER INSERT ON data_erasure_requests
        FOR EACH ROW EXECUTE FUNCTION public.revoke_scope_exports();

      CREATE FUNCTION public.enforce_budget_erasure() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          IF (NEW.account_id, NEW.contact_id, NEW.conversation_id, NEW.decision_id,
              NEW.reservation_key, NEW.target, NEW.account_bucket_id, NEW.contact_bucket_id,
              NEW.bypass_bucket_id, NEW.bypass) IS DISTINCT FROM
             (OLD.account_id, OLD.contact_id, OLD.conversation_id, OLD.decision_id,
              OLD.reservation_key, OLD.target, OLD.account_bucket_id, OLD.contact_bucket_id,
              OLD.bypass_bucket_id, OLD.bypass) THEN
            RAISE EXCEPTION 'ERASURE_BUDGET_OWNER_IMMUTABLE';
          END IF;
          -- Receipt settlement remains possible. Fresh/rebound authorization does not.
          IF NEW.state <> 'held' AND (NEW.outbound_group_id, NEW.copilot_draft_id)
             IS NOT DISTINCT FROM (OLD.outbound_group_id, OLD.copilot_draft_id) THEN
            RETURN NEW; END IF;
        END IF;
        IF public.scope_erasure_row_blocked('proactive_budget_reservations', to_jsonb(NEW)) THEN
          RAISE EXCEPTION 'ERASURE_BUDGET_BLOCKED';
        END IF;
        RETURN NEW;
      END; $$;
      CREATE TRIGGER trg_budget_erasure BEFORE INSERT OR UPDATE ON proactive_budget_reservations
        FOR EACH ROW EXECUTE FUNCTION public.enforce_budget_erasure();

      -- Requests predating this migration also invalidate their overlapping exports.
      UPDATE data_export_requests e SET
        erasure_requested_at = clock_timestamp(), version = version + 1,
        state = CASE WHEN state IN ('requested','claimed') THEN 'failed' ELSE state END,
        completed_at = CASE WHEN state IN ('requested','claimed') THEN clock_timestamp()
              ELSE completed_at END,
        last_error_code = CASE WHEN state IN ('requested','claimed') THEN 'ERASURE_SCOPE'
              ELSE last_error_code END,
        owner_instance_id = NULL, lease_expires_at = NULL
      WHERE erasure_requested_at IS NULL AND public.export_scope_erasure_blocked(to_jsonb(e));
    """)
    for name, args in (
        ("export_scope_erasure_blocked", "jsonb"),
        ("enforce_export_erasure", ""),
        ("serialize_scope_erasure_intent", ""),
        ("revoke_scope_exports", ""),
        ("enforce_budget_erasure", ""),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}({args}) FROM PUBLIC")
    _schema_revision(revision)


def downgrade() -> None:
    op.execute("LOCK TABLE data_export_requests, data_erasure_requests IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM data_export_requests "
            "WHERE erasure_requested_at IS NOT NULL)"
        )
    ):
        raise RuntimeError("MIGRATION_0031_DOWNGRADE_REQUIRES_NO_EXPORT_ERASURE")
    for name, args in (
        ("enforce_export_erasure", ""),
        ("export_scope_erasure_blocked", "jsonb"),
        ("serialize_scope_erasure_intent", ""),
        ("revoke_scope_exports", ""),
        ("enforce_budget_erasure", ""),
    ):
        op.execute(f"DROP FUNCTION public.{name}({args}) CASCADE")
    op.drop_constraint(
        op.f("ck_data_export_requests_erasure_cleanup_order"), "data_export_requests"
    )
    op.drop_column("data_export_requests", "erasure_cleaned_at")
    op.drop_column("data_export_requests", "erasure_requested_at")
    _schema_revision("0030_scope_derived_erasure")
