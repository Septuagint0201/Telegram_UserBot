"""Semantic checks supplementing the acceptance-manifest JSON Schema."""

from collections.abc import Mapping, Sequence

from telegram_userbot.domain.shared.result import EvidenceStatus


class ManifestSemanticError(ValueError):
    pass


M8_RUNTIME_REQUIREMENT_IDS = frozenset(
    {
        "M8-001",
        "M8-002",
        "M8-003",
        "M8-004",
        "M8-005",
        "M8-006",
        "M8-007",
        "M8-008",
        "M8-009",
        "M8-010",
        "M8-011",
        "M8-012",
        "M8-013",
        "M8-014",
        "M8-015",
        "M8-016",
    }
)
M8_EVIDENCE_SCOPES = frozenset(
    {
        "source-static",
        "ci-synthetic",
        "ubuntu-synthetic",
        "ubuntu-production",
        "manual",
    }
)


def requirement_ids_for_milestone(milestone: object) -> frozenset[str]:
    requirement_counts = {
        "M0": 12,
        "M1": 12,
        "M2": 11,
        "M3": 10,
        "M4": 11,
        "M5": 11,
        "M6": 12,
        "M7": 12,
        "M8": 16,
    }
    if milestone not in requirement_counts:
        raise ManifestSemanticError("unsupported evidence milestone")
    count = requirement_counts[milestone]
    return frozenset(f"{milestone}-{index:03d}" for index in range(1, count + 1))


def _as_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestSemanticError(f"{field} must be an object")
    return value


def _as_sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ManifestSemanticError(f"{field} must be an array")
    return value


def _validate_m8_requirement(
    requirement_id: str,
    status: EvidenceStatus,
    evidence_scope: object,
) -> None:
    if not requirement_id.startswith("M8-"):
        return
    if evidence_scope not in M8_EVIDENCE_SCOPES:
        raise ManifestSemanticError(f"M8 requirement has invalid evidence scope: {requirement_id}")
    if (
        status is EvidenceStatus.PASS
        and requirement_id in M8_RUNTIME_REQUIREMENT_IDS
        and evidence_scope != "ubuntu-production"
    ):
        raise ManifestSemanticError(
            f"runtime M8 requirement cannot pass from static evidence: {requirement_id}"
        )


def _validate_requirement(raw_requirement: object, index: int) -> str:
    requirement = _as_mapping(raw_requirement, f"requirements[{index}]")
    requirement_id = requirement.get("id")
    status = requirement.get("status")
    evidence = _as_sequence(requirement.get("evidence"), f"requirements[{index}].evidence")
    tests = requirement.get("tests")
    if tests is not None:
        parsed_tests = _as_sequence(tests, f"requirements[{index}].tests")
        if not parsed_tests:
            raise ManifestSemanticError(f"declared tests are empty: {requirement_id}")
    if not isinstance(requirement_id, str) or not requirement_id:
        raise ManifestSemanticError("requirement id must be non-empty")
    if not isinstance(status, str):
        raise ManifestSemanticError(f"evidence status must be a string for {requirement_id}")
    try:
        parsed_status = EvidenceStatus(status)
    except ValueError as error:
        raise ManifestSemanticError(f"unknown evidence status for {requirement_id}") from error
    if parsed_status is EvidenceStatus.PASS and not evidence:
        raise ManifestSemanticError(f"PASS requirement lacks evidence: {requirement_id}")
    if parsed_status is not EvidenceStatus.PASS and not requirement.get("reason"):
        raise ManifestSemanticError(f"non-PASS requirement lacks reason: {requirement_id}")
    _validate_m8_requirement(requirement_id, parsed_status, requirement.get("evidence_scope"))
    return requirement_id


def validate_manifest_semantics(
    document: Mapping[str, object],
    *,
    required_requirement_ids: frozenset[str] = frozenset(),
) -> None:
    source = _as_mapping(document.get("source"), "source")
    commit = source.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(ch not in "0123456789abcdef" for ch in commit)
    ):
        raise ManifestSemanticError("source.commit must be a lowercase full SHA-1")
    if source.get("dirty") is not False:
        raise ManifestSemanticError("source must be clean")

    requirements = _as_sequence(document.get("requirements"), "requirements")
    seen: set[str] = set()
    for index, raw_requirement in enumerate(requirements):
        requirement_id = _validate_requirement(raw_requirement, index)
        if requirement_id in seen:
            raise ManifestSemanticError(f"duplicate requirement id: {requirement_id}")
        seen.add(requirement_id)

    missing = required_requirement_ids - seen
    if missing:
        raise ManifestSemanticError("missing requirement ids: " + ", ".join(sorted(missing)))
