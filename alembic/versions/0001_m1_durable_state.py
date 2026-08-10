"""M1 PostgreSQL durable-state baseline.

Revision ID: 0001_m1_durable_state
Revises: None
"""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import metadata

revision: str = "0001_m1_durable_state"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata.create_all(bind=op.get_bind(), checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION enforce_message_revision_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.redacted_at IS NOT NULL THEN
            RAISE EXCEPTION 'redacted message revision is immutable';
          END IF;
          IF NEW.redacted_at IS NULL THEN
            RAISE EXCEPTION 'message revision update must be a one-way redaction';
          END IF;
          IF NEW.id <> OLD.id
             OR NEW.account_id <> OLD.account_id
             OR NEW.message_id <> OLD.message_id
             OR NEW.revision_no <> OLD.revision_no
             OR NEW.body_kind <> OLD.body_kind
             OR NEW.source_event_id <> OLD.source_event_id
             OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'message revision identity is immutable';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_message_revisions_one_way_redaction
        BEFORE UPDATE ON message_revisions
        FOR EACH ROW EXECUTE FUNCTION enforce_message_revision_immutability();
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS enforce_message_revision_immutability() CASCADE")
    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    # The vector extension is shared deployment state and is intentionally retained.
