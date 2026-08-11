from typing import cast

from scripts.write_m4_race_manifest import CASES, build_manifest


def test_m4_race_manifest_is_content_free_and_keeps_deadlines_distinct() -> None:
    manifest = build_manifest()
    assert manifest["external_network_access"] is False
    assert manifest["credential_access"] is False
    assert manifest["message_bodies_in_evidence"] is False
    assert manifest["grace_seconds"] == 3
    assert manifest["provider_timeout_is_distinct"] is True
    cases = cast(list[dict[str, str]], manifest["cases"])
    assert {case["name"] for case in cases} == set(CASES)
    assert all(case["status"] == "PASS" for case in cases)
