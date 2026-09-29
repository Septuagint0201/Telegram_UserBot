"""Bind uploads before filesystem writes and fence erased media references."""

# All SQL identifiers come from a fixed migration allowlist.
# ruff: noqa: S608
from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0033_media_upload_erasure"
down_revision: str | None = "0032_scope_metadata_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCES = (
    "message_media",
    "context_manifest_items",
    "memory_input_manifest_items",
    "memory_evidence",
    "memory_proposal_evidence",
)


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
    op.execute("ALTER TABLE media_objects ADD COLUMN IF NOT EXISTS source_revision_id uuid")
    op.execute("ALTER TABLE media_objects DROP CONSTRAINT IF EXISTS fk_media_objects_source_scope")
    op.create_foreign_key(
        "fk_media_objects_source_scope",
        "media_objects",
        "message_revisions",
        ["source_revision_id", "account_id"],
        ["id", "account_id"],
    )
    op.execute("""
      CREATE FUNCTION public.enforce_media_upload_erasure() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE source jsonb; parent public.media_objects%ROWTYPE;
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          -- Worker can request cleanup but cannot claim filesystem deletion.
          -- Normalize only a legacy pre-cleanup failure into a claimable state.
          IF OLD.status = 'failed' AND OLD.delete_requested_at IS NULL
             AND NEW.delete_requested_at IS NOT NULL AND NEW.status = OLD.status THEN
            NEW.status := 'rejected';
          END IF;
          IF (NEW.id,NEW.account_id,NEW.parent_object_id,NEW.source_revision_id) IS DISTINCT FROM
             (OLD.id,OLD.account_id,OLD.parent_object_id,OLD.source_revision_id) THEN
            RAISE EXCEPTION 'MEDIA_OWNER_IMMUTABLE'; END IF;
          IF OLD.delete_requested_at IS NOT NULL AND
             NEW.delete_requested_at IS DISTINCT FROM OLD.delete_requested_at THEN
            RAISE EXCEPTION 'MEDIA_ERASURE_IMMUTABLE'; END IF;
          IF OLD.status = 'deleted' AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'MEDIA_ERASURE_IMMUTABLE'; END IF;
          IF OLD.delete_requested_at IS NOT NULL AND NEW.status IN ('pending','ready')
             AND NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'MEDIA_ERASURE_WRITE_BLOCKED'; END IF;
          IF OLD.storage_key IS NOT NULL AND
             (NEW.storage_key,NEW.sha256) IS DISTINCT FROM (OLD.storage_key,OLD.sha256)
             AND NOT (NEW.status = 'deleted' AND NEW.storage_key IS NULL AND NEW.sha256 IS NULL)
             THEN RAISE EXCEPTION 'MEDIA_FILE_IDENTITY_IMMUTABLE'; END IF;
        END IF;
        IF TG_OP = 'INSERT' OR (NEW.status = 'ready' AND OLD.status IS DISTINCT FROM 'ready') THEN
          IF public.scope_erasure_row_blocked('',jsonb_build_object('account_id',NEW.account_id))
            THEN RAISE EXCEPTION 'MEDIA_ERASURE_WRITE_BLOCKED'; END IF;
          IF NEW.source_revision_id IS NOT NULL THEN
            SELECT to_jsonb(r) INTO source FROM public.message_revisions r
              WHERE r.id = NEW.source_revision_id AND r.account_id = NEW.account_id;
            IF public.scope_erasure_row_blocked('message_revisions',source) THEN
              RAISE EXCEPTION 'MEDIA_ERASURE_WRITE_BLOCKED'; END IF;
          END IF;
          IF NEW.parent_object_id IS NOT NULL THEN
            SELECT * INTO parent FROM public.media_objects WHERE id = NEW.parent_object_id
              AND account_id = NEW.account_id FOR SHARE;
            IF parent.id IS NULL OR parent.delete_requested_at IS NOT NULL
               OR parent.status IN ('delete_pending','deleted','failed') THEN
              RAISE EXCEPTION 'MEDIA_ERASURE_WRITE_BLOCKED'; END IF;
            IF parent.source_revision_id IS DISTINCT FROM NEW.source_revision_id THEN
              RAISE EXCEPTION 'MEDIA_SOURCE_MISMATCH'; END IF;
          END IF;
        END IF;
        RETURN NEW;
      END; $$;
      CREATE TRIGGER trg_media_upload_erasure BEFORE INSERT OR UPDATE ON public.media_objects
        FOR EACH ROW EXECUTE FUNCTION public.enforce_media_upload_erasure();

      CREATE FUNCTION public.enforce_media_reference_erasure() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      DECLARE object public.media_objects%ROWTYPE;
      BEGIN
        IF NEW.media_object_id IS NULL THEN RETURN NEW; END IF;
        IF TG_OP = 'UPDATE' AND NEW.media_object_id IS NOT DISTINCT FROM OLD.media_object_id
          THEN RETURN NEW; END IF;
        IF public.scope_erasure_row_blocked(TG_TABLE_NAME,to_jsonb(NEW)) THEN
          RAISE EXCEPTION 'MEDIA_ERASURE_REFERENCE_BLOCKED'; END IF;
        SELECT * INTO object FROM public.media_objects
          WHERE id = NEW.media_object_id AND account_id = NEW.account_id FOR SHARE;
        IF object.id IS NULL OR object.delete_requested_at IS NOT NULL
           OR object.status IN ('delete_pending','deleted','failed') THEN
          RAISE EXCEPTION 'MEDIA_ERASURE_REFERENCE_BLOCKED'; END IF;
        RETURN NEW;
      END; $$;
      REVOKE ALL ON FUNCTION public.enforce_media_upload_erasure() FROM PUBLIC;
      REVOKE ALL ON FUNCTION public.enforce_media_reference_erasure() FROM PUBLIC;
    """)
    for name in _REFERENCES:
        op.execute(f"""CREATE TRIGGER trg_media_reference_erasure BEFORE INSERT OR UPDATE
          ON public.{name} FOR EACH ROW EXECUTE FUNCTION public.enforce_media_reference_erasure()
        """)
    _schema_revision(revision)


def downgrade() -> None:
    op.execute("LOCK TABLE media_objects IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        text("""SELECT EXISTS (SELECT 1 FROM media_objects
        WHERE source_revision_id IS NOT NULL OR delete_requested_at IS NOT NULL)""")
    ):
        raise RuntimeError("MIGRATION_0033_DOWNGRADE_REQUIRES_NO_MEDIA_CLEANUP")
    op.execute("DROP FUNCTION public.enforce_media_reference_erasure() CASCADE")
    op.execute("DROP FUNCTION public.enforce_media_upload_erasure() CASCADE")
    op.drop_constraint("fk_media_objects_source_scope", "media_objects", type_="foreignkey")
    op.drop_column("media_objects", "source_revision_id")
    _schema_revision("0032_scope_metadata_erasure")
