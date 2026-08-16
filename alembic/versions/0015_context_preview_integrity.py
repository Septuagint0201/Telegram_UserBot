"""Keep exact-manifest preview current across M5 and M6 source types."""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_context_preview_integrity"
down_revision: str | Sequence[str] | None = "0014_m7_budget_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FUNCTION_HEADER = """
CREATE OR REPLACE FUNCTION public.context_preview_sources(
  preview_request_id uuid,
  preview_admin_user_id bigint,
  preview_bot_chat_id bigint,
  preview_bot_identity text
) RETURNS TABLE (
  ordinal integer,
  layer text,
  canonical_role text,
  source_actor text,
  source_type text,
  source_id uuid,
  source_revision text,
  trust_level text,
  image_detail text,
  content_sha256 bytea,
  rendered_part_sha256 bytea,
  source_content text,
  source_eligible boolean,
  source_revision_vector_sha256 bytea
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
"""

_SCOPED_FUNCTION_BODY = """
  SELECT i.ordinal, i.layer, i.canonical_role, i.source_actor,
         i.source_type, i.source_id, i.source_revision, i.trust_level,
         i.image_detail, i.content_sha256, i.rendered_part_sha256,
         CASE i.source_type
           WHEN 'trusted_instruction' THEN pv.template_body
           WHEN 'message_revision' THEN COALESCE(mr.text_content, mr.caption)
           WHEN 'media_object' THEN format(
             '[IMAGE media_object_id=%s sha256=%s mime=%s width=%s height=%s detail=auto]',
             mo.id, encode(mo.sha256, 'hex'), mo.validated_mime, mo.width, mo.height)
           WHEN 'memory_version' THEN mv.rendered_text
           WHEN 'summary_version' THEN sv.content_text
         END AS source_content,
         CASE i.source_type
           WHEN 'trusted_instruction' THEN
             pv.id IS NOT NULL
             AND pv.template_sha256 = i.content_sha256
             AND i.source_revision = 'version-' || pv.version_no::text
           WHEN 'message_revision' THEN
             mr.id IS NOT NULL AND mr.redacted_at IS NULL
             AND mr.content_sha256 = i.content_sha256
             AND msg.deleted_at IS NULL AND NOT msg.is_tombstone
             AND msg.current_revision_no = mr.revision_no
             AND i.source_revision = 'revision-' || mr.revision_no::text
           WHEN 'media_object' THEN
             mo.id IS NOT NULL AND mo.status = 'ready' AND mo.sha256 IS NOT NULL
             AND (mo.expires_at IS NULL OR mo.expires_at > CURRENT_TIMESTAMP)
             AND i.source_revision = 'sha256-' || encode(mo.sha256, 'hex')
             AND EXISTS (
               SELECT 1
               FROM public.message_media mm
               JOIN public.message_revisions mmr
                 ON mmr.id = mm.message_revision_id
                AND mmr.account_id = mm.account_id
               JOIN public.messages mmsg
                 ON mmsg.id = mmr.message_id
                AND mmsg.account_id = mmr.account_id
               WHERE mm.account_id = i.account_id
                 AND mm.media_object_id IN (mo.id, mo.parent_object_id)
                 AND mmr.redacted_at IS NULL
                 AND mmsg.deleted_at IS NULL AND NOT mmsg.is_tombstone
                 AND mmsg.current_revision_no = mmr.revision_no)
           WHEN 'memory_version' THEN
             mv.id IS NOT NULL AND mv.redacted_at IS NULL
             AND mv.rendered_text IS NOT NULL
             AND mem.status = 'active'
             AND mem.current_version_no = mv.version_no
             AND i.source_revision = 'version-' || mv.version_no::text
           WHEN 'summary_version' THEN
             sv.id IS NOT NULL AND sv.redacted_at IS NULL
             AND sv.content_text IS NOT NULL AND sv.content_sha256 = i.content_sha256
             AND sv.invalidation_state = 'active' AND summary.status = 'active'
             AND summary.current_version_no = sv.version_no
             AND i.source_revision = 'version-' || sv.version_no::text
           ELSE false
         END AS source_eligible,
         r.source_revision_vector_sha256
  FROM public.context_preview_requests r
  JOIN public.context_manifests m
    ON m.id = r.context_manifest_id AND m.account_id = r.account_id
  JOIN public.context_manifest_items i
    ON i.manifest_id = m.id AND i.account_id = m.account_id
  LEFT JOIN public.prompt_versions pv ON pv.id = i.prompt_version_id
  LEFT JOIN public.message_revisions mr
    ON mr.id = i.message_revision_id AND mr.account_id = i.account_id
  LEFT JOIN public.messages msg
    ON msg.id = mr.message_id AND msg.account_id = mr.account_id
  LEFT JOIN public.media_objects mo
    ON mo.id = i.media_object_id AND mo.account_id = i.account_id
  LEFT JOIN public.memory_versions mv
    ON mv.id = i.memory_version_id AND mv.account_id = i.account_id
  LEFT JOIN public.memories mem
    ON mem.id = mv.memory_id AND mem.account_id = mv.account_id
  LEFT JOIN public.summary_versions sv
    ON sv.id = i.summary_version_id AND sv.account_id = i.account_id
  LEFT JOIN public.summaries summary
    ON summary.id = sv.summary_id AND summary.account_id = sv.account_id
  WHERE r.id = preview_request_id
    AND r.admin_user_id = preview_admin_user_id
    AND r.bot_chat_id = preview_bot_chat_id
    AND r.bot_identity = preview_bot_identity
    AND r.state = 'confirmed'
    AND r.manifest_sha256 = m.manifest_sha256
    AND r.source_revision_vector_sha256 = m.source_revision_vector_sha256
  ORDER BY i.ordinal
$function$;
"""

_LEGACY_FUNCTION_BODY = """
  SELECT i.ordinal, i.layer, i.canonical_role, i.source_actor,
         i.source_type, i.source_id, i.source_revision, i.trust_level,
         i.image_detail, i.content_sha256, i.rendered_part_sha256,
         CASE i.source_type
           WHEN 'trusted_instruction' THEN pv.template_body
           WHEN 'message_revision' THEN COALESCE(mr.text_content, mr.caption)
           WHEN 'media_object' THEN format(
             '[IMAGE media_object_id=%s sha256=%s mime=%s width=%s height=%s detail=auto]',
             mo.id, encode(mo.sha256, 'hex'), mo.validated_mime, mo.width, mo.height)
         END AS source_content,
         CASE i.source_type
           WHEN 'trusted_instruction' THEN
             pv.id IS NOT NULL AND i.source_revision = 'version-' || pv.version_no::text
           WHEN 'message_revision' THEN
             mr.id IS NOT NULL AND mr.redacted_at IS NULL
             AND mr.content_sha256 IS NOT NULL
             AND msg.current_revision_no = mr.revision_no AND NOT msg.is_tombstone
             AND i.source_revision = 'revision-' || mr.revision_no::text
           WHEN 'media_object' THEN
             mo.id IS NOT NULL AND mo.status = 'ready' AND mo.sha256 IS NOT NULL
             AND i.source_revision = 'sha256-' || encode(mo.sha256, 'hex')
             AND EXISTS (
               SELECT 1
               FROM public.message_media mm
               JOIN public.message_revisions mmr ON mmr.id = mm.message_revision_id
               JOIN public.messages mmsg ON mmsg.id = mmr.message_id
                 AND mmsg.account_id = mmr.account_id
               WHERE mm.media_object_id IN (mo.id, mo.parent_object_id)
                 AND mmr.redacted_at IS NULL
                 AND mmsg.current_revision_no = mmr.revision_no
                 AND NOT mmsg.is_tombstone)
           ELSE false
         END AS source_eligible,
         r.source_revision_vector_sha256
  FROM public.context_preview_requests r
  JOIN public.context_manifests m ON m.id = r.context_manifest_id
  JOIN public.context_manifest_items i ON i.manifest_id = m.id
  LEFT JOIN public.prompt_versions pv ON pv.id = i.prompt_version_id
  LEFT JOIN public.message_revisions mr ON mr.id = i.message_revision_id
  LEFT JOIN public.messages msg ON msg.id = mr.message_id AND msg.account_id = mr.account_id
  LEFT JOIN public.media_objects mo ON mo.id = i.media_object_id
  WHERE r.id = preview_request_id
    AND r.admin_user_id = preview_admin_user_id
    AND r.bot_chat_id = preview_bot_chat_id
    AND r.bot_identity = preview_bot_identity
    AND r.state = 'confirmed'
    AND r.manifest_sha256 = m.manifest_sha256
    AND r.source_revision_vector_sha256 = m.source_revision_vector_sha256
  ORDER BY i.ordinal
$function$;
"""


def _constraint(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; END IF; END $$"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE context_manifest_omissions ADD COLUMN IF NOT EXISTS ordinal integer")
    op.execute(
        "WITH ordered AS (SELECT id, row_number() OVER "
        "(PARTITION BY manifest_id ORDER BY id) AS ordinal "
        "FROM context_manifest_omissions) "
        "UPDATE context_manifest_omissions AS omission SET ordinal = ordered.ordinal "
        "FROM ordered WHERE omission.id = ordered.id AND omission.ordinal IS NULL"
    )
    op.execute("ALTER TABLE context_manifest_omissions ALTER COLUMN ordinal SET NOT NULL")
    _constraint(
        "ck_context_manifest_omissions_ordinal_positive",
        "ALTER TABLE context_manifest_omissions ADD CONSTRAINT "
        "ck_context_manifest_omissions_ordinal_positive CHECK (ordinal > 0)",
    )
    _constraint(
        "uq_context_manifest_omissions_ordinal",
        "ALTER TABLE context_manifest_omissions ADD CONSTRAINT "
        "uq_context_manifest_omissions_ordinal UNIQUE (manifest_id, ordinal)",
    )
    op.execute(_FUNCTION_HEADER + _SCOPED_FUNCTION_BODY)


def downgrade() -> None:
    op.execute(_FUNCTION_HEADER + _LEGACY_FUNCTION_BODY)
    op.execute(
        "ALTER TABLE context_manifest_omissions DROP CONSTRAINT IF EXISTS "
        "uq_context_manifest_omissions_ordinal"
    )
    op.execute(
        "ALTER TABLE context_manifest_omissions DROP CONSTRAINT IF EXISTS "
        "ck_context_manifest_omissions_ordinal_positive"
    )
    op.execute("ALTER TABLE context_manifest_omissions DROP COLUMN IF EXISTS ordinal")
