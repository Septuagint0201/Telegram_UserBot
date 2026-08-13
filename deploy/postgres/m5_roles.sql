-- M5 media/context grants. Run after m1_roles.sql through m4_roles.sql.
DO $$
DECLARE
  context_table text;
BEGIN
  FOREACH context_table IN ARRAY ARRAY[
    'context_policies',
    'context_policy_versions',
    'retrieval_policies',
    'retrieval_policy_versions',
    'context_manifests',
    'context_manifest_items',
    'context_manifest_item_reasons',
    'context_manifest_omissions',
    'context_preview_requests',
    'context_preview_tokens',
    'context_preview_deliveries'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', context_table);
  END LOOP;
END
$$;

ALTER FUNCTION public.context_preview_sources(uuid,bigint,bigint,text)
  OWNER TO telegram_userbot_migrator;
REVOKE ALL ON FUNCTION public.context_preview_sources(uuid,bigint,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.context_preview_sources(uuid,bigint,bigint,text)
TO telegram_userbot_control_runtime;

GRANT SELECT, INSERT ON
  context_manifests,
  context_manifest_items,
  context_manifest_item_reasons,
  context_manifest_omissions
TO telegram_userbot_app_runtime;

GRANT SELECT ON
  context_policies,
  context_policy_versions,
  retrieval_policies,
  retrieval_policy_versions
TO telegram_userbot_app_runtime;

-- Control can see only content-free manifest projections and owns preview state.
GRANT SELECT ON context_manifests, context_manifest_items, context_manifest_omissions
TO telegram_userbot_control_runtime;
REVOKE SELECT ON message_revisions, media_objects, message_media
FROM telegram_userbot_control_runtime;
GRANT SELECT, INSERT, UPDATE ON
  context_preview_requests,
  context_preview_tokens,
  context_preview_deliveries
TO telegram_userbot_control_runtime;

GRANT SELECT ON
  context_manifests,
  context_manifest_items,
  context_manifest_item_reasons,
  context_manifest_omissions
TO telegram_userbot_worker_runtime;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_maintenance;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO telegram_userbot_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO telegram_userbot_maintenance;
