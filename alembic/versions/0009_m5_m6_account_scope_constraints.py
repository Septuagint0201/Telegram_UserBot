"""Bind M5/M6 references to their owning account."""

# ruff: noqa: S608 - identifiers are fixed migration constants

from collections.abc import Sequence

from alembic import op

revision: str = "0009_m5_m6_account_scope"
down_revision: str | Sequence[str] | None = "0008_m7_proactive_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_constraints(table: str, names: Sequence[str]) -> None:
    for name in names:
        op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"')


def _create_unique(table: str, name: str, columns: Sequence[str]) -> None:
    quoted = ", ".join(f'"{column}"' for column in columns)
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = '{table}'::regclass AND conname = '{name}'
          ) THEN
            ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE ({quoted});
          END IF;
        END
        $$;
        """
    )


def _ensure_scope_data() -> None:
    op.execute(
        """
        ALTER TABLE context_manifest_items
          ADD COLUMN IF NOT EXISTS account_id uuid;
        UPDATE context_manifest_items i
        SET account_id = m.account_id
        FROM context_manifests m
        WHERE m.id = i.manifest_id AND i.account_id IS NULL;

        ALTER TABLE memory_input_manifest_items
          ADD COLUMN IF NOT EXISTS account_id uuid;
        UPDATE memory_input_manifest_items i
        SET account_id = m.account_id
        FROM memory_input_manifests m
        WHERE m.id = i.manifest_id AND i.account_id IS NULL;

        ALTER TABLE memory_relations
          ADD COLUMN IF NOT EXISTS account_id uuid;
        UPDATE memory_relations r
        SET account_id = v.account_id
        FROM memory_versions v
        WHERE v.id = r.from_version_id AND r.account_id IS NULL;

        UPDATE embedding_records er
        SET account_id = target.account_id
        FROM (
          SELECT e.id,
                 COALESCE(mv.account_id, sv.account_id, mr.account_id) AS account_id
          FROM embedding_records e
          LEFT JOIN memory_versions mv ON mv.id = e.memory_version_id
          LEFT JOIN summary_versions sv ON sv.id = e.summary_version_id
          LEFT JOIN message_revisions mr ON mr.id = e.message_revision_id
        ) AS target
        WHERE er.id = target.id AND er.account_id IS NULL;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM context_manifest_items i
            LEFT JOIN context_manifests m ON m.id = i.manifest_id
            WHERE i.account_id IS NULL OR m.id IS NULL OR i.account_id <> m.account_id
          ) THEN
            RAISE EXCEPTION 'context manifest item account scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_input_manifest_items i
            LEFT JOIN memory_input_manifests m ON m.id = i.manifest_id
            WHERE i.account_id IS NULL OR m.id IS NULL OR i.account_id <> m.account_id
          ) THEN
            RAISE EXCEPTION 'memory manifest item account scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM memory_relations r
            JOIN memory_versions f ON f.id = r.from_version_id
            LEFT JOIN memory_versions t ON t.id = r.to_version_id
            WHERE r.account_id IS NULL OR f.account_id <> r.account_id
               OR t.id IS NULL OR t.account_id <> r.account_id
          ) THEN
            RAISE EXCEPTION 'memory relation account scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM embedding_records e
            LEFT JOIN memory_versions mv ON mv.id = e.memory_version_id
            LEFT JOIN summary_versions sv ON sv.id = e.summary_version_id
            LEFT JOIN message_revisions mr ON mr.id = e.message_revision_id
            WHERE e.account_id IS NULL
               OR (mv.id IS NOT NULL AND e.account_id <> mv.account_id)
               OR (sv.id IS NOT NULL AND e.account_id <> sv.account_id)
               OR (mr.id IS NOT NULL AND e.account_id <> mr.account_id)
          ) THEN
            RAISE EXCEPTION 'embedding record account scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_input_manifests m
            LEFT JOIN memory_jobs j ON j.id = m.memory_job_id
            WHERE j.id IS NULL OR m.account_id <> j.account_id
          ) THEN
            RAISE EXCEPTION 'memory input manifest job scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_jobs j
            LEFT JOIN memory_input_manifests m ON m.id = j.input_manifest_id
            WHERE j.input_manifest_id IS NOT NULL
              AND (m.id IS NULL OR j.account_id <> m.account_id)
          ) THEN
            RAISE EXCEPTION 'memory job input manifest scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM model_runs r
            JOIN memory_input_manifests m ON m.id = r.memory_input_manifest_id
            WHERE r.account_id <> m.account_id
          ) THEN
            RAISE EXCEPTION 'model run input manifest scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_proposals p
            JOIN memory_jobs j ON j.id = p.memory_job_id
            WHERE p.account_id <> j.account_id
          ) THEN
            RAISE EXCEPTION 'memory proposal job scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_proposals p
            JOIN model_runs r ON r.id = p.model_run_id
            WHERE p.account_id <> r.account_id OR p.model_role <> r.logical_role
          ) THEN
            RAISE EXCEPTION 'memory proposal model run scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_versions v
            JOIN model_runs r ON r.id = v.model_run_id
            WHERE v.account_id <> r.account_id OR v.model_role <> r.logical_role
          ) THEN
            RAISE EXCEPTION 'memory version model run scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM summary_versions v
            JOIN model_runs r ON r.id = v.model_run_id
            WHERE v.account_id <> r.account_id OR v.model_role <> r.logical_role
          ) THEN
            RAISE EXCEPTION 'summary version model run scope is inconsistent';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE context_manifest_items ALTER COLUMN account_id SET NOT NULL;
        ALTER TABLE memory_input_manifest_items ALTER COLUMN account_id SET NOT NULL;
        ALTER TABLE memory_relations ALTER COLUMN account_id SET NOT NULL;
        ALTER TABLE embedding_records ALTER COLUMN account_id SET NOT NULL;
        """
    )


def _create_scope_constraints() -> None:
    unique_constraints = (
        ("media_objects", "uq_media_objects_id_account", ("id", "account_id")),
        ("model_runs", "uq_model_runs_id_account_role", ("id", "account_id", "logical_role")),
        ("memory_jobs", "uq_memory_jobs_id_account", ("id", "account_id")),
        (
            "memory_input_manifests",
            "uq_memory_input_manifests_id_account",
            ("id", "account_id"),
        ),
        ("memory_proposals", "uq_memory_proposals_id_account", ("id", "account_id")),
    )
    old_constraints = {
        "context_manifest_items": (
            "fk_context_manifest_items_manifest_id_context_manifests",
            "fk_context_manifest_items_message_revision_id_message_revisions",
            "fk_context_manifest_items_media_object_id_media_objects",
            "fk_context_manifest_items_memory_version",
            "fk_context_manifest_items_summary_version",
            "fk_context_manifest_items_manifest_scope",
            "fk_context_manifest_items_message_revision_scope",
            "fk_context_manifest_items_media_scope",
            "fk_context_manifest_items_memory_version_scope",
            "fk_context_manifest_items_summary_version_scope",
        ),
        "context_preview_requests": (
            "fk_context_preview_requests_context_manifest_id_context_4e1c",
            "fk_context_preview_requests_manifest_scope",
        ),
        "memory_jobs": ("fk_memory_jobs_input_manifest", "fk_memory_jobs_input_manifest_scope"),
        "model_runs": (
            "fk_model_runs_memory_input_manifest",
            "fk_model_runs_memory_input_manifest_scope",
        ),
        "memory_input_manifests": (
            "fk_memory_input_manifests_job",
            "fk_memory_input_manifests_job_scope",
        ),
        "memory_input_manifest_items": (
            "fk_memory_manifest_items_manifest",
            "fk_memory_manifest_items_message_revision",
            "fk_memory_manifest_items_media",
            "fk_memory_manifest_items_memory_version",
            "fk_memory_manifest_items_summary_version",
            "fk_memory_manifest_items_manifest_scope",
            "fk_memory_manifest_items_message_revision_scope",
            "fk_memory_manifest_items_media_scope",
            "fk_memory_manifest_items_memory_version_scope",
            "fk_memory_manifest_items_summary_version_scope",
        ),
        "memory_watermarks": ("fk_memory_watermarks_job", "fk_memory_watermarks_job_scope"),
        "memory_versions": ("fk_memory_versions_model_run", "fk_memory_versions_model_run_scope"),
        "memory_proposals": (
            "fk_memory_proposals_contact_scope",
            "fk_memory_proposals_conversation_scope",
            "fk_memory_proposals_job",
            "fk_memory_proposals_job_scope",
            "fk_memory_proposals_model_run",
            "fk_memory_proposals_model_run_scope",
            "fk_memory_proposals_accepted_version",
            "fk_memory_proposals_accepted_version_scope",
        ),
        "memory_proposal_targets": (
            "fk_memory_proposal_targets_proposal",
            "fk_memory_proposal_targets_proposal_scope",
        ),
        "memory_proposal_evidence": (
            "fk_memory_proposal_evidence_proposal",
            "fk_memory_proposal_evidence_proposal_scope",
            "fk_memory_proposal_evidence_revision",
            "fk_memory_proposal_evidence_revision_scope",
            "fk_memory_proposal_evidence_media",
            "fk_memory_proposal_evidence_media_scope",
        ),
        "memory_evidence": (
            "fk_memory_evidence_version",
            "fk_memory_evidence_version_scope",
            "fk_memory_evidence_revision",
            "fk_memory_evidence_revision_scope",
            "fk_memory_evidence_summary",
            "fk_memory_evidence_summary_scope",
            "fk_memory_evidence_other_version",
            "fk_memory_evidence_other_version_scope",
            "fk_memory_evidence_media",
            "fk_memory_evidence_media_scope",
        ),
        "memory_relations": (
            "fk_memory_relations_from",
            "fk_memory_relations_from_scope",
            "fk_memory_relations_to",
            "fk_memory_relations_to_scope",
        ),
        "summary_versions": (
            "fk_summary_versions_model_run",
            "fk_summary_versions_model_run_scope",
        ),
        "summary_version_sources": (
            "fk_summary_sources_revision",
            "fk_summary_sources_revision_scope",
            "fk_summary_sources_prior",
            "fk_summary_sources_prior_scope",
        ),
        "summary_watermarks": (
            "fk_summary_watermarks_version",
            "fk_summary_watermarks_version_scope",
        ),
        "embedding_records": (
            "fk_embedding_records_memory_version",
            "fk_embedding_records_memory_version_scope",
            "fk_embedding_records_summary_version",
            "fk_embedding_records_summary_version_scope",
            "fk_embedding_records_message_revision",
            "fk_embedding_records_message_revision_scope",
        ),
        "memory_review_actions": (
            "fk_memory_review_actions_proposal",
            "fk_memory_review_actions_proposal_scope",
            "fk_memory_review_actions_memory",
            "fk_memory_review_actions_memory_scope",
            "fk_memory_review_actions_conversation_scope",
        ),
    }
    for table, names in old_constraints.items():
        _drop_constraints(table, names)
    for table, name, columns in unique_constraints:
        _create_unique(table, name, columns)

    foreign_keys = (
        (
            "context_manifest_items",
            "fk_context_manifest_items_manifest_scope",
            "context_manifests",
            ("manifest_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_manifest_items",
            "fk_context_manifest_items_message_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_manifest_items",
            "fk_context_manifest_items_media_scope",
            "media_objects",
            ("media_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_manifest_items",
            "fk_context_manifest_items_memory_version_scope",
            "memory_versions",
            ("memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_manifest_items",
            "fk_context_manifest_items_summary_version_scope",
            "summary_versions",
            ("summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_preview_requests",
            "fk_context_preview_requests_manifest_scope",
            "context_manifests",
            ("context_manifest_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_jobs",
            "fk_memory_jobs_input_manifest_scope",
            "memory_input_manifests",
            ("input_manifest_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "model_runs",
            "fk_model_runs_memory_input_manifest_scope",
            "memory_input_manifests",
            ("memory_input_manifest_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifests",
            "fk_memory_input_manifests_job_scope",
            "memory_jobs",
            ("memory_job_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifest_items",
            "fk_memory_manifest_items_manifest_scope",
            "memory_input_manifests",
            ("manifest_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifest_items",
            "fk_memory_manifest_items_message_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifest_items",
            "fk_memory_manifest_items_media_scope",
            "media_objects",
            ("media_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifest_items",
            "fk_memory_manifest_items_memory_version_scope",
            "memory_versions",
            ("memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_input_manifest_items",
            "fk_memory_manifest_items_summary_version_scope",
            "summary_versions",
            ("summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_watermarks",
            "fk_memory_watermarks_job_scope",
            "memory_jobs",
            ("last_succeeded_job_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_versions",
            "fk_memory_versions_model_run_scope",
            "model_runs",
            ("model_run_id", "account_id", "model_role"),
            ("id", "account_id", "logical_role"),
        ),
        (
            "memory_proposals",
            "fk_memory_proposals_contact_scope",
            "contacts",
            ("contact_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposals",
            "fk_memory_proposals_conversation_scope",
            "conversations",
            ("conversation_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposals",
            "fk_memory_proposals_job_scope",
            "memory_jobs",
            ("memory_job_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposals",
            "fk_memory_proposals_model_run_scope",
            "model_runs",
            ("model_run_id", "account_id", "model_role"),
            ("id", "account_id", "logical_role"),
        ),
        (
            "memory_proposals",
            "fk_memory_proposals_accepted_version_scope",
            "memory_versions",
            ("accepted_memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposal_targets",
            "fk_memory_proposal_targets_proposal_scope",
            "memory_proposals",
            ("proposal_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposal_evidence",
            "fk_memory_proposal_evidence_proposal_scope",
            "memory_proposals",
            ("proposal_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposal_evidence",
            "fk_memory_proposal_evidence_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_proposal_evidence",
            "fk_memory_proposal_evidence_media_scope",
            "media_objects",
            ("media_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_evidence",
            "fk_memory_evidence_version_scope",
            "memory_versions",
            ("memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_evidence",
            "fk_memory_evidence_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_evidence",
            "fk_memory_evidence_summary_scope",
            "summary_versions",
            ("summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_evidence",
            "fk_memory_evidence_other_version_scope",
            "memory_versions",
            ("other_memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_evidence",
            "fk_memory_evidence_media_scope",
            "media_objects",
            ("media_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_relations",
            "fk_memory_relations_from_scope",
            "memory_versions",
            ("from_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_relations",
            "fk_memory_relations_to_scope",
            "memory_versions",
            ("to_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "summary_versions",
            "fk_summary_versions_model_run_scope",
            "model_runs",
            ("model_run_id", "account_id", "model_role"),
            ("id", "account_id", "logical_role"),
        ),
        (
            "summary_version_sources",
            "fk_summary_sources_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "summary_version_sources",
            "fk_summary_sources_prior_scope",
            "summary_versions",
            ("prior_summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "summary_watermarks",
            "fk_summary_watermarks_version_scope",
            "summary_versions",
            ("last_summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "embedding_records",
            "fk_embedding_records_memory_version_scope",
            "memory_versions",
            ("memory_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "embedding_records",
            "fk_embedding_records_summary_version_scope",
            "summary_versions",
            ("summary_version_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "embedding_records",
            "fk_embedding_records_message_revision_scope",
            "message_revisions",
            ("message_revision_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_review_actions",
            "fk_memory_review_actions_proposal_scope",
            "memory_proposals",
            ("proposal_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_review_actions",
            "fk_memory_review_actions_memory_scope",
            "memories",
            ("memory_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_review_actions",
            "fk_memory_review_actions_conversation_scope",
            "conversations",
            ("conversation_id", "account_id"),
            ("id", "account_id"),
        ),
    )
    for table, name, referred_table, local_columns, remote_columns in foreign_keys:
        if name == "fk_memory_jobs_input_manifest_scope":
            op.create_foreign_key(
                name,
                table,
                referred_table,
                list(local_columns),
                list(remote_columns),
                deferrable=True,
                initially="DEFERRED",
            )
        else:
            op.create_foreign_key(
                name, table, referred_table, list(local_columns), list(remote_columns)
            )


def upgrade() -> None:
    _ensure_scope_data()
    _create_scope_constraints()


def _restore_fk(
    name: str,
    table: str,
    referred_table: str,
    local_columns: Sequence[str],
    remote_columns: Sequence[str],
) -> None:
    if name == "fk_memory_jobs_input_manifest":
        op.create_foreign_key(
            name,
            table,
            referred_table,
            list(local_columns),
            list(remote_columns),
            deferrable=True,
            initially="DEFERRED",
        )
    else:
        op.create_foreign_key(
            name, table, referred_table, list(local_columns), list(remote_columns)
        )


def downgrade() -> None:
    tables = {
        "context_manifest_items": (
            "fk_context_manifest_items_manifest_scope",
            "fk_context_manifest_items_message_revision_scope",
            "fk_context_manifest_items_media_scope",
            "fk_context_manifest_items_memory_version_scope",
            "fk_context_manifest_items_summary_version_scope",
        ),
        "context_preview_requests": ("fk_context_preview_requests_manifest_scope",),
        "memory_jobs": ("fk_memory_jobs_input_manifest_scope",),
        "model_runs": ("fk_model_runs_memory_input_manifest_scope",),
        "memory_input_manifests": ("fk_memory_input_manifests_job_scope",),
        "memory_input_manifest_items": (
            "fk_memory_manifest_items_manifest_scope",
            "fk_memory_manifest_items_message_revision_scope",
            "fk_memory_manifest_items_media_scope",
            "fk_memory_manifest_items_memory_version_scope",
            "fk_memory_manifest_items_summary_version_scope",
        ),
        "memory_watermarks": ("fk_memory_watermarks_job_scope",),
        "memory_versions": ("fk_memory_versions_model_run_scope",),
        "memory_proposals": (
            "fk_memory_proposals_contact_scope",
            "fk_memory_proposals_conversation_scope",
            "fk_memory_proposals_job_scope",
            "fk_memory_proposals_model_run_scope",
            "fk_memory_proposals_accepted_version_scope",
        ),
        "memory_proposal_targets": ("fk_memory_proposal_targets_proposal_scope",),
        "memory_proposal_evidence": (
            "fk_memory_proposal_evidence_proposal_scope",
            "fk_memory_proposal_evidence_revision_scope",
            "fk_memory_proposal_evidence_media_scope",
        ),
        "memory_evidence": (
            "fk_memory_evidence_version_scope",
            "fk_memory_evidence_revision_scope",
            "fk_memory_evidence_summary_scope",
            "fk_memory_evidence_other_version_scope",
            "fk_memory_evidence_media_scope",
        ),
        "memory_relations": ("fk_memory_relations_from_scope", "fk_memory_relations_to_scope"),
        "summary_versions": ("fk_summary_versions_model_run_scope",),
        "summary_version_sources": (
            "fk_summary_sources_revision_scope",
            "fk_summary_sources_prior_scope",
        ),
        "summary_watermarks": ("fk_summary_watermarks_version_scope",),
        "embedding_records": (
            "fk_embedding_records_memory_version_scope",
            "fk_embedding_records_summary_version_scope",
            "fk_embedding_records_message_revision_scope",
        ),
        "memory_review_actions": (
            "fk_memory_review_actions_proposal_scope",
            "fk_memory_review_actions_memory_scope",
            "fk_memory_review_actions_conversation_scope",
        ),
    }
    for table, names in tables.items():
        _drop_constraints(table, names)

    old_fks = (
        (
            "fk_context_manifest_items_manifest_id_context_manifests",
            "context_manifest_items",
            "context_manifests",
            ("manifest_id",),
            ("id",),
        ),
        (
            "fk_context_manifest_items_message_revision_id_message_revisions",
            "context_manifest_items",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_context_manifest_items_media_object_id_media_objects",
            "context_manifest_items",
            "media_objects",
            ("media_object_id",),
            ("id",),
        ),
        (
            "fk_context_preview_requests_context_manifest_id_context_4e1c",
            "context_preview_requests",
            "context_manifests",
            ("context_manifest_id",),
            ("id",),
        ),
        (
            "fk_memory_jobs_input_manifest",
            "memory_jobs",
            "memory_input_manifests",
            ("input_manifest_id",),
            ("id",),
        ),
        (
            "fk_model_runs_memory_input_manifest",
            "model_runs",
            "memory_input_manifests",
            ("memory_input_manifest_id",),
            ("id",),
        ),
        (
            "fk_memory_input_manifests_job",
            "memory_input_manifests",
            "memory_jobs",
            ("memory_job_id",),
            ("id",),
        ),
        (
            "fk_memory_manifest_items_manifest",
            "memory_input_manifest_items",
            "memory_input_manifests",
            ("manifest_id",),
            ("id",),
        ),
        (
            "fk_memory_manifest_items_message_revision",
            "memory_input_manifest_items",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_memory_manifest_items_media",
            "memory_input_manifest_items",
            "media_objects",
            ("media_object_id",),
            ("id",),
        ),
        (
            "fk_memory_manifest_items_memory_version",
            "memory_input_manifest_items",
            "memory_versions",
            ("memory_version_id",),
            ("id",),
        ),
        (
            "fk_memory_manifest_items_summary_version",
            "memory_input_manifest_items",
            "summary_versions",
            ("summary_version_id",),
            ("id",),
        ),
        (
            "fk_memory_watermarks_job",
            "memory_watermarks",
            "memory_jobs",
            ("last_succeeded_job_id",),
            ("id",),
        ),
        (
            "fk_memory_versions_model_run",
            "memory_versions",
            "model_runs",
            ("model_run_id",),
            ("id",),
        ),
        ("fk_memory_proposals_job", "memory_proposals", "memory_jobs", ("memory_job_id",), ("id",)),
        (
            "fk_memory_proposals_model_run",
            "memory_proposals",
            "model_runs",
            ("model_run_id",),
            ("id",),
        ),
        (
            "fk_memory_proposals_accepted_version",
            "memory_proposals",
            "memory_versions",
            ("accepted_memory_version_id",),
            ("id",),
        ),
        (
            "fk_memory_proposal_targets_proposal",
            "memory_proposal_targets",
            "memory_proposals",
            ("proposal_id",),
            ("id",),
        ),
        (
            "fk_memory_proposal_evidence_proposal",
            "memory_proposal_evidence",
            "memory_proposals",
            ("proposal_id",),
            ("id",),
        ),
        (
            "fk_memory_proposal_evidence_revision",
            "memory_proposal_evidence",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_memory_proposal_evidence_media",
            "memory_proposal_evidence",
            "media_objects",
            ("media_object_id",),
            ("id",),
        ),
        (
            "fk_memory_evidence_version",
            "memory_evidence",
            "memory_versions",
            ("memory_version_id",),
            ("id",),
        ),
        (
            "fk_memory_evidence_revision",
            "memory_evidence",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_memory_evidence_summary",
            "memory_evidence",
            "summary_versions",
            ("summary_version_id",),
            ("id",),
        ),
        (
            "fk_memory_evidence_other_version",
            "memory_evidence",
            "memory_versions",
            ("other_memory_version_id",),
            ("id",),
        ),
        (
            "fk_memory_evidence_media",
            "memory_evidence",
            "media_objects",
            ("media_object_id",),
            ("id",),
        ),
        (
            "fk_memory_relations_from",
            "memory_relations",
            "memory_versions",
            ("from_version_id",),
            ("id",),
        ),
        (
            "fk_memory_relations_to",
            "memory_relations",
            "memory_versions",
            ("to_version_id",),
            ("id",),
        ),
        (
            "fk_summary_versions_model_run",
            "summary_versions",
            "model_runs",
            ("model_run_id",),
            ("id",),
        ),
        (
            "fk_summary_sources_revision",
            "summary_version_sources",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_summary_sources_prior",
            "summary_version_sources",
            "summary_versions",
            ("prior_summary_version_id",),
            ("id",),
        ),
        (
            "fk_summary_watermarks_version",
            "summary_watermarks",
            "summary_versions",
            ("last_summary_version_id",),
            ("id",),
        ),
        (
            "fk_embedding_records_memory_version",
            "embedding_records",
            "memory_versions",
            ("memory_version_id",),
            ("id",),
        ),
        (
            "fk_embedding_records_summary_version",
            "embedding_records",
            "summary_versions",
            ("summary_version_id",),
            ("id",),
        ),
        (
            "fk_embedding_records_message_revision",
            "embedding_records",
            "message_revisions",
            ("message_revision_id",),
            ("id",),
        ),
        (
            "fk_memory_review_actions_proposal",
            "memory_review_actions",
            "memory_proposals",
            ("proposal_id",),
            ("id",),
        ),
        (
            "fk_memory_review_actions_memory",
            "memory_review_actions",
            "memories",
            ("memory_id",),
            ("id",),
        ),
    )
    for name, table, referred_table, local_columns, remote_columns in old_fks:
        _restore_fk(name, table, referred_table, local_columns, remote_columns)

    for table, name in (
        ("media_objects", "uq_media_objects_id_account"),
        ("model_runs", "uq_model_runs_id_account_role"),
        ("memory_jobs", "uq_memory_jobs_id_account"),
        ("memory_input_manifests", "uq_memory_input_manifests_id_account"),
        ("memory_proposals", "uq_memory_proposals_id_account"),
    ):
        _drop_constraints(table, (name,))
    op.execute(
        """
        ALTER TABLE context_manifest_items DROP COLUMN IF EXISTS account_id;
        ALTER TABLE memory_input_manifest_items DROP COLUMN IF EXISTS account_id;
        ALTER TABLE memory_relations DROP COLUMN IF EXISTS account_id;
        ALTER TABLE embedding_records ALTER COLUMN account_id DROP NOT NULL;
        """
    )
