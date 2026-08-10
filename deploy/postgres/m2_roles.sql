-- M2 least-privilege grants. Run after m1_roles.sql from the migrator context.
DO $$
DECLARE
  model_table text;
BEGIN
  FOREACH model_table IN ARRAY ARRAY[
    'model_endpoints',
    'model_profiles',
    'model_credentials',
    'model_credential_versions',
    'model_capability_snapshots',
    'model_config_versions',
    'model_config_drafts',
    'prompt_versions',
    'control_input_sessions',
    'model_key_launch_sessions',
    'model_key_rate_limits'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', model_table);
  END LOOP;
END
$$;

ALTER FUNCTION reject_immutable_model_row_change()
  OWNER TO telegram_userbot_migrator;
ALTER FUNCTION enforce_credential_version_destruction()
  OWNER TO telegram_userbot_migrator;

CREATE OR REPLACE FUNCTION get_model_credential_version(
  requested_profile_id uuid,
  requested_version_no integer
)
RETURNS TABLE (
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
  WHERE value.profile_id = requested_profile_id
    AND value.version_no = requested_version_no
    AND value.destroyed_at IS NULL
$$;

ALTER FUNCTION get_model_credential_version(uuid, integer)
  OWNER TO telegram_userbot_migrator;
REVOKE ALL ON FUNCTION get_model_credential_version(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION get_model_credential_version(uuid, integer) TO
  telegram_userbot_app_runtime,
  telegram_userbot_worker_runtime;

GRANT SELECT ON
  model_endpoints,
  model_profiles,
  model_credentials,
  model_capability_snapshots,
  model_config_versions,
  prompt_versions
TO
  telegram_userbot_app_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_control_runtime;

GRANT SELECT, INSERT, UPDATE ON
  model_endpoints,
  model_profiles,
  model_credentials,
  model_credential_versions,
  model_capability_snapshots,
  model_config_drafts,
  control_input_sessions,
  model_key_launch_sessions,
  model_key_rate_limits
TO telegram_userbot_control_runtime;

GRANT SELECT, INSERT ON
  model_config_versions,
  prompt_versions
TO telegram_userbot_control_runtime;

REVOKE SELECT, INSERT, UPDATE, DELETE ON
  model_credential_versions,
  model_config_drafts,
  control_input_sessions,
  model_key_launch_sessions,
  model_key_rate_limits
FROM
  telegram_userbot_app_runtime,
  telegram_userbot_worker_runtime;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO telegram_userbot_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO telegram_userbot_maintenance;
