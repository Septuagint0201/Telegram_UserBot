"""Content-free extraction of stable pytest identities from JUnit XML."""

from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

JUnitStatus = Literal["PASS", "FAIL", "NOT RUN"]


def load_junit_results(path: Path) -> dict[str, JUnitStatus]:
    """Return ``classname::name`` outcomes without copying failure bodies."""

    root = ET.parse(path).getroot()  # noqa: S314 - CI-owned local artifact
    results: dict[str, JUnitStatus] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname")
        name = case.get("name")
        if not classname or not name:
            raise ValueError("JUnit testcase lacks classname or name")
        identity = f"{classname}::{name}"
        if case.find("failure") is not None or case.find("error") is not None:
            status: JUnitStatus = "FAIL"
        elif case.find("skipped") is not None:
            status = "NOT RUN"
        else:
            status = "PASS"
        previous = results.get(identity)
        if previous is not None and previous != status:
            raise ValueError(f"conflicting duplicate JUnit testcase: {identity}")
        results[identity] = status
    if not results:
        raise ValueError("JUnit artifact contains no testcases")
    return results


def evidence_status(
    results: dict[str, JUnitStatus], required_tests: tuple[str, ...]
) -> tuple[JUnitStatus, str | None]:
    """Aggregate exact required tests, failing closed on missing/skipped cases."""

    if not required_tests:
        raise ValueError("at least one required test is needed")
    missing = tuple(test_id for test_id in required_tests if test_id not in results)
    if missing:
        return "NOT RUN", "required JUnit testcase is missing: " + missing[0]
    failed = tuple(test_id for test_id in required_tests if results[test_id] == "FAIL")
    if failed:
        return "FAIL", "required JUnit testcase failed: " + failed[0]
    skipped = tuple(test_id for test_id in required_tests if results[test_id] == "NOT RUN")
    if skipped:
        return "NOT RUN", "required JUnit testcase did not run: " + skipped[0]
    return "PASS", None


__all__ = ["JUnitStatus", "evidence_status", "load_junit_results"]
