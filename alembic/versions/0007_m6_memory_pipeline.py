"""M6 memory, summary, review, and embedding pipeline state."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column

from telegram_userbot.adapters.persistence.schema import M6_TABLES, metadata

revision: str = "0007_m6_memory_pipeline"
down_revision: str | Sequence[str] | None = "0006_m5_media_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_model_config_versions_id_profile",
        "model_config_versions",
        ["id", "profile_id"],
    )
    op.add_column(
        "model_runs",
        Column("memory_input_manifest_id", metadata.tables["memory_input_manifests"].c.id.type),
    )
    tables = [metadata.tables[name] for name in M6_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    op.create_foreign_key(
        "fk_model_runs_memory_input_manifest",
        "model_runs",
        "memory_input_manifests",
        ["memory_input_manifest_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_context_manifest_items_memory_version",
        "context_manifest_items",
        "memory_versions",
        ["memory_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_context_manifest_items_summary_version",
        "context_manifest_items",
        "summary_versions",
        ["summary_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_context_manifest_items_summary_version", "context_manifest_items", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_context_manifest_items_memory_version", "context_manifest_items", type_="foreignkey"
    )
    op.drop_constraint("fk_model_runs_memory_input_manifest", "model_runs", type_="foreignkey")
    op.drop_constraint(
        "fk_memory_manifest_items_summary_version",
        "memory_input_manifest_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_memory_manifest_items_memory_version",
        "memory_input_manifest_items",
        type_="foreignkey",
    )
    op.drop_constraint("fk_summaries_current_version", "summaries", type_="foreignkey")
    op.drop_constraint("fk_memories_current_version", "memories", type_="foreignkey")
    op.drop_constraint("fk_memory_jobs_input_manifest", "memory_jobs", type_="foreignkey")
    for name in reversed(M6_TABLES):
        op.drop_table(name)
    op.drop_column("model_runs", "memory_input_manifest_id")
    op.drop_constraint(
        "uq_model_config_versions_id_profile",
        "model_config_versions",
        type_="unique",
    )
