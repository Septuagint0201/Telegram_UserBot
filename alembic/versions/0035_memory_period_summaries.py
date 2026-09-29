"""Allow consolidation to publish calendar summaries with the summary output schema."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, text

from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0035_memory_period_summaries"
down_revision: str | None = "0034_scope_erasure_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(*, periods: bool) -> None:
    op.drop_constraint(
        op.f("ck_memory_input_manifests_kind_purpose_match"),
        "memory_input_manifests",
        type_="check",
    )
    consolidation = "output_schema_version IN (2, 3)" if periods else "output_schema_version = 3"
    op.create_check_constraint(
        "kind_purpose_match",
        "memory_input_manifests",
        (
            "(manifest_kind = 'episode' AND purpose = 'memory_episode' AND "
            "output_schema_version = 1) OR "
            "(manifest_kind = 'rolling_summary' AND purpose = 'memory_rolling_summary' AND "
            "output_schema_version = 2) OR "
            "(manifest_kind = 'consolidation' AND purpose = 'memory_consolidation' AND "
            + consolidation
            + ") OR "
            "(manifest_kind = 'reconciliation' AND purpose = 'memory_reconciliation' AND "
            "output_schema_version = 1)"
        ),
    )


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


def upgrade() -> None:
    _constraint(periods=True)
    _revision(revision)
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='telegram_userbot_worker_runtime') THEN
          GRANT SELECT (default_timezone) ON accounts TO telegram_userbot_worker_runtime;
          GRANT SELECT (timezone) ON contacts TO telegram_userbot_worker_runtime;
          GRANT SELECT (observed_at) ON message_events TO telegram_userbot_worker_runtime;
          GRANT SELECT (deleted_at, metadata_erased_at) ON conversations
            TO telegram_userbot_worker_runtime;
        END IF;
      END $$;
    """)


def downgrade() -> None:
    if op.get_bind().scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM memory_jobs WHERE job_kind='consolidation' "
            "AND output_schema_version=2)"
        )
    ):
        raise RuntimeError("calendar summary jobs prevent downgrade")
    _constraint(periods=False)
    _revision("0034_scope_erasure_completion")
