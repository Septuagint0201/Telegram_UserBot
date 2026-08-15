-- M7 proactive pipeline grants. Run after m1_roles.sql through m6_roles.sql.
DO $$
DECLARE
  proactive_table text;
BEGIN
  FOREACH proactive_table IN ARRAY ARRAY[
    'proactive_policies',
    'proactive_contact_settings',
    'proactive_life_events',
    'proactive_intentions',
    'proactive_relationships',
    'proactive_occurrences',
    'proactive_occurrence_evidence',
    'proactive_candidates',
    'proactive_candidate_memberships',
    'proactive_jobs',
    'proactive_budget_buckets',
    'proactive_budget_reservations',
    'proactive_decisions',
    'proactive_decision_memberships',
    'proactive_state_transitions',
    'proactive_scan_cursors'
  ]
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO telegram_userbot_migrator', proactive_table);
  END LOOP;
END
$$;

-- The worker owns rule materialization, job leases, reservations, decisions, and audit rows.
GRANT SELECT, INSERT, UPDATE ON
  proactive_policies,
  proactive_contact_settings,
  proactive_life_events,
  proactive_intentions,
  proactive_relationships,
  proactive_occurrences,
  proactive_occurrence_evidence,
  proactive_candidates,
  proactive_candidate_memberships,
  proactive_jobs,
  proactive_budget_buckets,
  proactive_budget_reservations,
  proactive_decisions,
  proactive_decision_memberships,
  proactive_state_transitions,
  proactive_scan_cursors
TO telegram_userbot_worker_runtime;

-- Control reads content-free status and creates versioned settings through its command queue.
GRANT SELECT ON
  proactive_policies,
  proactive_contact_settings,
  proactive_occurrences,
  proactive_candidates,
  proactive_candidate_memberships,
  proactive_jobs,
  proactive_budget_buckets,
  proactive_budget_reservations,
  proactive_decisions,
  proactive_decision_memberships,
  proactive_state_transitions,
  proactive_scan_cursors
TO telegram_userbot_control_runtime;
GRANT INSERT ON proactive_policies, proactive_contact_settings
TO telegram_userbot_control_runtime;
REVOKE INSERT, UPDATE, DELETE ON
  proactive_occurrences,
  proactive_occurrence_evidence,
  proactive_candidates,
  proactive_candidate_memberships,
  proactive_jobs,
  proactive_budget_buckets,
  proactive_budget_reservations,
  proactive_decisions,
  proactive_decision_memberships,
  proactive_state_transitions,
  proactive_scan_cursors
FROM telegram_userbot_control_runtime;

GRANT SELECT ON
  proactive_policies,
  proactive_contact_settings,
  proactive_life_events,
  proactive_intentions,
  proactive_relationships,
  proactive_occurrences,
  proactive_occurrence_evidence,
  proactive_candidates,
  proactive_candidate_memberships,
  proactive_jobs,
  proactive_budget_buckets,
  proactive_budget_reservations,
  proactive_decisions,
  proactive_decision_memberships,
  proactive_state_transitions,
  proactive_scan_cursors
TO telegram_userbot_backup;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  proactive_policies,
  proactive_contact_settings,
  proactive_life_events,
  proactive_intentions,
  proactive_relationships,
  proactive_occurrences,
  proactive_occurrence_evidence,
  proactive_candidates,
  proactive_candidate_memberships,
  proactive_jobs,
  proactive_budget_buckets,
  proactive_budget_reservations,
  proactive_decisions,
  proactive_decision_memberships,
  proactive_state_transitions,
  proactive_scan_cursors
TO telegram_userbot_maintenance;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  telegram_userbot_worker_runtime,
  telegram_userbot_control_runtime,
  telegram_userbot_maintenance;
