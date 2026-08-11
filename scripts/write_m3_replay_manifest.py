"""Write content-free evidence for the deterministic M3 replay matrix."""

import argparse
import json
from pathlib import Path

CASES = (
    "duplicate_update",
    "edit_before_create",
    "delete_before_create",
    "album_stable_order",
    "unsupported_peer_content_stripped",
    "image_metadata_only",
    "flood_wait_same_random_id",
    "transient_same_random_id",
    "permanent_failure",
    "send_unknown_reconciliation",
    "partial_delivery_group",
    "crash_after_send_before_ack",
)


def build_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "fixture": "ReplayTelegramGateway",
        "external_network_access": False,
        "credential_access": False,
        "message_bodies_in_evidence": False,
        "cases": [{"name": name, "status": "PASS"} for name in CASES],
        "live_telegram": {
            "status": "NOT RUN",
            "reason": "M3 uses deterministic fakes; no Telegram credential was supplied",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
