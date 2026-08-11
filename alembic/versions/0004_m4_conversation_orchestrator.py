# ruff: noqa: E501 - SQL migration statements remain grep-friendly and executable verbatim.
"""M4 conversation orchestrator and Main AI lifecycle.

Revision ID: 0004_m4_conversation_orchestrator
Revises: 0003_m3_telegram_lifecycle
"""

from collections.abc import Sequence

from alembic import op

from telegram_userbot.adapters.persistence.schema import M4_TABLES, metadata

revision: str = "0004_m4_conversation_orchestrator"
down_revision: str | Sequence[str] | None = "0003_m3_telegram_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _upgrade_existing_tables() -> None:
    op.execute(
        """
        ALTER TABLE conversations DROP CONSTRAINT IF EXISTS
          uq_conversations_id_account_contact;
        ALTER TABLE conversations ADD CONSTRAINT uq_conversations_id_account_contact
          UNIQUE (id, account_id, contact_id);

        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS debounce_seconds integer
          NOT NULL DEFAULT 3;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS hard_cap_seconds integer
          NOT NULL DEFAULT 10;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS collect_started_at timestamptz;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS hard_deadline_at timestamptz;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS sealed_at timestamptz;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS lease_owner uuid;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS fencing_token bigint
          NOT NULL DEFAULT 0;
        ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS terminal_reason text;

        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS ck_conversation_turns_state_values;
        ALTER TABLE conversation_turns ADD CONSTRAINT ck_conversation_turns_state_values CHECK (
          state IN ('collecting','ready','generating','output_ready','superseded',
                    'completed','cancelled','failed'));
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_trigger_kind_values;
        ALTER TABLE conversation_turns ADD CONSTRAINT ck_conversation_turns_trigger_kind_values CHECK (
          trigger_kind IN ('incoming','replacement','manual_pending_reply','copilot','proactive'));
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_debounce_seconds_positive;
        ALTER TABLE conversation_turns ADD CONSTRAINT
          ck_conversation_turns_debounce_seconds_positive CHECK (debounce_seconds > 0);
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_hard_cap_not_before_debounce;
        ALTER TABLE conversation_turns ADD CONSTRAINT
          ck_conversation_turns_hard_cap_not_before_debounce CHECK (
            hard_cap_seconds >= debounce_seconds);
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_fencing_token_nonnegative;
        ALTER TABLE conversation_turns ADD CONSTRAINT
          ck_conversation_turns_fencing_token_nonnegative CHECK (fencing_token >= 0);
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_lease_fields_match;
        ALTER TABLE conversation_turns ADD CONSTRAINT ck_conversation_turns_lease_fields_match CHECK (
          (lease_owner IS NULL AND lease_expires_at IS NULL) OR
          (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL));
        DROP INDEX IF EXISTS uq_conversation_turns_collecting;
        CREATE UNIQUE INDEX uq_conversation_turns_collecting
          ON conversation_turns(conversation_id) WHERE state = 'collecting';
        DROP INDEX IF EXISTS uq_conversation_turns_processing;
        CREATE UNIQUE INDEX uq_conversation_turns_processing
          ON conversation_turns(conversation_id)
          WHERE state IN ('ready','generating','output_ready');

        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS turn_id uuid;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS model_role text;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS copilot_draft_id uuid;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS approved_draft_revision_id uuid;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS generation_no integer
          NOT NULL DEFAULT 1;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS account_control_version bigint
          NOT NULL DEFAULT 1;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS logical_content_sha256 bytea;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS normalizer_version text
          NOT NULL DEFAULT 'normalized-text-v1';
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS splitter_version text
          NOT NULL DEFAULT 'telegram-text-v1';
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS max_delivery_chunks integer
          NOT NULL DEFAULT 16;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS send_authorized_at timestamptz;
        ALTER TABLE outbound_delivery_groups ADD COLUMN IF NOT EXISTS first_side_effect_at timestamptz;

        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS turn_id uuid;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS model_role text;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS source text;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS generation_no integer
          NOT NULL DEFAULT 1;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS account_control_version bigint
          NOT NULL DEFAULT 1;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS mode_version bigint;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS content_revision bigint;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS idempotency_key bytea;
        ALTER TABLE outbound_intents ADD COLUMN IF NOT EXISTS chunk_count integer
          NOT NULL DEFAULT 1;

        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          ck_outbound_delivery_groups_generation_no_positive;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT
          ck_outbound_delivery_groups_generation_no_positive CHECK (generation_no > 0);
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          ck_outbound_delivery_groups_control_version_positive;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT
          ck_outbound_delivery_groups_control_version_positive CHECK (
            account_control_version > 0);
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          ck_outbound_delivery_groups_max_delivery_chunks_positive;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT
          ck_outbound_delivery_groups_max_delivery_chunks_positive CHECK (
            max_delivery_chunks > 0);
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          ck_outbound_delivery_groups_logical_content_hash_32_bytes;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT
          ck_outbound_delivery_groups_logical_content_hash_32_bytes CHECK (
            logical_content_sha256 IS NULL OR octet_length(logical_content_sha256) = 32);

        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS
          ck_outbound_intents_generation_no_positive;
        ALTER TABLE outbound_intents ADD CONSTRAINT ck_outbound_intents_generation_no_positive
          CHECK (generation_no > 0);
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS
          ck_outbound_intents_control_version_positive;
        ALTER TABLE outbound_intents ADD CONSTRAINT ck_outbound_intents_control_version_positive
          CHECK (account_control_version > 0);
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS
          ck_outbound_intents_chunk_position_valid;
        ALTER TABLE outbound_intents ADD CONSTRAINT ck_outbound_intents_chunk_position_valid
          CHECK (chunk_count > 0 AND sequence_no < chunk_count);
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS
          ck_outbound_intents_idempotency_32_bytes;
        ALTER TABLE outbound_intents ADD CONSTRAINT ck_outbound_intents_idempotency_32_bytes
          CHECK (idempotency_key IS NULL OR octet_length(idempotency_key) = 32);
        """
    )


def _add_wide_constraints() -> None:
    op.execute(
        """
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT fk_outbound_groups_turn_scope
          FOREIGN KEY (turn_id, account_id, conversation_id)
          REFERENCES conversation_turns(id, account_id, conversation_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT fk_outbound_groups_model_run_scope
          FOREIGN KEY (model_run_id, account_id, conversation_id, model_role)
          REFERENCES model_runs(id, account_id, conversation_id, logical_role)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT fk_outbound_groups_copilot_draft_scope
          FOREIGN KEY (copilot_draft_id, account_id, conversation_id)
          REFERENCES copilot_drafts(id, account_id, conversation_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT
          fk_outbound_groups_approved_draft_revision
          FOREIGN KEY (approved_draft_revision_id, account_id, copilot_draft_id)
          REFERENCES copilot_draft_revisions(id, account_id, draft_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_delivery_groups ADD CONSTRAINT uq_outbound_groups_m4_scope
          UNIQUE (id, account_id, conversation_id, model_run_id, model_role, source, generation_no);

        ALTER TABLE outbound_intents ADD CONSTRAINT fk_outbound_intents_turn_scope
          FOREIGN KEY (turn_id, account_id, conversation_id)
          REFERENCES conversation_turns(id, account_id, conversation_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_intents ADD CONSTRAINT fk_outbound_intents_model_run_scope
          FOREIGN KEY (model_run_id, account_id, conversation_id, model_role)
          REFERENCES model_runs(id, account_id, conversation_id, logical_role)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE outbound_intents ADD CONSTRAINT fk_outbound_intents_group_m4_scope
          FOREIGN KEY (delivery_group_id, account_id, conversation_id, model_run_id,
                       model_role, source, generation_no)
          REFERENCES outbound_delivery_groups(id, account_id, conversation_id, model_run_id,
                                                model_role, source, generation_no)
          DEFERRABLE INITIALLY DEFERRED;
        """
    )


def upgrade() -> None:
    _upgrade_existing_tables()
    m4_tables = [metadata.tables[name] for name in M4_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=m4_tables, checkfirst=False)
    _add_wide_constraints()
    op.execute(
        """
        CREATE FUNCTION enforce_copilot_revision_redaction()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'copilot draft revision history cannot be deleted';
          END IF;
          IF OLD.redacted_at IS NOT NULL
             OR NEW.id <> OLD.id
             OR NEW.account_id <> OLD.account_id
             OR NEW.conversation_id <> OLD.conversation_id
             OR NEW.draft_id <> OLD.draft_id
             OR NEW.revision_no <> OLD.revision_no
             OR NEW.author_type <> OLD.author_type
             OR NEW.created_at <> OLD.created_at
             OR NEW.redacted_at IS NULL
             OR NEW.content_text IS NOT NULL
             OR NEW.content_sha256 IS NOT NULL THEN
            RAISE EXCEPTION 'copilot revision update must be one-way redaction';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_copilot_draft_revisions_one_way_redaction
        BEFORE UPDATE OR DELETE ON copilot_draft_revisions
        FOR EACH ROW EXECUTE FUNCTION enforce_copilot_revision_redaction();
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS enforce_copilot_revision_redaction() CASCADE")
    op.execute(
        """
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS fk_outbound_intents_group_m4_scope;
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS fk_outbound_intents_model_run_scope;
        ALTER TABLE outbound_intents DROP CONSTRAINT IF EXISTS fk_outbound_intents_turn_scope;
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS uq_outbound_groups_m4_scope;
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          fk_outbound_groups_approved_draft_revision;
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          fk_outbound_groups_copilot_draft_scope;
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS
          fk_outbound_groups_model_run_scope;
        ALTER TABLE outbound_delivery_groups DROP CONSTRAINT IF EXISTS fk_outbound_groups_turn_scope;
        """
    )
    m4_tables = [metadata.tables[name] for name in M4_TABLES]
    metadata.drop_all(bind=op.get_bind(), tables=m4_tables, checkfirst=False)
    op.execute(
        """
        ALTER TABLE conversations DROP CONSTRAINT IF EXISTS
          uq_conversations_id_account_contact;

        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS chunk_count;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS idempotency_key;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS content_revision;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS mode_version;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS account_control_version;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS generation_no;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS source;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS model_role;
        ALTER TABLE outbound_intents DROP COLUMN IF EXISTS turn_id;

        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS first_side_effect_at;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS send_authorized_at;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS max_delivery_chunks;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS splitter_version;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS normalizer_version;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS logical_content_sha256;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS account_control_version;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS generation_no;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS approved_draft_revision_id;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS copilot_draft_id;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS model_role;
        ALTER TABLE outbound_delivery_groups DROP COLUMN IF EXISTS turn_id;

        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS ck_conversation_turns_state_values;
        ALTER TABLE conversation_turns ADD CONSTRAINT ck_conversation_turns_state_values CHECK (
          state IN ('collecting','sealed','generating','superseded','completed','cancelled','failed'));
        ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS
          ck_conversation_turns_trigger_kind_values;
        ALTER TABLE conversation_turns ADD CONSTRAINT ck_conversation_turns_trigger_kind_values CHECK (
          trigger_kind IN ('incoming','replacement','copilot','proactive'));
        DROP INDEX IF EXISTS uq_conversation_turns_processing;
        DROP INDEX IF EXISTS uq_conversation_turns_collecting;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS terminal_reason;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS fencing_token;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS lease_expires_at;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS lease_owner;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS sealed_at;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS hard_deadline_at;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS collect_started_at;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS hard_cap_seconds;
        ALTER TABLE conversation_turns DROP COLUMN IF EXISTS debounce_seconds;
        """
    )
