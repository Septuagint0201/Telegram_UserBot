from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

import pytest
from scripts.write_m4_race_manifest import CASE_TESTS, CASES, build_manifest

from telegram_userbot.platform.evidence import load_junit_results


def write_junit(path: Path, *, missing: str | None = None, failed: str | None = None) -> None:
    suite = ET.Element("testsuite")
    for test_id in sorted({test_id for tests in CASE_TESTS.values() for test_id in tests}):
        if test_id == missing:
            continue
        classname, name = test_id.split("::", maxsplit=1)
        case = ET.SubElement(suite, "testcase", classname=classname, name=name)
        if test_id == failed:
            ET.SubElement(case, "failure")
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def test_junit_loader_rejects_malformed_empty_and_conflicting_artifacts(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.xml"
    ET.ElementTree(ET.Element("testcase", name="missing-class")).write(malformed)
    with pytest.raises(ValueError, match="classname"):
        load_junit_results(malformed)

    empty = tmp_path / "empty.xml"
    ET.ElementTree(ET.Element("testsuite")).write(empty)
    with pytest.raises(ValueError, match="no testcases"):
        load_junit_results(empty)

    conflicting = tmp_path / "conflicting.xml"
    suite = ET.Element("testsuite")
    ET.SubElement(suite, "testcase", classname="tests.unit.test", name="test_case")
    failed = ET.SubElement(suite, "testcase", classname="tests.unit.test", name="test_case")
    ET.SubElement(failed, "failure")
    ET.ElementTree(suite).write(conflicting)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        load_junit_results(conflicting)


def test_m4_race_manifest_is_junit_derived_content_free_and_deadline_distinct(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "junit.xml"
    write_junit(junit)
    manifest = build_manifest(junit)
    assert manifest["external_network_access"] is False
    assert manifest["credential_access"] is False
    assert manifest["message_bodies_in_evidence"] is False
    assert manifest["grace_seconds"] == 3
    assert manifest["provider_timeout_is_distinct"] is True
    cases = cast(list[dict[str, object]], manifest["cases"])
    assert {case["name"] for case in cases} == set(CASES)
    assert all(case["status"] == "PASS" for case in cases)
    assert all(case["tests"] for case in cases)


def test_m4_race_manifest_fails_closed_for_missing_or_failed_junit_case(tmp_path: Path) -> None:
    required = CASE_TESTS["durable_control_command_replay"][0]
    missing = tmp_path / "missing.xml"
    write_junit(missing, missing=required)
    missing_cases = cast(list[dict[str, object]], build_manifest(missing)["cases"])
    missing_case = next(
        case for case in missing_cases if case["name"] == "durable_control_command_replay"
    )
    assert missing_case["status"] == "NOT RUN"
    assert "missing" in cast(str, missing_case["reason"])

    failed = tmp_path / "failed.xml"
    write_junit(failed, failed=required)
    failed_cases = cast(list[dict[str, object]], build_manifest(failed)["cases"])
    failed_case = next(
        case for case in failed_cases if case["name"] == "durable_control_command_replay"
    )
    assert failed_case["status"] == "FAIL"
    assert "failed" in cast(str, failed_case["reason"])
