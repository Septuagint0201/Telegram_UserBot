"""Close runtime fencing and metadata drift identified in the M1-M7 review."""

# ruff: noqa: S608 - migration identifiers and expressions are fixed constants

from collections.abc import Sequence

from alembic import op

revision: str = "0024_runtime_fencing_provenance"
down_revision: str | Sequence[str] | None = "0023_m7_proactive_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(table: str, name: str, expression: str) -> None:
    op.execute(
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = '{table}'::regclass "
        f"AND conname = '{name}') THEN ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({expression}); END IF; END $$;"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS memory_input_manifest_id uuid")
    op.execute(
        """
        UPDATE outbound_delivery_groups
        SET mode_version = COALESCE(mode_version, 1),
            content_revision = COALESCE(content_revision, 0);
        ALTER TABLE outbound_delivery_groups
          ALTER COLUMN mode_version SET DEFAULT 1,
          ALTER COLUMN mode_version SET NOT NULL,
          ALTER COLUMN content_revision SET DEFAULT 0,
          ALTER COLUMN content_revision SET NOT NULL;

        UPDATE outbound_intents
        SET mode_version = COALESCE(mode_version, 1),
            content_revision = COALESCE(content_revision, 0),
            idempotency_key = COALESCE(
              idempotency_key,
              decode(md5(id::text) || md5(id::text || ':' || 'intent-v2'), 'hex')
            );
        ALTER TABLE outbound_intents
          ALTER COLUMN mode_version SET DEFAULT 1,
          ALTER COLUMN mode_version SET NOT NULL,
          ALTER COLUMN content_revision SET DEFAULT 0,
          ALTER COLUMN content_revision SET NOT NULL,
          ALTER COLUMN idempotency_key SET NOT NULL;

        ALTER TABLE outbound_intents
          ADD COLUMN IF NOT EXISTS send_fencing_token bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS send_lease_expires_at timestamptz;
        -- A process that crashed while sending is conservatively unknown before
        -- the new lease constraint is installed.
        UPDATE outbound_intents
        SET state = 'unknown',
            unknown_since = COALESCE(unknown_since, updated_at),
            last_error_code = COALESCE(last_error_code, 'crash_after_dispatch'),
            send_lease_expires_at = NULL
        WHERE state = 'sending';
        """
    )
    _constraint(
        "outbound_intents",
        "ck_outbound_intents_send_fencing_token_nonnegative",
        "send_fencing_token >= 0",
    )
    _constraint(
        "outbound_intents",
        "ck_outbound_intents_send_lease_matches_state",
        "(state = 'sending' AND send_lease_expires_at IS NOT NULL) OR "
        "(state <> 'sending' AND send_lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS "
        "ck_outbound_intents_send_lease_matches_state"
    )
    op.execute(
        "ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS "
        "ck_outbound_intents_send_fencing_token_nonnegative"
    )
    op.execute(
        "ALTER TABLE outbound_intents DROP COLUMN IF EXISTS send_lease_expires_at, "
        "DROP COLUMN IF EXISTS send_fencing_token"
    )
    op.execute(
        "ALTER TABLE outbound_intents ALTER COLUMN idempotency_key DROP NOT NULL, "
        "ALTER COLUMN mode_version DROP NOT NULL, ALTER COLUMN content_revision DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE outbound_delivery_groups ALTER COLUMN mode_version DROP NOT NULL, "
        "ALTER COLUMN content_revision DROP NOT NULL"
    )
