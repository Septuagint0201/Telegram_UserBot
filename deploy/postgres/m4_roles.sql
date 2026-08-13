-- M4 conversation orchestrator grants. Run after m1_roles.sql through m3_roles.sql.
DO $$
DECLARE
  orchestrator_table text;
BEGIN
  FOREACH orchestrator_table IN ARRAY ARRAY[
    'turn_messages',
    'model_runs',
    'model_run_attempts',
    'turn_grace_authorizations',
    'turn_grace_events',
    'operational_blocks',
    'copilot_drafts',
    'copilot_draft_revisions',
    'copilot_action_tokens',
    'copilot_edit_sessions',
    'control_commands'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', orchestrator_table);
  END LOOP;
END
$$;

ALTER FUNCTION enforce_copilot_revision_redaction()
  OWNER TO telegram_userbot_migrator;

GRANT SELECT, INSERT, UPDATE ON
  turn_messages,
  model_runs,
  model_run_attempts,
  turn_grace_authorizations,
  turn_grace_events,
  operational_blocks,
  copilot_drafts,
  copilot_draft_revisions,
  copilot_action_tokens,
  copilot_edit_sessions,
  control_commands
TO telegram_userbot_app_runtime;

GRANT SELECT, INSERT, UPDATE ON
  account_orchestrator_states,
  conversation_mode_history,
  account_control_history
TO telegram_userbot_app_runtime;

-- Control authenticates and enqueues commands; only app owns orchestration mutations.
REVOKE INSERT, UPDATE, DELETE ON
  account_orchestrator_states,
  conversation_mode_history,
  account_control_history
FROM telegram_userbot_control_runtime;

REVOKE UPDATE, DELETE ON transactional_outbox
FROM telegram_userbot_control_runtime;

GRANT SELECT, INSERT ON
  control_commands
TO telegram_userbot_control_runtime;

REVOKE UPDATE, DELETE ON control_commands
FROM telegram_userbot_control_runtime;

REVOKE INSERT, UPDATE, DELETE ON
  operational_blocks,
  copilot_action_tokens,
  copilot_edit_sessions
FROM telegram_userbot_control_runtime;

REVOKE SELECT ON
  contacts,
  conversations,
  conversation_turns,
  copilot_drafts,
  copilot_draft_revisions
FROM telegram_userbot_control_runtime;

GRANT SELECT ON
  conversation_turns,
  turn_messages,
  model_runs,
  model_run_attempts,
  turn_grace_authorizations,
  turn_grace_events,
  operational_blocks,
  copilot_drafts,
  copilot_draft_revisions,
  copilot_action_tokens,
  copilot_edit_sessions,
  control_commands
TO telegram_userbot_worker_runtime;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_maintenance;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO telegram_userbot_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO telegram_userbot_maintenance;
