"""Bind remaining account-owned references to their durable scope."""

# ruff: noqa: S608 - identifiers are fixed migration constants

from collections.abc import Sequence

from alembic import op

revision: str = "0010_account_scope_refs"
down_revision: str | Sequence[str] | None = "0009_m5_m6_account_scope"
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
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM message_revisions r
            LEFT JOIN message_events e ON e.id = r.source_event_id
            WHERE e.id IS NULL OR e.account_id IS DISTINCT FROM r.account_id
          ) THEN
            RAISE EXCEPTION 'message revision source event scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM media_objects child
            LEFT JOIN media_objects parent ON parent.id = child.parent_object_id
            WHERE child.parent_object_id IS NOT NULL
              AND (parent.id IS NULL OR parent.account_id IS DISTINCT FROM child.account_id)
          ) THEN
            RAISE EXCEPTION 'media parent scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM message_media mm
            LEFT JOIN media_objects mo ON mo.id = mm.media_object_id
            WHERE mm.media_object_id IS NOT NULL
              AND (mo.id IS NULL OR mo.account_id IS DISTINCT FROM mm.account_id)
          ) THEN
            RAISE EXCEPTION 'message media object scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM conversation_turns current_turn
            LEFT JOIN conversation_turns prior
              ON prior.id = current_turn.supersedes_turn_id
            WHERE current_turn.supersedes_turn_id IS NOT NULL
              AND (prior.id IS NULL
                   OR prior.account_id IS DISTINCT FROM current_turn.account_id
                   OR prior.conversation_id IS DISTINCT FROM current_turn.conversation_id)
          ) THEN
            RAISE EXCEPTION 'superseded turn scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM turn_messages tm
            LEFT JOIN message_events e ON e.id = tm.source_event_id
            WHERE e.id IS NULL
               OR e.account_id IS DISTINCT FROM tm.account_id
               OR e.conversation_id IS DISTINCT FROM tm.conversation_id
          ) THEN
            RAISE EXCEPTION 'turn source event scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM context_manifests m
            LEFT JOIN background_jobs j ON j.id = m.background_job_id
            WHERE m.background_job_id IS NOT NULL
              AND (j.id IS NULL OR j.account_id IS DISTINCT FROM m.account_id)
          ) THEN
            RAISE EXCEPTION 'context manifest background job scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM context_preview_requests r
            LEFT JOIN control_commands c ON c.id = r.control_command_id
            WHERE r.control_command_id IS NOT NULL
              AND (c.id IS NULL OR c.account_id IS DISTINCT FROM r.account_id)
          ) THEN
            RAISE EXCEPTION 'context preview control command scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memory_jobs m
            LEFT JOIN background_jobs j ON j.id = m.background_job_id
            WHERE m.background_job_id IS NOT NULL
              AND (j.id IS NULL OR j.account_id IS DISTINCT FROM m.account_id)
          ) THEN
            RAISE EXCEPTION 'memory background job scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM embedding_records r
            LEFT JOIN embedding_spaces s ON s.id = r.embedding_space_id
            WHERE s.id IS NULL
               OR s.account_id IS DISTINCT FROM r.account_id
               OR s.dimensions <> r.dimensions
          ) THEN
            RAISE EXCEPTION 'embedding space scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM memories m
            LEFT JOIN memory_versions v
              ON v.memory_id = m.id AND v.version_no = m.current_version_no
            WHERE v.id IS NULL OR v.account_id IS DISTINCT FROM m.account_id
          ) THEN
            RAISE EXCEPTION 'memory current version scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM summaries s
            LEFT JOIN summary_versions v
              ON v.summary_id = s.id AND v.version_no = s.current_version_no
            WHERE v.id IS NULL OR v.account_id IS DISTINCT FROM s.account_id
          ) THEN
            RAISE EXCEPTION 'summary current version scope is inconsistent';
          END IF;
        END
        $$;
        """
    )


def _create_scope_constraints() -> None:
    old_constraints = {
        "message_revisions": (
            "fk_message_revisions_source_event",
            "fk_message_revisions_source_event_scope",
        ),
        "media_objects": (
            "fk_media_objects_parent_object_id_media_objects",
            "fk_media_objects_parent_scope",
        ),
        "message_media": (
            "fk_message_media_media_object_id_media_objects",
            "fk_message_media_object_scope",
        ),
        "conversation_turns": (
            "fk_conversation_turns_supersedes_turn_id_conversation_turns",
            "fk_conversation_turns_supersedes_scope",
        ),
        "turn_messages": (
            "fk_turn_messages_source_event",
            "fk_turn_messages_source_event_scope",
        ),
        "context_manifests": (
            "fk_context_manifests_job",
            "fk_context_manifests_job_scope",
        ),
        "context_preview_requests": (
            "fk_context_preview_requests_control_command_id_control_commands",
            "fk_context_preview_requests_control_scope",
        ),
        "memory_jobs": (
            "fk_memory_jobs_background_job",
            "fk_memory_jobs_background_job_scope",
        ),
        "embedding_records": (
            "fk_embedding_records_space_dimensions",
            "fk_embedding_records_space_scope",
        ),
        "memories": ("fk_memories_current_version",),
        "summaries": ("fk_summaries_current_version",),
    }
    for table, names in old_constraints.items():
        _drop_constraints(table, names)

    unique_constraints = (
        ("message_events", "uq_message_events_id_account", ("id", "account_id")),
        (
            "message_events",
            "uq_message_events_full_scope",
            ("id", "account_id", "conversation_id"),
        ),
        ("background_jobs", "uq_background_jobs_id_account", ("id", "account_id")),
        ("control_commands", "uq_control_commands_id_account", ("id", "account_id")),
        (
            "embedding_spaces",
            "uq_embedding_spaces_account_dimensions",
            ("id", "account_id", "dimensions"),
        ),
        (
            "memory_versions",
            "uq_memory_versions_account_no",
            ("memory_id", "account_id", "version_no"),
        ),
        (
            "summary_versions",
            "uq_summary_versions_account_no",
            ("summary_id", "account_id", "version_no"),
        ),
    )
    for table, name, columns in unique_constraints:
        _create_unique(table, name, columns)

    foreign_keys = (
        (
            "message_revisions",
            "fk_message_revisions_source_event_scope",
            "message_events",
            ("source_event_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "media_objects",
            "fk_media_objects_parent_scope",
            "media_objects",
            ("parent_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "message_media",
            "fk_message_media_object_scope",
            "media_objects",
            ("media_object_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "conversation_turns",
            "fk_conversation_turns_supersedes_scope",
            "conversation_turns",
            ("supersedes_turn_id", "account_id", "conversation_id"),
            ("id", "account_id", "conversation_id"),
        ),
        (
            "turn_messages",
            "fk_turn_messages_source_event_scope",
            "message_events",
            ("source_event_id", "account_id", "conversation_id"),
            ("id", "account_id", "conversation_id"),
        ),
        (
            "context_manifests",
            "fk_context_manifests_job_scope",
            "background_jobs",
            ("background_job_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "context_preview_requests",
            "fk_context_preview_requests_control_scope",
            "control_commands",
            ("control_command_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "memory_jobs",
            "fk_memory_jobs_background_job_scope",
            "background_jobs",
            ("background_job_id", "account_id"),
            ("id", "account_id"),
        ),
        (
            "embedding_records",
            "fk_embedding_records_space_scope",
            "embedding_spaces",
            ("embedding_space_id", "account_id", "dimensions"),
            ("id", "account_id", "dimensions"),
        ),
        (
            "memories",
            "fk_memories_current_version",
            "memory_versions",
            ("id", "account_id", "current_version_no"),
            ("memory_id", "account_id", "version_no"),
        ),
        (
            "summaries",
            "fk_summaries_current_version",
            "summary_versions",
            ("id", "account_id", "current_version_no"),
            ("summary_id", "account_id", "version_no"),
        ),
    )
    for table, name, referred_table, local_columns, remote_columns in foreign_keys:
        kwargs = (
            {"deferrable": True, "initially": "DEFERRED"}
            if name in {"fk_memories_current_version", "fk_summaries_current_version"}
            else {}
        )
        op.create_foreign_key(
            name,
            table,
            referred_table,
            list(local_columns),
            list(remote_columns),
            **kwargs,
        )


def upgrade() -> None:
    _ensure_scope_data()
    _create_scope_constraints()


def downgrade() -> None:
    scope_constraints = {
        "message_revisions": ("fk_message_revisions_source_event_scope",),
        "media_objects": ("fk_media_objects_parent_scope",),
        "message_media": ("fk_message_media_object_scope",),
        "conversation_turns": ("fk_conversation_turns_supersedes_scope",),
        "turn_messages": ("fk_turn_messages_source_event_scope",),
        "context_manifests": ("fk_context_manifests_job_scope",),
        "context_preview_requests": ("fk_context_preview_requests_control_scope",),
        "memory_jobs": ("fk_memory_jobs_background_job_scope",),
        "embedding_records": ("fk_embedding_records_space_scope",),
        "memories": ("fk_memories_current_version",),
        "summaries": ("fk_summaries_current_version",),
    }
    for table, names in scope_constraints.items():
        _drop_constraints(table, names)

    old_fks = (
        (
            "fk_message_revisions_source_event",
            "message_revisions",
            "message_events",
            ("source_event_id",),
            ("id",),
        ),
        (
            "fk_media_objects_parent_object_id_media_objects",
            "media_objects",
            "media_objects",
            ("parent_object_id",),
            ("id",),
        ),
        (
            "fk_message_media_media_object_id_media_objects",
            "message_media",
            "media_objects",
            ("media_object_id",),
            ("id",),
        ),
        (
            "fk_conversation_turns_supersedes_turn_id_conversation_turns",
            "conversation_turns",
            "conversation_turns",
            ("supersedes_turn_id",),
            ("id",),
        ),
        (
            "fk_turn_messages_source_event",
            "turn_messages",
            "message_events",
            ("source_event_id",),
            ("id",),
        ),
        (
            "fk_context_manifests_job",
            "context_manifests",
            "background_jobs",
            ("background_job_id",),
            ("id",),
        ),
        (
            "fk_context_preview_requests_control_command_id_control_commands",
            "context_preview_requests",
            "control_commands",
            ("control_command_id",),
            ("id",),
        ),
        (
            "fk_memory_jobs_background_job",
            "memory_jobs",
            "background_jobs",
            ("background_job_id",),
            ("id",),
        ),
        (
            "fk_embedding_records_space_dimensions",
            "embedding_records",
            "embedding_spaces",
            ("embedding_space_id", "dimensions"),
            ("id", "dimensions"),
        ),
        (
            "fk_memories_current_version",
            "memories",
            "memory_versions",
            ("id", "current_version_no"),
            ("memory_id", "version_no"),
        ),
        (
            "fk_summaries_current_version",
            "summaries",
            "summary_versions",
            ("id", "current_version_no"),
            ("summary_id", "version_no"),
        ),
    )
    for name, table, referred_table, local_columns, remote_columns in old_fks:
        kwargs = (
            {"deferrable": True, "initially": "DEFERRED"}
            if name in {"fk_memories_current_version", "fk_summaries_current_version"}
            else {}
        )
        op.create_foreign_key(
            name,
            table,
            referred_table,
            list(local_columns),
            list(remote_columns),
            **kwargs,
        )

    # Candidate keys are redundant with global ids and intentionally remain so
    # current metadata can re-upgrade from any intermediate migration revision.
