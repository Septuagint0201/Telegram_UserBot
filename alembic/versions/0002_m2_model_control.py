"""M2 model configuration and key-only control plane.

Revision ID: 0002_m2_model_control
Revises: 0001_m1_durable_state
"""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M2_TABLES, metadata

revision: str = "0002_m2_model_control"
down_revision: str | Sequence[str] | None = "0001_m1_durable_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    m2_tables = [metadata.tables[name] for name in M2_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=m2_tables, checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION reject_immutable_model_row_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'immutable model version cannot be changed';
        END;
        $$;

        CREATE TRIGGER trg_model_endpoints_immutable
        BEFORE UPDATE OR DELETE ON model_endpoints
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_model_row_change();

        CREATE TRIGGER trg_model_capability_snapshots_immutable
        BEFORE UPDATE OR DELETE ON model_capability_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_model_row_change();

        CREATE TRIGGER trg_model_config_versions_immutable
        BEFORE UPDATE OR DELETE ON model_config_versions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_model_row_change();

        CREATE TRIGGER trg_prompt_versions_immutable
        BEFORE UPDATE OR DELETE ON prompt_versions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_model_row_change();

        CREATE FUNCTION enforce_credential_version_destruction()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'credential version must be cryptographically destroyed';
          END IF;
          IF OLD.destroyed_at IS NOT NULL
             OR NEW.id <> OLD.id
             OR NEW.credential_id <> OLD.credential_id
             OR NEW.profile_id <> OLD.profile_id
             OR NEW.version_no <> OLD.version_no
             OR NEW.algorithm <> OLD.algorithm
             OR NEW.key_version <> OLD.key_version
             OR NEW.aad_schema_version <> OLD.aad_schema_version
             OR NEW.created_at <> OLD.created_at
             OR NEW.destroyed_at IS NULL
             OR NEW.destroy_reason IS NULL
             OR NEW.nonce IS NOT NULL
             OR NEW.ciphertext IS NOT NULL
             OR NEW.secret_fingerprint IS NOT NULL THEN
            RAISE EXCEPTION 'credential version update must be one-way destruction';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_model_credential_versions_one_way_destroy
        BEFORE UPDATE OR DELETE ON model_credential_versions
        FOR EACH ROW EXECUTE FUNCTION enforce_credential_version_destruction();
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS enforce_credential_version_destruction() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_immutable_model_row_change() CASCADE")
    m2_tables = [metadata.tables[name] for name in M2_TABLES]
    metadata.drop_all(bind=op.get_bind(), tables=m2_tables, checkfirst=False)
