import copy
import json
from pathlib import Path
from typing import cast

import jsonschema
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from telegram_userbot.platform.evidence.manifest import (
    ManifestSemanticError,
    requirement_ids_for_milestone,
    validate_manifest_semantics,
)

ROOT = Path(__file__).resolve().parents[3]


def valid_manifest() -> dict[str, object]:
    return {
        "source": {"commit": "a" * 40, "dirty": False},
        "requirements": [{"id": "M0-001", "status": "PASS", "evidence": ["test"]}],
    }


@pytest.mark.unit
def test_alembic_head_fits_default_version_column() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head == "0012_worker_lease_retry"
    assert len(head) <= 32


def first_requirement(document: dict[str, object]) -> dict[str, object]:
    requirements = cast(list[dict[str, object]], document["requirements"])
    return requirements[0]


@pytest.mark.unit
def test_valid_manifest_semantics() -> None:
    validate_manifest_semantics(valid_manifest(), required_requirement_ids=frozenset({"M0-001"}))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("milestone", "count"),
    [
        ("M0", 12),
        ("M1", 12),
        ("M2", 11),
        ("M3", 10),
        ("M4", 11),
        ("M5", 11),
        ("M6", 12),
    ],
)
def test_requirement_ids_follow_supported_manifest_milestone(milestone: str, count: int) -> None:
    assert requirement_ids_for_milestone(milestone) == frozenset(
        f"{milestone}-{index:03d}" for index in range(1, count + 1)
    )


@pytest.mark.unit
def test_requirement_ids_reject_unknown_or_missing_milestone() -> None:
    for milestone in (None, "M8", 1):
        with pytest.raises(ManifestSemanticError, match="unsupported evidence milestone"):
            requirement_ids_for_milestone(milestone)


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
            "external_service_access": True,
            "services": [
                {
                    "name": "postgresql-pgvector",
                    "version": "17.10 / 0.8.6",
                    "image": "example@sha256:" + "f" * 64,
                }
            ],
        },
        "disclosure": {"path": "DISCLOSURE", "sha256": "c" * 64},
        "locks": [
            {"path": "runtime.lock", "sha256": "d" * 64},
            {"path": "dev.lock", "sha256": "e" * 64},
        ],
        "requirements": [
            {
                "id": "M0-001",
                "status": "PASS",
                "evidence": ["unit"],
                "tests": ["tests.unit.test_example::test_case"],
            },
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

    document = valid_manifest()
    first_requirement(document)["tests"] = []
    with pytest.raises(ManifestSemanticError, match="declared tests are empty"):
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
