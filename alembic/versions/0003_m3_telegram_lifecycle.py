"""M3 Telegram event and outbound lifecycle.

Revision ID: 0003_m3_telegram_lifecycle
Revises: 0002_m2_model_control
"""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M3_TABLES, metadata

revision: str = "0003_m3_telegram_lifecycle"
down_revision: str | Sequence[str] | None = "0002_m2_model_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    m3_tables = [metadata.tables[name] for name in M3_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=m3_tables, checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION enforce_outbound_attempt_completion()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'outbound attempt history cannot be deleted';
          END IF;
          IF OLD.state <> 'started'
             OR NEW.id <> OLD.id
             OR NEW.intent_id <> OLD.intent_id
             OR NEW.account_id <> OLD.account_id
             OR NEW.attempt_no <> OLD.attempt_no
             OR NEW.started_at <> OLD.started_at
             OR NEW.state = 'started'
             OR NEW.finished_at IS NULL THEN
            RAISE EXCEPTION 'outbound attempt update must be one-way completion';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_outbound_attempts_one_way_completion
        BEFORE UPDATE OR DELETE ON outbound_attempts
        FOR EACH ROW EXECUTE FUNCTION enforce_outbound_attempt_completion();
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS enforce_outbound_attempt_completion() CASCADE")
    m3_tables = [metadata.tables[name] for name in M3_TABLES]
    metadata.drop_all(bind=op.get_bind(), tables=m3_tables, checkfirst=False)
