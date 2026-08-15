-- M6 memory/summary/embedding grants. Run after m1_roles.sql through m5_roles.sql.
DO $$
DECLARE
  memory_table text;
BEGIN
  FOREACH memory_table IN ARRAY ARRAY[
    'memory_jobs',
    'memory_input_manifests',
    'memory_input_manifest_items',
    'memory_watermarks',
    'memories',
    'memory_versions',
    'memory_proposals',
    'memory_proposal_targets',
    'memory_proposal_evidence',
    'memory_evidence',
    'memory_relations',
    'summaries',
    'summary_versions',
    'summary_version_sources',
    'summary_watermarks',
    'embedding_spaces',
    'embedding_records',
    'memory_review_actions'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', memory_table);
  END LOOP;
END
$$;

GRANT SELECT, INSERT, UPDATE ON
  memory_jobs,
  memory_input_manifests,
  memory_input_manifest_items,
  memory_watermarks,
  memories,
  memory_versions,
  memory_proposals,
  memory_proposal_targets,
  memory_proposal_evidence,
  memory_evidence,
  memory_relations,
  summaries,
  summary_versions,
  summary_version_sources,
  summary_watermarks,
  embedding_spaces,
  embedding_records,
  memory_review_actions
TO telegram_userbot_worker_runtime;

-- Control may create review requests, but cannot mutate derived truth directly.
GRANT SELECT ON
  memories,
  memory_versions,
  memory_proposals,
  memory_proposal_evidence,
  summary_watermarks,
  embedding_spaces,
  memory_jobs,
  memory_review_actions
TO telegram_userbot_control_runtime;
GRANT INSERT, UPDATE ON memory_review_actions TO telegram_userbot_control_runtime;
REVOKE INSERT, UPDATE, DELETE ON
  memories,
  memory_versions,
  memory_proposals,
  memory_evidence,
  summary_versions,
  embedding_records
FROM telegram_userbot_control_runtime;

GRANT SELECT ON
  memory_jobs,
  memory_input_manifests,
  memory_input_manifest_items,
  memory_watermarks,
  memories,
  memory_versions,
  memory_proposals,
  memory_proposal_targets,
  memory_proposal_evidence,
  memory_evidence,
  memory_relations,
  summaries,
  summary_versions,
  summary_version_sources,
  summary_watermarks,
  embedding_spaces,
  embedding_records,
  memory_review_actions
TO telegram_userbot_backup;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  memory_jobs,
  memory_input_manifests,
  memory_input_manifest_items,
  memory_watermarks,
  memories,
  memory_versions,
  memory_proposals,
  memory_proposal_targets,
  memory_proposal_evidence,
  memory_evidence,
  memory_relations,
  summaries,
  summary_versions,
  summary_version_sources,
  summary_watermarks,
  embedding_spaces,
  embedding_records,
  memory_review_actions
TO telegram_userbot_maintenance;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_worker_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_maintenance;
