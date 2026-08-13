"""Write content-free M4 race evidence derived from actual JUnit outcomes."""

import argparse
import json
from pathlib import Path

from telegram_userbot.platform.evidence import evidence_status, load_junit_results

CASE_TESTS: dict[str, tuple[str, ...]] = {
    "mode_flip_before_materialization": (
        "tests.unit.domain.test_conversation::test_final_gate_fails_closed_for_every_stale_dimension",
    ),
    "mode_flip_before_telegram_rpc": (
        "tests.unit.domain.test_conversation::test_final_gate_fails_closed_for_every_stale_dimension",
    ),
    "incoming_completed_within_three_second_grace": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_generation_grace_and_late_result_discard",
    ),
    "incoming_after_three_second_grace": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_generation_grace_and_late_result_discard",
    ),
    "edit_or_delete_invalidates_pre_send_work": (
        "tests.unit.platform.test_conversation_runtime::"
        "test_orchestrated_ingest_routes_all_event_classes",
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[edit]",
    ),
    "human_outgoing_takeover": (
        "tests.unit.platform.test_conversation_runtime::"
        "test_orchestrated_ingest_routes_all_event_classes",
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[human]",
    ),
    "provider_timeout": (
        "tests.unit.platform.test_conversation_runtime::"
        "test_runtime_auto_success_and_provider_failure",
    ),
    "duplicate_worker_lease": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_auto_turn_run_send_gate_and_reconciliation",
        "tests.unit.adapters.persistence.test_orchestrator_repository::"
        "test_generation_lease_renewal_obeys_owner_mode_and_run_state",
    ),
    "late_model_result_discard": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_generation_grace_and_late_result_discard",
    ),
    "copilot_action_token_replay": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_mode_flip_blocks_auto_and_copilot_tokens_are_one_time",
    ),
    "copilot_revision_bound_approval": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_mode_flip_blocks_auto_and_copilot_tokens_are_one_time",
    ),
    "crash_after_durable_intent": (
        "tests.unit.adapters.persistence.test_telegram_repository::"
        "test_claim_finish_recover_and_get_intent_state_paths",
    ),
    "deterministic_telegram_splitter": (
        "tests.unit.domain.test_conversation::"
        "test_splitter_is_deterministic_and_respects_combining_boundaries",
    ),
    "manual_modes_have_no_read_or_typing": (
        "tests.unit.platform.test_conversation_runtime::"
        "test_runtime_auto_success_and_provider_failure",
    ),
    "multichunk_continuation_gate": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[edit]",
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[human]",
    ),
    "durable_control_command_replay": (
        "tests.integration.test_m4_conversation_orchestrator::"
        "test_queued_conversation_control_executes_reply_pending_once",
    ),
}
CASES = tuple(CASE_TESTS)


def build_manifest(junit_path: Path) -> dict[str, object]:
    results = load_junit_results(junit_path)
    cases: list[dict[str, object]] = []
    for name, test_ids in CASE_TESTS.items():
        status, reason = evidence_status(results, test_ids)
        case: dict[str, object] = {"name": name, "status": status, "tests": list(test_ids)}
        if reason is not None:
            case["reason"] = reason
        cases.append(case)
    return {
        "schema_version": 2,
        "fixture": "pytest fakes plus disposable PostgreSQL and Redis",
        "external_network_access": False,
        "credential_access": False,
        "message_bodies_in_evidence": False,
        "grace_seconds": 3,
        "provider_timeout_is_distinct": True,
        "junit_artifact": junit_path.as_posix(),
        "cases": cases,
        "live_runtime": {
            "status": "NOT RUN",
            "reason": "M4 uses deterministic fakes and disposable services only",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    document = build_manifest(args.junit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cases = document["cases"]
    assert isinstance(cases, list)
    failed = [case for case in cases if isinstance(case, dict) and case["status"] != "PASS"]
    if failed:
        print(f"FAIL wrote {args.output.as_posix()} with {len(failed)} non-PASS cases")
        return 1
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
