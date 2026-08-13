"""M5 media capability and context manifest/control lifecycle.

Revision ID: 0006_m5_media_context
Revises: 0005_m4_control_result
"""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M5_TABLES, metadata

revision: str = "0006_m5_media_context"
down_revision: str | Sequence[str] | None = "0005_m4_control_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_capability_snapshots
          ADD COLUMN IF NOT EXISTS max_images_per_request integer NOT NULL DEFAULT 10,
          ADD COLUMN IF NOT EXISTS max_image_bytes_per_request bigint NOT NULL DEFAULT 20971520,
          ADD COLUMN IF NOT EXISTS auto_image_tokens integer NOT NULL DEFAULT 2048,
          ADD COLUMN IF NOT EXISTS messages_auto_detail_equivalent boolean NOT NULL DEFAULT false;
        ALTER TABLE model_capability_snapshots DROP CONSTRAINT IF EXISTS
          ck_model_capability_snapshots_image_limits_positive;
        ALTER TABLE model_capability_snapshots ADD CONSTRAINT
          ck_model_capability_snapshots_image_limits_positive CHECK (
            max_images_per_request > 0 AND max_image_bytes_per_request > 0
            AND auto_image_tokens > 0);
        """
    )
    tables = [metadata.tables[name] for name in M5_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    op.create_foreign_key(
        "fk_model_runs_context_manifest_scope",
        "model_runs",
        "context_manifests",
        ["context_manifest_id", "account_id"],
        ["id", "account_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION public.context_preview_sources(
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
        REVOKE ALL ON FUNCTION public.context_preview_sources(uuid,bigint,bigint,text)
          FROM PUBLIC;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.context_preview_sources(uuid,bigint,bigint,text)")
    op.drop_constraint("fk_model_runs_context_manifest_scope", "model_runs", type_="foreignkey")
    tables = [metadata.tables[name] for name in M5_TABLES]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    op.execute(
        """
        ALTER TABLE model_capability_snapshots
          DROP CONSTRAINT IF EXISTS ck_model_capability_snapshots_image_limits_positive,
          DROP COLUMN IF EXISTS messages_auto_detail_equivalent,
          DROP COLUMN IF EXISTS auto_image_tokens,
          DROP COLUMN IF EXISTS max_image_bytes_per_request,
          DROP COLUMN IF EXISTS max_images_per_request;
        """
    )
