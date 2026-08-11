from typing import cast

from scripts.write_m3_replay_manifest import CASES, build_manifest


def test_m3_replay_manifest_is_content_free_and_complete() -> None:
    manifest = build_manifest()
    assert manifest["external_network_access"] is False
    assert manifest["credential_access"] is False
    assert manifest["message_bodies_in_evidence"] is False
    cases = cast(list[dict[str, str]], manifest["cases"])
    assert {case["name"] for case in cases} == set(CASES)
    assert all(case["status"] == "PASS" for case in cases)
