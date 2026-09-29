"""Allow content-free events from peers without a conversation scope."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0029_unsupported_peer_events"
down_revision: str | None = "0028_m8_background_model_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_schema_revision(projection: str) -> None:
    op.drop_constraint(
        op.f("ck_service_instances_schema_revision_current"), "service_instances", type_="check"
    )
    op.get_bind().execute(
        text("UPDATE service_instances SET schema_revision = :projection"),
        {"projection": projection},
    )
    op.create_check_constraint(
        "schema_revision_current", "service_instances", f"schema_revision = '{projection}'"
    )
    op.drop_constraint(
        op.f("ck_service_status_events_schema_revision_current"),
        "service_status_events",
        type_="check",
    )
    # Status events are historical facts, including after a partial downgrade.
    # Do not rewrite or delete their original schema revision.
    op.create_check_constraint(
        "schema_revision_current",
        "service_status_events",
        "schema_revision IN ('0025_m8_service_status','0026_m8_data_export',"
        "'0027_m8_model_run_claim','0028_m8_background_model_runtime',"
        "'0029_unsupported_peer_events','0030_scope_derived_erasure','0031_scope_export_budget',"
        "'0032_scope_metadata_erasure','0033_media_upload_erasure','0034_scope_erasure_completion','0035_memory_period_summaries','0036_worker_complete')",
    )


def upgrade() -> None:
    op.alter_column(
        "message_events",
        "conversation_id",
        nullable=True,
    )
    _replace_schema_revision(revision)


def downgrade() -> None:
    # Hold the same lock as ALTER COLUMN before checking so an unsupported event
    # cannot arrive between the compatibility check and restoring NOT NULL.
    op.execute("LOCK TABLE message_events IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        text("SELECT EXISTS (SELECT 1 FROM message_events WHERE conversation_id IS NULL)")
    ):
        raise RuntimeError("MIGRATION_0029_DOWNGRADE_REQUIRES_SCOPED_EVENTS")
    op.alter_column(
        "message_events",
        "conversation_id",
        nullable=False,
    )
    _replace_schema_revision("0028_m8_background_model_runtime")
