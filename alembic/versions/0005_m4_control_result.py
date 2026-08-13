"""Harden queued Control Bot command identity and result semantics.

Revision ID: 0005_m4_control_result
Revises: 0004_m4_orchestrator
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_m4_control_result"
down_revision: str | Sequence[str] | None = "0004_m4_orchestrator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE control_commands ADD COLUMN IF NOT EXISTS bot_chat_id bigint;
        UPDATE control_commands
        SET bot_chat_id = admin_telegram_user_id
        WHERE bot_chat_id IS NULL;
        ALTER TABLE control_commands ALTER COLUMN bot_chat_id SET NOT NULL;
        ALTER TABLE control_commands ADD COLUMN IF NOT EXISTS result_changed boolean;
        ALTER TABLE control_commands ADD COLUMN IF NOT EXISTS result_control_version bigint;
        ALTER TABLE control_commands ADD COLUMN IF NOT EXISTS result_mode_version bigint;
        ALTER TABLE control_commands ADD COLUMN IF NOT EXISTS result_payload jsonb;
        UPDATE control_commands
        SET result_code = COALESCE(
              result_code,
              CASE state
                WHEN 'applied' THEN 'LEGACY_APPLIED'
                ELSE 'LEGACY_REJECTED'
              END
            ),
            completed_at = COALESCE(completed_at, created_at)
        WHERE state IN ('applied', 'rejected');
        UPDATE control_commands
        SET result_changed = CASE
          WHEN state = 'applied' THEN result_code NOT IN ('NO_CHANGE', 'NO_TEMPORARY_TAKEOVER')
          ELSE false
        END
        WHERE state IN ('applied', 'rejected') AND result_changed IS NULL;
        UPDATE control_commands
        SET result_code = NULL,
            result_changed = NULL,
            result_control_version = NULL,
            result_mode_version = NULL,
            result_payload = NULL,
            completed_at = NULL
        WHERE state = 'pending';
        ALTER TABLE control_commands DROP CONSTRAINT IF EXISTS
          ck_control_commands_terminal_result_complete;
        ALTER TABLE control_commands ADD CONSTRAINT
          ck_control_commands_terminal_result_complete CHECK (
            (state = 'pending' AND result_code IS NULL AND result_changed IS NULL
             AND result_control_version IS NULL AND result_mode_version IS NULL
             AND result_payload IS NULL
             AND completed_at IS NULL) OR
            (state IN ('applied','rejected') AND result_code IS NOT NULL
             AND result_changed IS NOT NULL AND completed_at IS NOT NULL)
          );
        ALTER TABLE control_commands DROP CONSTRAINT IF EXISTS
          ck_control_commands_result_payload_object;
        ALTER TABLE control_commands ADD CONSTRAINT
          ck_control_commands_result_payload_object CHECK (
            result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'
          );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE control_commands DROP CONSTRAINT IF EXISTS
          ck_control_commands_terminal_result_complete;
        ALTER TABLE control_commands DROP CONSTRAINT IF EXISTS
          ck_control_commands_result_payload_object;
        ALTER TABLE control_commands DROP COLUMN IF EXISTS result_payload;
        ALTER TABLE control_commands DROP COLUMN IF EXISTS result_mode_version;
        ALTER TABLE control_commands DROP COLUMN IF EXISTS result_control_version;
        ALTER TABLE control_commands DROP COLUMN IF EXISTS result_changed;
        ALTER TABLE control_commands DROP COLUMN IF EXISTS bot_chat_id;
        """
    )
