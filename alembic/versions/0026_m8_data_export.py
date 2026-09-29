"""Add the durable M8 data-export queue and advance runtime status fencing."""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M8_EXPORT_TABLES, metadata

revision: str = "0026_m8_data_export"
down_revision: str | Sequence[str] | None = "0025_m8_service_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXPORT_VIEWS: tuple[tuple[str, str], ...] = (
    (
        "export_account_peers_v1",
        "SELECT id, account_id, peer_id, username, display_name, observed_is_contact, "
        "last_observed_at FROM account_peers",
    ),
    (
        "export_message_revisions_v1",
        "SELECT id, account_id, message_id, revision_no, body_kind, text_content, caption, "
        "entities_schema_version, entities, source_event_id, telegram_edited_at, created_at, "
        "redacted_at FROM message_revisions WHERE redacted_at IS NULL",
    ),
    (
        "export_message_media_v1",
        "SELECT mm.id, mm.account_id, mm.message_revision_id, mm.media_object_id, "
        "mm.media_kind, mm.position, mm.declared_mime, mm.declared_size, mm.duration_ms, "
        "mm.original_name_sanitized, mm.created_at FROM message_media AS mm "
        "JOIN message_revisions AS mr ON mr.id = mm.message_revision_id "
        "AND mr.account_id = mm.account_id WHERE mr.redacted_at IS NULL",
    ),
    (
        "export_memories_v1",
        "SELECT id, account_id, contact_id, conversation_id, memory_type, status, "
        "current_version_no, superseded_by_memory_id, created_at, updated_at, forgotten_at "
        "FROM memories WHERE status <> 'forgotten' AND forgotten_at IS NULL",
    ),
    (
        "export_memory_versions_v1",
        "SELECT mv.id, mv.account_id, mv.memory_id, mv.version_no, mv.operation, "
        "mv.payload_schema_version, mv.payload, mv.rendered_text, mv.importance, "
        "mv.confidence, mv.observed_at, mv.valid_from, mv.valid_to, mv.time_precision, "
        "mv.timezone, mv.model_role, mv.prompt_version, mv.validator_policy_version, "
        "mv.acceptance_kind, mv.created_at, mv.redacted_at FROM memory_versions AS mv "
        "JOIN memories AS m ON m.id = mv.memory_id AND m.account_id = mv.account_id "
        "WHERE mv.redacted_at IS NULL AND m.status <> 'forgotten' AND m.forgotten_at IS NULL",
    ),
    (
        "export_summary_versions_v1",
        "SELECT id, account_id, summary_id, version_no, range_start_event_id, "
        "range_end_event_id, period_start_at, period_end_at, timezone_snapshot, "
        "content_text, model_role, prompt_version, pipeline_version, output_schema_version, "
        "invalidation_state, created_at, redacted_at FROM summary_versions "
        "WHERE redacted_at IS NULL",
    ),
)


def _replace_schema_revision(*, projection_revision: str) -> None:
    op.execute(
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS "
        "ck_service_instances_schema_revision_current"
    )
    # Migration is an explicit maintenance operation.  Only the mutable current
    # projection advances; append-only status events retain their true producer
    # revision and are never rewritten.
    op.execute(
        "UPDATE service_instances "  # noqa: S608 - projection_revision is migration constant
        f"SET schema_revision = '{projection_revision}' "
        "WHERE schema_revision <> "
        f"'{projection_revision}'"
    )
    op.create_check_constraint(
        "schema_revision_current",
        "service_instances",
        f"schema_revision = '{projection_revision}'",
    )

    op.execute(
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS "
        "ck_service_status_events_schema_revision_current"
    )
    op.create_check_constraint(
        "schema_revision_current",
        "service_status_events",
        "schema_revision IN ('0025_m8_service_status','0026_m8_data_export')",
    )


def upgrade() -> None:
    tables = [metadata.tables[name] for name in M8_EXPORT_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    op.execute("ALTER TABLE data_export_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_export_requests FORCE ROW LEVEL SECURITY")
    for view_name, query in _EXPORT_VIEWS:
        op.execute(f"CREATE VIEW {view_name} WITH (security_barrier=true) AS {query}")
    _replace_schema_revision(projection_revision=revision)


def downgrade() -> None:
    _replace_schema_revision(projection_revision="0025_m8_service_status")
    for view_name, _query in reversed(_EXPORT_VIEWS):
        op.execute(f"DROP VIEW {view_name}")
    tables = [metadata.tables[name] for name in reversed(M8_EXPORT_TABLES)]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
