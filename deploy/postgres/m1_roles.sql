-- M1 least-privilege role baseline. Run only from the one-shot migrator context.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_migrator') THEN
    CREATE ROLE telegram_userbot_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_app_runtime') THEN
    CREATE ROLE telegram_userbot_app_runtime NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_control_runtime') THEN
    CREATE ROLE telegram_userbot_control_runtime NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_worker_runtime') THEN
    CREATE ROLE telegram_userbot_worker_runtime NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_backup') THEN
    CREATE ROLE telegram_userbot_backup NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_maintenance') THEN
    CREATE ROLE telegram_userbot_maintenance NOLOGIN;
  END IF;
END
$$;

ALTER SCHEMA public OWNER TO telegram_userbot_migrator;
DO $$
DECLARE
  durable_table record;
BEGIN
  FOR durable_table IN
    SELECT schemaname, tablename
    FROM pg_tables
    WHERE schemaname = 'public'
  LOOP
    EXECUTE format(
      'ALTER TABLE %I.%I OWNER TO telegram_userbot_migrator',
      durable_table.schemaname,
      durable_table.tablename
    );
  END LOOP;
END
$$;
ALTER FUNCTION enforce_message_revision_immutability()
  OWNER TO telegram_userbot_migrator;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_backup,
  telegram_userbot_maintenance;
GRANT USAGE, CREATE ON SCHEMA public TO telegram_userbot_migrator;

GRANT SELECT, INSERT, UPDATE ON
  accounts, telegram_peers, account_peers, contacts, conversations,
  message_events, messages, message_revisions, media_objects, message_media,
  message_reactions, conversation_turns, transactional_outbox
TO telegram_userbot_app_runtime;

-- Control may enqueue durable work, but app remains the owner of orchestrator state.
GRANT INSERT ON transactional_outbox
TO telegram_userbot_control_runtime;

GRANT SELECT, INSERT, UPDATE ON
  background_jobs, transactional_outbox, data_erasure_requests, erasure_progress
TO telegram_userbot_worker_runtime;

GRANT SELECT, INSERT ON audit_log TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;
REVOKE UPDATE, DELETE ON audit_log FROM
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO telegram_userbot_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO telegram_userbot_maintenance;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_maintenance;
