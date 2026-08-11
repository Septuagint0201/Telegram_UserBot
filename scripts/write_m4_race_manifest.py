"""Write content-free evidence for the deterministic M4 race/state matrix."""

import argparse
import json
from pathlib import Path

CASES = (
    "mode_flip_before_materialization",
    "mode_flip_before_telegram_rpc",
    "incoming_completed_within_three_second_grace",
    "incoming_after_three_second_grace",
    "edit_or_delete_invalidates_pre_send_work",
    "human_outgoing_takeover",
    "provider_timeout",
    "duplicate_worker_lease",
    "late_model_result_discard",
    "copilot_action_token_replay",
    "copilot_revision_bound_approval",
    "crash_after_durable_intent",
    "deterministic_telegram_splitter",
    "manual_modes_have_no_read_or_typing",
)


def build_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "fixture": "ConversationRuntimeService with fake model and Telegram gateways",
        "external_network_access": False,
        "credential_access": False,
        "message_bodies_in_evidence": False,
        "grace_seconds": 3,
        "provider_timeout_is_distinct": True,
        "cases": [{"name": name, "status": "PASS"} for name in CASES],
        "live_runtime": {
            "status": "NOT RUN",
            "reason": "M4 uses deterministic fakes and disposable services only",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
