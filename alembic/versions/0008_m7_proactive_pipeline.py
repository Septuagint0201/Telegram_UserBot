"""M7 proactive candidate, scheduling, budget, and decision state."""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M7_TABLES, metadata

revision: str = "0008_m7_proactive_pipeline"
down_revision: str | Sequence[str] | None = "0007_m6_memory_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = [metadata.tables[name] for name in M7_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)


def downgrade() -> None:
    tables = [metadata.tables[name] for name in reversed(M7_TABLES)]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
