-- M3 Telegram lifecycle grants. Run after m1_roles.sql and m2_roles.sql.
DO $$
DECLARE
  lifecycle_table text;
BEGIN
  FOREACH lifecycle_table IN ARRAY ARRAY[
    'outbound_delivery_groups',
    'outbound_intents',
    'outbound_attempts',
    'telegram_read_states',
    'telegram_typing_states',
    'telegram_operations'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', lifecycle_table);
  END LOOP;
END
$$;

ALTER FUNCTION enforce_outbound_attempt_completion()
  OWNER TO telegram_userbot_migrator;

GRANT SELECT, INSERT, UPDATE ON
  outbound_delivery_groups,
  outbound_intents,
  telegram_read_states,
  telegram_typing_states,
  telegram_operations
TO telegram_userbot_app_runtime;

GRANT SELECT, INSERT, UPDATE ON outbound_attempts TO telegram_userbot_app_runtime;

-- Workers may inspect recovery state and enqueue maintenance, but never hold the
-- Telethon Session or mutate send state directly.
GRANT SELECT ON
  outbound_delivery_groups,
  outbound_intents,
  outbound_attempts,
  telegram_read_states,
  telegram_typing_states,
  telegram_operations
TO telegram_userbot_worker_runtime;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_app_runtime,
  telegram_userbot_maintenance;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO telegram_userbot_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO telegram_userbot_maintenance;
