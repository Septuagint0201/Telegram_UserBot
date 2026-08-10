import copy
import json
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from telegram_userbot.platform.evidence.manifest import (
    ManifestSemanticError,
    validate_manifest_semantics,
)

ROOT = Path(__file__).resolve().parents[3]


def valid_manifest() -> dict[str, object]:
    return {
        "source": {"commit": "a" * 40, "dirty": False},
        "requirements": [{"id": "M0-001", "status": "PASS", "evidence": ["test"]}],
    }


def first_requirement(document: dict[str, object]) -> dict[str, object]:
    requirements = cast(list[dict[str, object]], document["requirements"])
    return requirements[0]


@pytest.mark.unit
def test_valid_manifest_semantics() -> None:
    validate_manifest_semantics(valid_manifest(), required_requirement_ids=frozenset({"M0-001"}))


@pytest.mark.unit
def test_acceptance_json_schema_is_valid_and_accepts_all_id_forms() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "acceptance-manifest.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    document = {
        "schema_version": 1,
        "milestone": "M0",
        "generated_at": "2030-01-02T03:04:05Z",
        "source": {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "dirty": False,
            "signed_commit": True,
        },
        "environment": {
            "python": "3.14.7",
            "implementation": "CPython",
            "platform": "synthetic",
            "external_service_access": False,
        },
        "disclosure": {"path": "DISCLOSURE", "sha256": "c" * 64},
        "locks": [
            {"path": "runtime.lock", "sha256": "d" * 64},
            {"path": "dev.lock", "sha256": "e" * 64},
        ],
        "requirements": [
            {"id": "M0-001", "status": "PASS", "evidence": ["unit"]},
            {"id": "X-001", "status": "NOT RUN", "evidence": [], "reason": "future"},
        ],
        "external_evidence": [{"name": "external", "status": "NOT RUN", "reason": "M0 boundary"}],
    }
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        document
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source", {"commit": "short", "dirty": False}, "commit"),
        ("source", {"commit": "a" * 40, "dirty": True}, "clean"),
        ("requirements", "not-an-array", "array"),
    ],
)
def test_manifest_rejects_invalid_top_level(field: str, value: object, match: str) -> None:
    document = valid_manifest()
    document[field] = value
    with pytest.raises(ManifestSemanticError, match=match):
        validate_manifest_semantics(document)


@pytest.mark.unit
def test_manifest_rejects_unknown_or_unsubstantiated_status() -> None:
    document = valid_manifest()
    requirement = first_requirement(document)
    requirement["status"] = "UNKNOWN"
    with pytest.raises(ManifestSemanticError, match="unknown evidence status"):
        validate_manifest_semantics(document)

    document = valid_manifest()
    requirement = first_requirement(document)
    requirement["evidence"] = []
    with pytest.raises(ManifestSemanticError, match="lacks evidence"):
        validate_manifest_semantics(document)


@pytest.mark.unit
def test_manifest_requires_reason_for_non_pass_and_unique_ids() -> None:
    document = valid_manifest()
    requirement = first_requirement(document)
    requirement["status"] = "NOT RUN"
    with pytest.raises(ManifestSemanticError, match="lacks reason"):
        validate_manifest_semantics(document)

    document = valid_manifest()
    requirement = first_requirement(document)
    document["requirements"] = [requirement, copy.deepcopy(requirement)]
    with pytest.raises(ManifestSemanticError, match="duplicate"):
        validate_manifest_semantics(document)


@pytest.mark.unit
def test_manifest_requires_all_declared_requirement_ids() -> None:
    with pytest.raises(ManifestSemanticError, match="M0-002"):
        validate_manifest_semantics(
            valid_manifest(),
            required_requirement_ids=frozenset({"M0-001", "M0-002"}),
        )
