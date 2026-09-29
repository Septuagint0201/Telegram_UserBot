"""Bind generation runs to immutable memory and proactive owners.

Revision ID: 0028_m8_background_model_runtime
Revises: 0027_m8_model_run_claim
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from telegram_userbot.adapters.persistence.schema import M8_MODEL_TABLES, metadata

revision: str = "0028_m8_background_model_runtime"
down_revision: str | Sequence[str] | None = "0027_m8_model_run_claim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_schema_revision(*, projection: str, event_revisions: Sequence[str]) -> None:
    connection = op.get_bind()
    op.execute(
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS "
        "ck_service_instances_schema_revision_current"
    )
    connection.execute(
        text(
            "UPDATE service_instances SET schema_revision = :projection "
            "WHERE schema_revision <> :projection"
        ),
        {"projection": projection},
    )
    op.create_check_constraint(
        "schema_revision_current",
        "service_instances",
        f"schema_revision = '{projection}'",
    )
    op.execute(
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS "
        "ck_service_status_events_schema_revision_current"
    )
    allowed = ",".join(f"'{item}'" for item in event_revisions)
    op.create_check_constraint(
        "schema_revision_current",
        "service_status_events",
        f"schema_revision IN ({allowed})",
    )


def _upgrade_existing_columns() -> None:
    op.execute(
        """
        ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS memory_job_id uuid;
        ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS proactive_job_id uuid;
        ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS proactive_input_manifest_id uuid;

        ALTER TABLE memory_input_manifests
          ADD COLUMN IF NOT EXISTS prompt_bundle_sha256 bytea;
        ALTER TABLE memory_input_manifests
          ADD COLUMN IF NOT EXISTS capability_snapshot_sha256 bytea;
        ALTER TABLE memory_input_manifests
          ADD COLUMN IF NOT EXISTS logical_role text DEFAULT 'memory_agent';
        ALTER TABLE memory_input_manifests ADD COLUMN IF NOT EXISTS purpose text;
        UPDATE memory_input_manifests SET purpose = CASE manifest_kind
          WHEN 'episode' THEN 'memory_episode'
          WHEN 'rolling_summary' THEN 'memory_rolling_summary'
          WHEN 'consolidation' THEN 'memory_consolidation'
          WHEN 'reconciliation' THEN 'memory_reconciliation' END
          WHERE purpose IS NULL;
        ALTER TABLE memory_input_manifests ALTER COLUMN logical_role SET NOT NULL;
        ALTER TABLE memory_input_manifests ALTER COLUMN purpose SET NOT NULL;
        ALTER TABLE memory_input_manifest_items ADD COLUMN IF NOT EXISTS source_revision text;
        ALTER TABLE memory_input_manifest_items ADD COLUMN IF NOT EXISTS source_redacted boolean;
        ALTER TABLE memory_input_manifest_items ADD COLUMN IF NOT EXISTS source_visual_only boolean;

        ALTER TABLE proactive_jobs ADD COLUMN IF NOT EXISTS conversation_id uuid;
        UPDATE proactive_jobs AS j SET conversation_id = c.conversation_id
          FROM proactive_candidates AS c
          WHERE j.candidate_id = c.id AND j.account_id = c.account_id
            AND j.conversation_id IS NULL;

        UPDATE model_runs AS r SET
          memory_job_id = m.memory_job_id,
          conversation_id = m.conversation_id
          FROM memory_input_manifests AS m
          WHERE r.memory_input_manifest_id = m.id
            AND r.account_id = m.account_id
            AND r.memory_job_id IS NULL;
        UPDATE memory_input_manifests AS m SET
          prompt_bundle_sha256 = r.prompt_bundle_sha256,
          capability_snapshot_sha256 = r.capability_snapshot_sha256
          FROM model_runs AS r
          WHERE r.memory_input_manifest_id = m.id
            AND r.account_id = m.account_id
            AND r.conversation_id = m.conversation_id
            AND r.config_version_id = m.model_config_version_id
            AND r.credential_version_id = m.credential_version_id
            AND m.prompt_bundle_sha256 IS NULL
            AND m.capability_snapshot_sha256 IS NULL;
        """
    )


def _assert_legacy_rows_upgradeable() -> None:
    """Refuse ambiguous legacy ownership instead of inventing provenance."""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM model_runs
            WHERE logical_role = 'embedding'
          ) THEN
            RAISE EXCEPTION
              '0028 cannot migrate legacy embedding model runs safely';
          END IF;
          IF EXISTS (
            SELECT 1 FROM model_runs
            WHERE conversation_id IS NULL OR
              ((turn_id IS NOT NULL)::integer + (memory_job_id IS NOT NULL)::integer +
               (proactive_job_id IS NOT NULL)::integer) <> 1 OR
              (turn_id IS NOT NULL AND
               (logical_role <> 'main_ai' OR
                purpose NOT IN ('conversation_reply','copilot_reactive_draft') OR
                memory_input_manifest_id IS NOT NULL)) OR
              (memory_job_id IS NOT NULL AND
               (logical_role <> 'memory_agent' OR
                purpose NOT IN ('memory_episode','memory_rolling_summary',
                                'memory_consolidation','memory_reconciliation') OR
                memory_input_manifest_id IS NULL)) OR
              proactive_job_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION '0028 cannot infer legacy model-run ownership safely';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_input_manifests
            WHERE (model_config_version_id IS NULL) <>
                  (credential_version_id IS NULL) OR
              (model_config_version_id IS NULL) <>
                  (prompt_bundle_sha256 IS NULL) OR
              (model_config_version_id IS NULL) <>
                  (capability_snapshot_sha256 IS NULL)
          ) THEN
            RAISE EXCEPTION '0028 cannot infer legacy memory generation provenance safely';
          END IF;
          IF EXISTS (
            SELECT 1 FROM proactive_jobs AS j
            LEFT JOIN proactive_candidates AS c
              ON c.id = j.candidate_id AND c.account_id = j.account_id
            WHERE (j.job_kind = 'candidate_due' AND
                   (c.id IS NULL OR j.conversation_id IS DISTINCT FROM c.conversation_id)) OR
              (j.job_kind IN ('compensation_scan','budget_reaper') AND
               (j.candidate_id IS NOT NULL OR j.conversation_id IS NOT NULL))
          ) THEN
            RAISE EXCEPTION '0028 cannot infer proactive job scope safely';
          END IF;
        END;
        $$;
        ALTER TABLE model_runs ALTER COLUMN conversation_id SET NOT NULL;
        """
    )


def _replace_constraints() -> None:
    op.execute(
        """
        -- Historical create migrations import the current metadata. That can leave
        -- either the pre-M8 names or the current names in place before this revision.
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS logical_role_values;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_logical_role_values;
        ALTER TABLE model_runs ADD CONSTRAINT ck_model_runs_logical_role_values CHECK (
          logical_role IN ('main_ai','memory_agent','proactive_agent'));
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS owner_exactly_one;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_owner_exactly_one;
        ALTER TABLE model_runs ADD CONSTRAINT ck_model_runs_owner_exactly_one CHECK (
          (turn_id IS NOT NULL)::integer + (memory_job_id IS NOT NULL)::integer +
          (proactive_job_id IS NOT NULL)::integer = 1);
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS owner_role_purpose_match;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_owner_role_purpose_match;
        ALTER TABLE model_runs ADD CONSTRAINT ck_model_runs_owner_role_purpose_match CHECK (
          (turn_id IS NOT NULL AND logical_role = 'main_ai' AND
           purpose IN ('conversation_reply','copilot_reactive_draft') AND
           memory_input_manifest_id IS NULL AND proactive_input_manifest_id IS NULL) OR
          (memory_job_id IS NOT NULL AND logical_role = 'memory_agent' AND
           purpose IN ('memory_episode','memory_rolling_summary','memory_consolidation',
                       'memory_reconciliation') AND turn_id IS NULL AND
           context_manifest_id IS NULL AND memory_input_manifest_id IS NOT NULL AND
           proactive_input_manifest_id IS NULL) OR
          (proactive_job_id IS NOT NULL AND turn_id IS NULL AND memory_job_id IS NULL AND
           context_manifest_id IS NULL AND memory_input_manifest_id IS NULL AND
           proactive_input_manifest_id IS NOT NULL AND
           ((logical_role = 'proactive_agent' AND purpose = 'proactive_decision') OR
            (logical_role = 'main_ai' AND purpose = 'proactive_final'))));

        ALTER TABLE memory_jobs DROP CONSTRAINT IF EXISTS uq_memory_jobs_conversation_scope;
        ALTER TABLE memory_jobs ADD CONSTRAINT uq_memory_jobs_conversation_scope
          UNIQUE (id, account_id, conversation_id);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS logical_role_memory_agent;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_logical_role_memory_agent;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          ck_memory_input_manifests_logical_role_memory_agent CHECK (
            logical_role = 'memory_agent');
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS kind_purpose_match;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_kind_purpose_match;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          ck_memory_input_manifests_kind_purpose_match CHECK (
            (manifest_kind = 'episode' AND purpose = 'memory_episode' AND
             output_schema_version = 1) OR
            (manifest_kind = 'rolling_summary' AND purpose = 'memory_rolling_summary' AND
             output_schema_version = 2) OR
            (manifest_kind = 'consolidation' AND purpose = 'memory_consolidation' AND
             output_schema_version = 3) OR
            (manifest_kind = 'reconciliation' AND purpose = 'memory_reconciliation' AND
             output_schema_version = 1));
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          uq_memory_input_manifests_owner_scope;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          uq_memory_input_manifests_owner_scope
          UNIQUE (id, account_id, conversation_id, memory_job_id);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          fk_memory_input_manifests_job_scope;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          fk_memory_input_manifests_job_scope
          FOREIGN KEY (memory_job_id, account_id, conversation_id)
          REFERENCES memory_jobs(id, account_id, conversation_id);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          uq_memory_input_manifests_run_provenance;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          uq_memory_input_manifests_run_provenance UNIQUE
          (id, account_id, conversation_id, memory_job_id, logical_role, purpose,
           model_config_version_id, credential_version_id, prompt_version, prompt_bundle_sha256,
           capability_snapshot_sha256, output_schema_version);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS prompt_hash_32_bytes;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_prompt_hash_32_bytes;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          ck_memory_input_manifests_prompt_hash_32_bytes CHECK (
            prompt_bundle_sha256 IS NULL OR octet_length(prompt_bundle_sha256) = 32);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS capability_hash_32_bytes;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_capability_hash_32_bytes;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          ck_memory_input_manifests_capability_hash_32_bytes CHECK (
            capability_snapshot_sha256 IS NULL OR
            octet_length(capability_snapshot_sha256) = 32);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS generation_provenance_complete;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_generation_provenance_complete;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          ck_memory_input_manifests_generation_provenance_complete CHECK (
            (model_config_version_id IS NULL AND credential_version_id IS NULL AND
             prompt_bundle_sha256 IS NULL AND capability_snapshot_sha256 IS NULL) OR
            (model_config_version_id IS NOT NULL AND credential_version_id IS NOT NULL AND
             prompt_bundle_sha256 IS NOT NULL AND capability_snapshot_sha256 IS NOT NULL));
        ALTER TABLE memory_input_manifest_items DROP CONSTRAINT IF EXISTS source_snapshot_complete;
        ALTER TABLE memory_input_manifest_items DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifest_items_source_snapshot_complete;
        ALTER TABLE memory_input_manifest_items ADD CONSTRAINT
          ck_memory_input_manifest_items_source_snapshot_complete CHECK (
            (source_revision IS NULL AND source_redacted IS NULL AND
             source_visual_only IS NULL) OR
            (source_revision IS NOT NULL AND source_redacted IS NOT NULL AND
             source_visual_only IS NOT NULL));

        -- Both candidate-scope foreign keys depend on this candidate key.
        -- Remove them explicitly before replacing it; CASCADE could silently
        -- remove unrelated integrity constraints during a forward retry.
        ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS
          fk_proactive_decisions_candidate_scope;
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          fk_proactive_jobs_candidate_scope;
        ALTER TABLE proactive_candidates DROP CONSTRAINT IF EXISTS
          uq_proactive_candidates_conversation_scope;
        ALTER TABLE proactive_candidates ADD CONSTRAINT
          uq_proactive_candidates_conversation_scope UNIQUE (id, account_id, conversation_id);
        ALTER TABLE proactive_occurrence_evidence DROP CONSTRAINT IF EXISTS
          uq_proactive_occurrence_evidence_scope;
        ALTER TABLE proactive_occurrence_evidence ADD CONSTRAINT
          uq_proactive_occurrence_evidence_scope UNIQUE (occurrence_id, account_id, ordinal);
        ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS
          uq_proactive_decisions_owner_scope;
        ALTER TABLE proactive_decisions ADD CONSTRAINT uq_proactive_decisions_owner_scope
          UNIQUE (id, account_id, conversation_id, candidate_id);
        ALTER TABLE proactive_decisions ADD CONSTRAINT
          fk_proactive_decisions_candidate_scope
          FOREIGN KEY (candidate_id, account_id, conversation_id)
          REFERENCES proactive_candidates(id, account_id, conversation_id);
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          uq_proactive_jobs_conversation_scope;
        ALTER TABLE proactive_jobs ADD CONSTRAINT uq_proactive_jobs_conversation_scope
          UNIQUE (id, account_id, conversation_id);
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          fk_proactive_jobs_conversation_scope;
        ALTER TABLE proactive_jobs ADD CONSTRAINT fk_proactive_jobs_conversation_scope
          FOREIGN KEY (conversation_id, account_id)
          REFERENCES conversations(id, account_id);
        ALTER TABLE proactive_jobs ADD CONSTRAINT fk_proactive_jobs_candidate_scope
          FOREIGN KEY (candidate_id, account_id, conversation_id)
          REFERENCES proactive_candidates(id, account_id, conversation_id);
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          ck_proactive_jobs_candidate_scope_matches_kind;
        ALTER TABLE proactive_jobs ADD CONSTRAINT
          ck_proactive_jobs_candidate_scope_matches_kind CHECK (
            (job_kind = 'candidate_due' AND candidate_id IS NOT NULL AND
             conversation_id IS NOT NULL) OR
            (job_kind IN ('compensation_scan','budget_reaper') AND
             candidate_id IS NULL AND conversation_id IS NULL));
        """
    )


def _add_model_run_scope_constraints() -> None:
    op.execute(
        """
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS fk_model_runs_memory_job_scope;
        ALTER TABLE model_runs ADD CONSTRAINT fk_model_runs_memory_job_scope
          FOREIGN KEY (memory_job_id, account_id, conversation_id)
          REFERENCES memory_jobs(id, account_id, conversation_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS fk_model_runs_memory_manifest_provenance;
        ALTER TABLE model_runs ADD CONSTRAINT fk_model_runs_memory_manifest_provenance
          FOREIGN KEY (memory_input_manifest_id, account_id, conversation_id, memory_job_id,
                       logical_role, purpose, config_version_id, credential_version_id,
                       prompt_version,
                       prompt_bundle_sha256, capability_snapshot_sha256,
                       output_schema_version)
          REFERENCES memory_input_manifests
            (id, account_id, conversation_id, memory_job_id, logical_role, purpose,
             model_config_version_id, credential_version_id, prompt_version, prompt_bundle_sha256,
             capability_snapshot_sha256, output_schema_version)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS fk_model_runs_proactive_job_scope;
        ALTER TABLE model_runs ADD CONSTRAINT fk_model_runs_proactive_job_scope
          FOREIGN KEY (proactive_job_id, account_id, conversation_id)
          REFERENCES proactive_jobs(id, account_id, conversation_id)
          DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS
          fk_model_runs_proactive_manifest_provenance;
        ALTER TABLE model_runs ADD CONSTRAINT fk_model_runs_proactive_manifest_provenance
          FOREIGN KEY (proactive_input_manifest_id, account_id, conversation_id,
                       proactive_job_id, logical_role, purpose, config_version_id,
                       credential_version_id, prompt_version, prompt_bundle_sha256,
                       capability_snapshot_sha256, output_schema_version)
          REFERENCES proactive_input_manifests
            (id, account_id, conversation_id, proactive_job_id, logical_role, purpose,
             model_config_version_id, credential_version_id, prompt_version,
             prompt_bundle_sha256, capability_snapshot_sha256, output_schema_version)
          DEFERRABLE INITIALLY DEFERRED;

        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS uq_model_runs_memory_owner_scope;
        ALTER TABLE model_runs ADD CONSTRAINT uq_model_runs_memory_owner_scope
          UNIQUE (id, account_id, conversation_id, memory_job_id, logical_role, purpose);
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS uq_model_runs_proactive_owner_scope;
        ALTER TABLE model_runs ADD CONSTRAINT uq_model_runs_proactive_owner_scope
          UNIQUE (id, account_id, conversation_id, proactive_job_id, logical_role, purpose);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_model_runs_memory_generation
          ON model_runs(memory_job_id, logical_role, generation_no)
          WHERE memory_job_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_model_runs_proactive_purpose_generation
          ON model_runs(proactive_job_id, logical_role, purpose, generation_no)
          WHERE proactive_job_id IS NOT NULL;
        """
    )


def _install_immutability_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_memory_input_manifest_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE sealed timestamptz;
        BEGIN
          IF TG_TABLE_NAME = 'memory_input_manifests' THEN
            RAISE EXCEPTION 'memory input manifests are immutable';
          END IF;
          SELECT j.sealed_at INTO sealed FROM memory_input_manifests AS m
            JOIN memory_jobs AS j ON j.id = m.memory_job_id AND j.account_id = m.account_id
            WHERE m.id = COALESCE(NEW.manifest_id, OLD.manifest_id);
          IF TG_OP <> 'INSERT' OR sealed IS NOT NULL THEN
            RAISE EXCEPTION 'sealed memory input manifest items are immutable';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_memory_input_manifests_immutable
          BEFORE UPDATE OR DELETE ON memory_input_manifests
          FOR EACH ROW EXECUTE FUNCTION enforce_memory_input_manifest_immutability();
        CREATE TRIGGER trg_memory_input_manifest_items_immutable
          BEFORE INSERT OR UPDATE OR DELETE ON memory_input_manifest_items
          FOR EACH ROW EXECUTE FUNCTION enforce_memory_input_manifest_immutability();

        CREATE FUNCTION enforce_proactive_input_manifest_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE sealed timestamptz;
        BEGIN
          IF TG_TABLE_NAME = 'proactive_input_manifests' THEN
            IF TG_OP = 'UPDATE' AND OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL AND
               OLD.manifest_sha256 IS NULL AND NEW.manifest_sha256 IS NOT NULL AND
               NEW.id = OLD.id AND NEW.account_id = OLD.account_id AND
               NEW.conversation_id = OLD.conversation_id AND
               NEW.proactive_job_id = OLD.proactive_job_id AND
               NEW.candidate_id = OLD.candidate_id AND
               NEW.proactive_decision_id IS NOT DISTINCT FROM OLD.proactive_decision_id AND
               NEW.logical_role = OLD.logical_role AND NEW.purpose = OLD.purpose AND
               NEW.job_fencing_token = OLD.job_fencing_token AND
               NEW.candidate_generation = OLD.candidate_generation AND
               NEW.candidate_key = OLD.candidate_key AND
               NEW.candidate_membership_hash = OLD.candidate_membership_hash AND
               NEW.decision_snapshot IS NOT DISTINCT FROM OLD.decision_snapshot AND
               NEW.decision_snapshot_sha256 IS NOT DISTINCT FROM
                 OLD.decision_snapshot_sha256 AND
               NEW.mode_version = OLD.mode_version AND
               NEW.content_revision = OLD.content_revision AND
               NEW.activity_revision = OLD.activity_revision AND
               NEW.policy_version_id = OLD.policy_version_id AND
               NEW.policy_version_no = OLD.policy_version_no AND
               NEW.policy_snapshot = OLD.policy_snapshot AND
               NEW.policy_snapshot_sha256 = OLD.policy_snapshot_sha256 AND
               NEW.timezone_snapshot = OLD.timezone_snapshot AND
               NEW.context_contract_version = OLD.context_contract_version AND
               NEW.prompt_version_id = OLD.prompt_version_id AND
               NEW.prompt_version = OLD.prompt_version AND
               NEW.prompt_bundle_sha256 = OLD.prompt_bundle_sha256 AND
               NEW.model_config_version_id = OLD.model_config_version_id AND
               NEW.credential_version_id = OLD.credential_version_id AND
               NEW.capability_snapshot_sha256 = OLD.capability_snapshot_sha256 AND
               NEW.input_schema_version = OLD.input_schema_version AND
               NEW.output_schema_version = OLD.output_schema_version AND
               NEW.input_token_estimate = OLD.input_token_estimate AND
               NEW.occurrence_count = OLD.occurrence_count AND
               NEW.created_at = OLD.created_at THEN RETURN NEW;
            END IF;
            RAISE EXCEPTION 'proactive input manifests are immutable after creation';
          END IF;
          SELECT sealed_at INTO sealed FROM proactive_input_manifests
            WHERE id = COALESCE(NEW.manifest_id, OLD.manifest_id);
          IF TG_OP <> 'INSERT' OR sealed IS NOT NULL THEN
            RAISE EXCEPTION 'sealed proactive input manifest items are immutable';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_proactive_input_manifests_immutable
          BEFORE UPDATE OR DELETE ON proactive_input_manifests
          FOR EACH ROW EXECUTE FUNCTION enforce_proactive_input_manifest_immutability();
        CREATE TRIGGER trg_proactive_input_manifest_items_immutable
          BEFORE INSERT OR UPDATE OR DELETE ON proactive_input_manifest_items
          FOR EACH ROW EXECUTE FUNCTION enforce_proactive_input_manifest_immutability();
        """
    )


def _install_credential_accessor() -> None:
    """Create the least-privilege runtime credential accessor with run binding."""

    op.execute(
        """
        DROP FUNCTION IF EXISTS public.get_model_credential_version_by_id(uuid, uuid, uuid);
        DROP FUNCTION IF EXISTS public.get_model_credential_version_by_id(uuid, uuid);
        CREATE FUNCTION public.get_model_credential_version_by_id(
          requested_run_id uuid,
          requested_profile_id uuid,
          requested_version_id uuid
        )
        RETURNS TABLE (
          id uuid,
          credential_id uuid,
          profile_id uuid,
          version_no integer,
          algorithm text,
          key_version integer,
          aad_schema_version smallint,
          nonce bytea,
          ciphertext bytea,
          secret_fingerprint bytea
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT
            value.id,
            value.credential_id,
            value.profile_id,
            value.version_no,
            value.algorithm,
            value.key_version,
            value.aad_schema_version,
            value.nonce,
            value.ciphertext,
            value.secret_fingerprint
          FROM public.model_credential_versions AS value
          JOIN public.model_credentials AS identity
            ON identity.id = value.credential_id
           AND identity.profile_id = value.profile_id
          JOIN public.model_runs AS run
            ON run.id = requested_run_id
           AND run.model_profile_id = requested_profile_id
           AND run.credential_version_id = value.id
           AND run.logical_role IN ('main_ai', 'memory_agent', 'proactive_agent')
           AND run.state = 'running'
           AND run.cancel_requested_at IS NULL
           AND (
             -- App owns immediate conversation generation only.  Background
             -- memory/proactive work is never allowed to expose its profile key
             -- to the Telethon-owning process.
             -- SECURITY DEFINER changes current_user to the migrator.  The
             -- immutable login identity remains available as session_user and
             -- is one-to-one with the process runtime role.
             (session_user = 'telegram_userbot_app_login' AND
              run.logical_role = 'main_ai' AND run.purpose IN
                ('conversation_reply', 'copilot_reactive_draft')) OR
             -- Worker owns all durable background generations.  A proactive
             -- final uses the main_ai profile, but remains worker-owned until
             -- app performs the final send gate.
             (session_user = 'telegram_userbot_worker_login' AND
              ((run.logical_role = 'memory_agent' AND run.purpose IN
                ('memory_episode', 'memory_rolling_summary', 'memory_consolidation',
                 'memory_reconciliation')) OR
               (run.logical_role = 'proactive_agent' AND
                run.purpose = 'proactive_decision') OR
               (run.logical_role = 'main_ai' AND run.purpose = 'proactive_final')))
           )
          JOIN public.model_config_versions AS config
            ON config.id = run.config_version_id
           AND config.profile_id = requested_profile_id
           AND config.credential_id = value.credential_id
          WHERE value.id = requested_version_id
            AND value.profile_id = requested_profile_id
            AND value.destroyed_at IS NULL
        $$;
        -- Disposable integration databases use their bootstrap superuser and do
        -- not contain the production migration group role.  Keep that owner
        -- there; the production role-closure below this migration assigns the
        -- fixed migrator owner after its role contract has been verified.
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = 'telegram_userbot_migrator'
          ) THEN
            ALTER FUNCTION public.get_model_credential_version_by_id(uuid, uuid, uuid)
              OWNER TO telegram_userbot_migrator;
          END IF;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.get_model_credential_version_by_id(uuid, uuid, uuid)
          FROM PUBLIC;
        """
    )


def upgrade() -> None:
    _upgrade_existing_columns()
    _assert_legacy_rows_upgradeable()
    _replace_constraints()
    tables = [metadata.tables[name] for name in M8_MODEL_TABLES]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    _add_model_run_scope_constraints()
    _install_credential_accessor()
    _install_immutability_guards()
    _replace_schema_revision(
        projection=revision,
        event_revisions=(
            "0025_m8_service_status",
            "0026_m8_data_export",
            "0027_m8_model_run_claim",
            revision,
        ),
    )


def downgrade() -> None:
    _replace_schema_revision(
        projection="0027_m8_model_run_claim",
        event_revisions=(
            "0025_m8_service_status",
            "0026_m8_data_export",
            "0027_m8_model_run_claim",
        ),
    )
    op.execute(
        """
        DROP FUNCTION IF EXISTS enforce_proactive_input_manifest_immutability() CASCADE;
        DROP FUNCTION IF EXISTS enforce_memory_input_manifest_immutability() CASCADE;
        DROP FUNCTION IF EXISTS public.get_model_credential_version_by_id(uuid, uuid, uuid);
        DROP FUNCTION IF EXISTS public.get_model_credential_version_by_id(uuid, uuid);
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS
          fk_model_runs_proactive_manifest_provenance;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS uq_model_runs_proactive_owner_scope;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS uq_model_runs_memory_owner_scope;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS fk_model_runs_proactive_job_scope;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS
          fk_model_runs_memory_manifest_provenance;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS fk_model_runs_memory_job_scope;
        DROP INDEX IF EXISTS uq_model_runs_proactive_purpose_generation;
        DROP INDEX IF EXISTS uq_model_runs_memory_generation;
        """
    )
    tables = [metadata.tables[name] for name in reversed(M8_MODEL_TABLES)]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
    op.execute(
        """
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          ck_proactive_jobs_candidate_scope_matches_kind;
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          fk_proactive_jobs_candidate_scope;
        ALTER TABLE proactive_jobs ADD CONSTRAINT fk_proactive_jobs_candidate_scope
          FOREIGN KEY (candidate_id, account_id)
          REFERENCES proactive_candidates(id, account_id);
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          fk_proactive_jobs_conversation_scope;
        ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS
          uq_proactive_jobs_conversation_scope;
        ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS
          fk_proactive_decisions_candidate_scope;
        ALTER TABLE proactive_decisions ADD CONSTRAINT
          fk_proactive_decisions_candidate_scope
          FOREIGN KEY (candidate_id, account_id)
          REFERENCES proactive_candidates(id, account_id);
        ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS
          uq_proactive_decisions_owner_scope;
        ALTER TABLE proactive_occurrence_evidence DROP CONSTRAINT IF EXISTS
          uq_proactive_occurrence_evidence_scope;
        ALTER TABLE proactive_candidates DROP CONSTRAINT IF EXISTS
          uq_proactive_candidates_conversation_scope;
        ALTER TABLE memory_input_manifest_items DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifest_items_source_snapshot_complete;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          fk_memory_input_manifests_job_scope;
        ALTER TABLE memory_input_manifests ADD CONSTRAINT
          fk_memory_input_manifests_job_scope
          FOREIGN KEY (memory_job_id, account_id)
          REFERENCES memory_jobs(id, account_id);
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_generation_provenance_complete;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_capability_hash_32_bytes;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          ck_memory_input_manifests_prompt_hash_32_bytes;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          uq_memory_input_manifests_run_provenance;
        ALTER TABLE memory_input_manifests DROP CONSTRAINT IF EXISTS
          uq_memory_input_manifests_owner_scope;
        ALTER TABLE memory_jobs DROP CONSTRAINT IF EXISTS uq_memory_jobs_conversation_scope;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_owner_role_purpose_match;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_owner_exactly_one;
        ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS ck_model_runs_logical_role_values;
        ALTER TABLE model_runs ADD CONSTRAINT ck_model_runs_logical_role_values CHECK (
          logical_role IN ('main_ai','memory_agent','proactive_agent','embedding'));
        ALTER TABLE model_runs ALTER COLUMN conversation_id DROP NOT NULL;
        ALTER TABLE proactive_jobs DROP COLUMN IF EXISTS conversation_id;
        ALTER TABLE memory_input_manifest_items DROP COLUMN IF EXISTS source_visual_only;
        ALTER TABLE memory_input_manifest_items DROP COLUMN IF EXISTS source_redacted;
        ALTER TABLE memory_input_manifest_items DROP COLUMN IF EXISTS source_revision;
        ALTER TABLE memory_input_manifests DROP COLUMN IF EXISTS capability_snapshot_sha256;
        ALTER TABLE memory_input_manifests DROP COLUMN IF EXISTS prompt_bundle_sha256;
        ALTER TABLE memory_input_manifests DROP COLUMN IF EXISTS purpose;
        ALTER TABLE memory_input_manifests DROP COLUMN IF EXISTS logical_role;
        ALTER TABLE model_runs DROP COLUMN IF EXISTS proactive_input_manifest_id;
        ALTER TABLE model_runs DROP COLUMN IF EXISTS proactive_job_id;
        ALTER TABLE model_runs DROP COLUMN IF EXISTS memory_job_id;
        """
    )
