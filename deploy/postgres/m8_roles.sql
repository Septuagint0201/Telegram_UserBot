-- M8 production status and restore-gate grants. Run after m1_roles.sql through m7_roles.sql.
ALTER FUNCTION public.redact_scope_retention(uuid,timestamptz) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.erasure_live_media(uuid) OWNER TO telegram_userbot_migrator;
GRANT EXECUTE ON FUNCTION public.erasure_live_media(uuid) TO telegram_userbot_app_runtime;
GRANT EXECUTE ON FUNCTION public.redact_scope_retention(uuid,timestamptz)
  TO telegram_userbot_worker_runtime;
ALTER TABLE erasure_media_checks OWNER TO telegram_userbot_migrator;
ALTER TABLE erasure_restore_replays OWNER TO telegram_userbot_migrator;
GRANT SELECT, INSERT ON erasure_media_checks TO telegram_userbot_app_runtime;
GRANT SELECT ON erasure_media_checks TO telegram_userbot_worker_runtime;
GRANT SELECT, INSERT ON erasure_ledger TO telegram_userbot_worker_runtime;
GRANT SELECT ON erasure_progress TO telegram_userbot_app_runtime;
GRANT SELECT (id, account_id, scope_type, contact_id, state) ON data_erasure_requests
  TO telegram_userbot_app_runtime;
GRANT SELECT ON erasure_media_checks, erasure_restore_replays TO telegram_userbot_backup;
ALTER FUNCTION public.enforce_media_upload_erasure() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_media_reference_erasure() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.scope_metadata_owner(text,jsonb) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.scope_metadata_cleanup_notice(text,jsonb) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.scope_metadata_blocked(text,jsonb) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.scope_metadata_matches(text,jsonb,uuid,uuid) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_metadata_erasure() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_metadata_deleted_row() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.redact_scope_metadata(uuid,timestamptz) OWNER TO telegram_userbot_migrator;
GRANT EXECUTE ON FUNCTION public.redact_scope_metadata(uuid,timestamptz)
  TO telegram_userbot_worker_runtime;
GRANT EXECUTE ON FUNCTION public.scope_metadata_blocked(text,jsonb)
  TO telegram_userbot_app_runtime;
ALTER FUNCTION public.export_scope_erasure_blocked(jsonb) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_export_erasure() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.serialize_scope_erasure_intent() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.revoke_scope_exports() OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_budget_erasure() OWNER TO telegram_userbot_migrator;
GRANT SELECT ON data_export_requests TO telegram_userbot_worker_runtime;
DROP POLICY IF EXISTS data_export_requests_worker ON data_export_requests;
CREATE POLICY data_export_requests_worker ON data_export_requests
  FOR SELECT TO telegram_userbot_worker_runtime USING (true);
ALTER FUNCTION public.scope_erasure_row_blocked(text, jsonb) OWNER TO telegram_userbot_migrator;
ALTER FUNCTION public.enforce_scope_payload_erasure() OWNER TO telegram_userbot_migrator;
REVOKE ALL ON FUNCTION public.scope_erasure_row_blocked(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enforce_scope_payload_erasure() FROM PUBLIC;
GRANT DELETE ON embedding_records TO telegram_userbot_worker_runtime;
GRANT UPDATE (content_text, content_sha256, redacted_at, scope_erased_at)
ON copilot_draft_revisions TO telegram_userbot_worker_runtime;
GRANT UPDATE (state, terminal_at, terminal_reason) ON copilot_drafts TO telegram_userbot_worker_runtime;
GRANT SELECT ON outbound_intents, outbound_delivery_groups TO telegram_userbot_worker_runtime;
GRANT UPDATE (text_content, payload_sha256, scope_erased_at, state, next_attempt_at,
              last_error_code, updated_at) ON outbound_intents TO telegram_userbot_worker_runtime;
GRANT UPDATE (logical_content_sha256, scope_erased_at, state, updated_at)
ON outbound_delivery_groups TO telegram_userbot_worker_runtime;
GRANT UPDATE (source_revision_vector_sha256, manifest_sha256, scope_erased_at)
ON context_manifests TO telegram_userbot_worker_runtime;
GRANT UPDATE (content_sha256, rendered_part_sha256, score_features, scope_erased_at)
ON context_manifest_items TO telegram_userbot_worker_runtime;
GRANT UPDATE (source_hash, summary, scope_erased_at)
ON proactive_input_manifest_items TO telegram_userbot_worker_runtime;

-- Control only needs scope identity to revoke previews after durable erasure intent.
GRANT SELECT (account_id, scope_type, contact_id) ON data_erasure_requests
TO telegram_userbot_control_runtime;
GRANT SELECT (id, account_id, contact_id) ON conversations
TO telegram_userbot_control_runtime;

-- Scope erasure only adds marker/redaction column writes, with no deployment
-- configuration or credential authority. Revision triggers enforce one-way changes.
GRANT SELECT (id, status, deleted_at) ON accounts TO telegram_userbot_worker_runtime;
-- Calendar summaries read timezone precedence and skip erased/deleted scopes.
GRANT SELECT (default_timezone) ON accounts TO telegram_userbot_worker_runtime;
GRANT SELECT (timezone) ON contacts TO telegram_userbot_worker_runtime;
GRANT SELECT (observed_at) ON message_events TO telegram_userbot_worker_runtime;
GRANT SELECT (deleted_at, metadata_erased_at) ON conversations TO telegram_userbot_worker_runtime;
GRANT UPDATE (status, updated_at) ON accounts TO telegram_userbot_worker_runtime;
GRANT SELECT (id, account_id, automation_status) ON contacts TO telegram_userbot_worker_runtime;
GRANT UPDATE (automation_status, proactive_enabled, updated_at) ON contacts
TO telegram_userbot_worker_runtime;
GRANT SELECT (id, account_id, contact_id, contact_paused, mode_version) ON conversations
TO telegram_userbot_worker_runtime;
GRANT UPDATE (contact_paused, mode_version, updated_at) ON conversations
TO telegram_userbot_worker_runtime;
GRANT UPDATE (is_tombstone, deleted_at) ON messages TO telegram_userbot_worker_runtime;
GRANT SELECT ON messages, message_revisions TO telegram_userbot_worker_runtime;
GRANT UPDATE (text_content, caption, entities, content_sha256, redacted_at, redaction_reason)
ON message_revisions TO telegram_userbot_worker_runtime;
GRANT SELECT ON media_objects, message_media TO telegram_userbot_worker_runtime;
GRANT UPDATE (delete_requested_at, expires_at) ON media_objects TO telegram_userbot_worker_runtime;

-- App-owned physical deletion must be able to prove that no memory job still
-- holds a media reference. These columns contain IDs and state only.
GRANT SELECT (media_object_id, manifest_id, account_id) ON memory_input_manifest_items
TO telegram_userbot_app_runtime;
GRANT SELECT (id, account_id, scope_erased_at) ON memory_input_manifests TO telegram_userbot_app_runtime;
GRANT SELECT (input_manifest_id, account_id, state) ON memory_jobs TO telegram_userbot_app_runtime;

ALTER TABLE public.service_instances OWNER TO telegram_userbot_migrator;
ALTER TABLE public.service_status_events OWNER TO telegram_userbot_migrator;
ALTER TABLE public.control_bot_cursors OWNER TO telegram_userbot_migrator;
ALTER TABLE public.control_bot_update_receipts OWNER TO telegram_userbot_migrator;
ALTER TABLE public.telegram_ingest_watermarks OWNER TO telegram_userbot_migrator;
ALTER TABLE public.deployment_restore_state OWNER TO telegram_userbot_migrator;
ALTER TABLE public.data_export_requests OWNER TO telegram_userbot_migrator;
ALTER TABLE public.proactive_input_manifests OWNER TO telegram_userbot_migrator;
ALTER TABLE public.proactive_input_manifest_items OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_account_peers_v1 OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_message_revisions_v1 OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_message_media_v1 OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_memories_v1 OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_memory_versions_v1 OWNER TO telegram_userbot_migrator;
ALTER VIEW public.export_summary_versions_v1 OWNER TO telegram_userbot_migrator;

-- Runtime identities cannot SELECT the credential-version table.  The narrow
-- accessor is created by the schema migration and returns exactly one undeleted
-- immutable version bound to the currently-running durable model run.
ALTER FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid)
  OWNER TO telegram_userbot_migrator;
REVOKE ALL ON FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid) TO
  telegram_userbot_app_runtime,
  telegram_userbot_worker_runtime;

-- Each runtime publishes its current projection and meaningful transitions. Service
-- scoping is enforced by the repository contract; no runtime may rewrite event history.
GRANT SELECT, INSERT, UPDATE ON service_instances TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;
GRANT SELECT, INSERT ON service_status_events TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;
REVOKE UPDATE, DELETE ON service_status_events FROM
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;

-- The worker alone composes background model generations.  Proactive input
-- manifests are append-only and their trigger permits only the one-way seal;
-- app/control never need their private evidence summaries.
GRANT SELECT, INSERT, UPDATE ON proactive_input_manifests
TO telegram_userbot_worker_runtime;
GRANT SELECT, INSERT ON proactive_input_manifest_items
TO telegram_userbot_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON model_runs, model_run_attempts
TO telegram_userbot_worker_runtime;
REVOKE INSERT, UPDATE, DELETE ON
  proactive_input_manifests,
  proactive_input_manifest_items
FROM telegram_userbot_app_runtime, telegram_userbot_control_runtime;

-- Control owns the Bot API cursor and content-free update receipts. App owns the
-- Telethon ingest watermark. Cross-process writes stay closed even if an adapter
-- accidentally imports the wrong repository.
GRANT SELECT, INSERT, UPDATE ON
  control_bot_cursors,
  control_bot_update_receipts
TO telegram_userbot_control_runtime;
REVOKE INSERT, UPDATE, DELETE ON
  control_bot_cursors,
  control_bot_update_receipts
FROM telegram_userbot_app_runtime, telegram_userbot_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON telegram_ingest_watermarks
TO telegram_userbot_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON telegram_ingest_watermarks
FROM telegram_userbot_control_runtime, telegram_userbot_worker_runtime;

-- Runtime processes can only observe the durable restore gate. Opening/resetting it is
-- an explicit maintenance action after all four verification facts are persisted.
GRANT SELECT ON deployment_restore_state TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;

-- Control may create and observe durable requests, but only the one-shot export
-- identity may claim/finalize them.  The export identity has a fixed SELECT
-- allowlist and cannot read credential ciphertext, control sessions, raw model
-- attempts, Session data, or arbitrary future tables.
GRANT SELECT, INSERT ON data_export_requests TO telegram_userbot_control_runtime;
REVOKE UPDATE, DELETE ON data_export_requests FROM telegram_userbot_control_runtime;
GRANT SELECT, UPDATE ON data_export_requests TO telegram_userbot_export_runtime;
REVOKE INSERT, DELETE ON data_export_requests FROM telegram_userbot_export_runtime;
-- Cumulative deletion exports need only keyed ledger metadata and request identity.
GRANT SELECT (account_scope_hmac, scope_type, target_scope_hmac, request_id,
  policy_version, completed_at)
ON erasure_ledger TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, state, request_idempotency_key)
ON data_erasure_requests TO telegram_userbot_export_runtime;
GRANT SELECT (id, telegram_user_id, display_label, status, default_timezone,
  created_at, updated_at, deleted_at)
ON accounts TO telegram_userbot_export_runtime;
GRANT SELECT (id, peer_type, telegram_peer_id, is_bot, created_at)
ON telegram_peers TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, account_peer_id, automation_status, proactive_enabled,
  timezone, locale, created_at, updated_at, deleted_at)
ON contacts TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, contact_id, account_peer_id, telegram_chat_id,
  base_mode_override, contact_paused, temporary_human_until, last_message_at,
  last_completed_turn_at, created_at, updated_at, deleted_at)
ON conversations TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, conversation_id, telegram_message_id,
  sender_account_peer_id, direction, role, source, source_status, current_revision_no,
  grouped_id, reply_to_telegram_message_id, telegram_created_at, edited_at, deleted_at,
  is_tombstone, first_observed_at, last_observed_at)
ON messages TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, object_kind, status, validated_mime, byte_size, width,
  height, validation_error_code, created_at, ready_at, deleted_at, retention_class,
  expires_at)
ON media_objects TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, conversation_id, summary_kind, period_key,
  timezone_snapshot, period_start_at, period_end_at, status, current_version_no,
  created_at, updated_at)
ON summaries TO telegram_userbot_export_runtime;
GRANT SELECT (id, account_id, candidate_id, from_state, to_state, event, reason, actor,
  created_at)
ON proactive_state_transitions TO telegram_userbot_export_runtime;
GRANT SELECT (id, occurred_at, account_id, actor_type, action, target_type, result,
  reason_code, request_id)
ON audit_log TO telegram_userbot_export_runtime;
GRANT SELECT ON
  export_account_peers_v1,
  export_message_revisions_v1,
  export_message_media_v1,
  export_memories_v1,
  export_memory_versions_v1,
  export_summary_versions_v1
TO telegram_userbot_export_runtime;
REVOKE INSERT, UPDATE, DELETE ON deployment_restore_state FROM
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime;

GRANT SELECT ON
  service_instances,
  service_status_events,
  background_jobs,
  transactional_outbox,
  outbound_delivery_groups,
  outbound_intents,
  outbound_attempts,
  model_run_attempts,
  data_erasure_requests,
  deployment_restore_state,
  data_export_requests
TO telegram_userbot_monitor_runtime;
REVOKE INSERT, UPDATE, DELETE ON
  service_instances,
  service_status_events,
  background_jobs,
  transactional_outbox,
  outbound_delivery_groups,
  outbound_intents,
  outbound_attempts,
  model_run_attempts,
  data_erasure_requests,
  deployment_restore_state,
  data_export_requests
FROM telegram_userbot_monitor_runtime;

GRANT SELECT ON
  service_instances,
  service_status_events,
  control_bot_cursors,
  control_bot_update_receipts,
  telegram_ingest_watermarks,
  deployment_restore_state,
  data_export_requests
TO telegram_userbot_backup;
GRANT SELECT ON proactive_input_manifests, proactive_input_manifest_items
TO telegram_userbot_backup;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  service_instances,
  control_bot_cursors,
  control_bot_update_receipts,
  telegram_ingest_watermarks,
  deployment_restore_state,
  data_export_requests
TO telegram_userbot_maintenance;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  proactive_input_manifests,
  proactive_input_manifest_items
TO telegram_userbot_maintenance;
GRANT SELECT, INSERT, DELETE ON service_status_events
TO telegram_userbot_maintenance;
REVOKE UPDATE ON service_status_events FROM telegram_userbot_maintenance;

GRANT USAGE ON SEQUENCE service_status_events_id_seq TO
  telegram_userbot_app_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_worker_runtime,
  telegram_userbot_maintenance;
GRANT USAGE ON SEQUENCE model_run_attempts_id_seq TO
  telegram_userbot_worker_runtime;

-- Memory input sealing pins an immutable credential UUID before creating its run.
-- Ciphertext remains available only through the existing scoped accessor.
GRANT SELECT (id, profile_id, credential_id, version_no, destroyed_at)
ON model_credential_versions TO telegram_userbot_worker_runtime;
GRANT SELECT (id, account_id, conversation_id, projected_at)
ON message_events TO telegram_userbot_worker_runtime;

-- RLS prevents one runtime identity from publishing another service's status. Control
-- retains the cross-service read needed by /server_status but can write only itself.
DROP POLICY IF EXISTS service_instances_app ON service_instances;
CREATE POLICY service_instances_app ON service_instances
  FOR ALL TO telegram_userbot_app_runtime
  USING (service_name = 'app') WITH CHECK (service_name = 'app');
DROP POLICY IF EXISTS service_instances_control_read ON service_instances;
CREATE POLICY service_instances_control_read ON service_instances
  FOR SELECT TO telegram_userbot_control_runtime USING (true);
DROP POLICY IF EXISTS service_instances_control_write ON service_instances;
CREATE POLICY service_instances_control_write ON service_instances
  FOR INSERT TO telegram_userbot_control_runtime WITH CHECK (service_name = 'control');
DROP POLICY IF EXISTS service_instances_control_update ON service_instances;
CREATE POLICY service_instances_control_update ON service_instances
  FOR UPDATE TO telegram_userbot_control_runtime
  USING (service_name = 'control') WITH CHECK (service_name = 'control');
DROP POLICY IF EXISTS service_instances_worker ON service_instances;
CREATE POLICY service_instances_worker ON service_instances
  FOR ALL TO telegram_userbot_worker_runtime
  USING (service_name = 'worker') WITH CHECK (service_name = 'worker');
DROP POLICY IF EXISTS service_instances_backup ON service_instances;
CREATE POLICY service_instances_backup ON service_instances
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS service_instances_monitor ON service_instances;
CREATE POLICY service_instances_monitor ON service_instances
  FOR SELECT TO telegram_userbot_monitor_runtime USING (true);
DROP POLICY IF EXISTS service_instances_maintenance ON service_instances;
CREATE POLICY service_instances_maintenance ON service_instances
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS service_instances_migrator ON service_instances;
CREATE POLICY service_instances_migrator ON service_instances
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_events_app_read ON service_status_events;
CREATE POLICY service_events_app_read ON service_status_events
  FOR SELECT TO telegram_userbot_app_runtime USING (service_name = 'app');
DROP POLICY IF EXISTS service_events_app_insert ON service_status_events;
CREATE POLICY service_events_app_insert ON service_status_events
  FOR INSERT TO telegram_userbot_app_runtime WITH CHECK (service_name = 'app');
DROP POLICY IF EXISTS service_events_control_read ON service_status_events;
CREATE POLICY service_events_control_read ON service_status_events
  FOR SELECT TO telegram_userbot_control_runtime USING (true);
DROP POLICY IF EXISTS service_events_control_insert ON service_status_events;
CREATE POLICY service_events_control_insert ON service_status_events
  FOR INSERT TO telegram_userbot_control_runtime WITH CHECK (service_name = 'control');
DROP POLICY IF EXISTS service_events_worker_read ON service_status_events;
CREATE POLICY service_events_worker_read ON service_status_events
  FOR SELECT TO telegram_userbot_worker_runtime USING (service_name = 'worker');
DROP POLICY IF EXISTS service_events_worker_insert ON service_status_events;
CREATE POLICY service_events_worker_insert ON service_status_events
  FOR INSERT TO telegram_userbot_worker_runtime WITH CHECK (service_name = 'worker');
DROP POLICY IF EXISTS service_events_backup ON service_status_events;
CREATE POLICY service_events_backup ON service_status_events
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS service_events_monitor ON service_status_events;
CREATE POLICY service_events_monitor ON service_status_events
  FOR SELECT TO telegram_userbot_monitor_runtime USING (true);
DROP POLICY IF EXISTS service_events_maintenance ON service_status_events;
CREATE POLICY service_events_maintenance ON service_status_events
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS service_events_migrator ON service_status_events;
CREATE POLICY service_events_migrator ON service_status_events
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS control_bot_cursors_control ON control_bot_cursors;
CREATE POLICY control_bot_cursors_control ON control_bot_cursors
  FOR ALL TO telegram_userbot_control_runtime USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS control_bot_cursors_backup ON control_bot_cursors;
CREATE POLICY control_bot_cursors_backup ON control_bot_cursors
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS control_bot_cursors_maintenance ON control_bot_cursors;
CREATE POLICY control_bot_cursors_maintenance ON control_bot_cursors
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS control_bot_cursors_migrator ON control_bot_cursors;
CREATE POLICY control_bot_cursors_migrator ON control_bot_cursors
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS control_bot_receipts_control ON control_bot_update_receipts;
CREATE POLICY control_bot_receipts_control ON control_bot_update_receipts
  FOR ALL TO telegram_userbot_control_runtime USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS control_bot_receipts_backup ON control_bot_update_receipts;
CREATE POLICY control_bot_receipts_backup ON control_bot_update_receipts
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS control_bot_receipts_maintenance ON control_bot_update_receipts;
CREATE POLICY control_bot_receipts_maintenance ON control_bot_update_receipts
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS control_bot_receipts_migrator ON control_bot_update_receipts;
CREATE POLICY control_bot_receipts_migrator ON control_bot_update_receipts
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS telegram_watermarks_app ON telegram_ingest_watermarks;
CREATE POLICY telegram_watermarks_app ON telegram_ingest_watermarks
  FOR ALL TO telegram_userbot_app_runtime USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS telegram_watermarks_backup ON telegram_ingest_watermarks;
CREATE POLICY telegram_watermarks_backup ON telegram_ingest_watermarks
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS telegram_watermarks_maintenance ON telegram_ingest_watermarks;
CREATE POLICY telegram_watermarks_maintenance ON telegram_ingest_watermarks
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS telegram_watermarks_migrator ON telegram_ingest_watermarks;
CREATE POLICY telegram_watermarks_migrator ON telegram_ingest_watermarks
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS data_export_requests_control_read ON data_export_requests;
CREATE POLICY data_export_requests_control_read ON data_export_requests
  FOR SELECT TO telegram_userbot_control_runtime USING (true);
DROP POLICY IF EXISTS data_export_requests_control_insert ON data_export_requests;
CREATE POLICY data_export_requests_control_insert ON data_export_requests
  FOR INSERT TO telegram_userbot_control_runtime
  WITH CHECK (state = 'requested' AND attempt_count = 0 AND version = 1);
DROP POLICY IF EXISTS data_export_requests_exporter ON data_export_requests;
CREATE POLICY data_export_requests_exporter ON data_export_requests
  FOR ALL TO telegram_userbot_export_runtime USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS data_export_requests_backup ON data_export_requests;
CREATE POLICY data_export_requests_backup ON data_export_requests
  FOR SELECT TO telegram_userbot_backup USING (true);
DROP POLICY IF EXISTS data_export_requests_monitor ON data_export_requests;
CREATE POLICY data_export_requests_monitor ON data_export_requests
  FOR SELECT TO telegram_userbot_monitor_runtime USING (true);
DROP POLICY IF EXISTS data_export_requests_maintenance ON data_export_requests;
CREATE POLICY data_export_requests_maintenance ON data_export_requests
  FOR ALL TO telegram_userbot_maintenance USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS data_export_requests_migrator ON data_export_requests;
CREATE POLICY data_export_requests_migrator ON data_export_requests
  FOR ALL TO telegram_userbot_migrator USING (true) WITH CHECK (true);

-- Complete Worker output planning; App remains the only Telegram sender.
GRANT SELECT ON accounts, contacts, conversations, account_orchestrator_states, conversation_turns, message_events, outbound_delivery_groups, outbound_intents, copilot_drafts, copilot_draft_revisions TO telegram_userbot_worker_runtime;
GRANT INSERT ON conversation_turns, outbound_delivery_groups, outbound_intents,
  copilot_drafts, copilot_draft_revisions TO telegram_userbot_worker_runtime;
GRANT UPDATE (lease_owner, lease_expires_at, state, terminal_reason, completed_at)
  ON conversation_turns TO telegram_userbot_worker_runtime;
GRANT UPDATE (completed_at) ON outbound_delivery_groups TO telegram_userbot_worker_runtime;

GRANT UPDATE (updated_at) ON account_orchestrator_states, conversations TO telegram_userbot_worker_runtime;

-- Proactive delivery rechecks current formal facts before the App RPC.
GRANT SELECT (id, account_id, conversation_id, status, current_version_no)
ON memories TO telegram_userbot_app_runtime;
