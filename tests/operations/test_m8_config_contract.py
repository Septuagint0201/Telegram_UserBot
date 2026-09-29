import copy
import json
from pathlib import Path

import jsonschema
import pytest

from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_deployment_schema_covers_every_artifact_and_bounded_26_04_profile() -> None:
    schema = json.loads((ROOT / "deploy/config/deployment.schema.json").read_text(encoding="utf-8"))
    example = json.loads(
        (ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.check_schema(schema)
    validator.validate(example)

    assert example["deployable"] is False
    assert example["target"] == {
        "ubuntu": "26.04",
        "os": "linux",
        "architecture": "amd64",
        "vcpu": 2,
        "memory_gib": 4,
        "minimum_disk_gib": 40,
        "validation_disk_gib": 64,
    }
    assert example["database_compatibility"] == {
        "schema_revision": EXPECTED_SCHEMA_REVISION,
        "pgvector_version": "0.8.6",
    }
    assert example["runtime_identity"] == {
        "account_id": "01900000-0000-7000-8000-000000000001",
        "telegram_user_id": 1000000001,
        "control_bot_user_id": 1000000002,
        "control_bot_username": "ExampleControlBot",
        "control_admin_user_ids": [1000000001],
    }
    assert example["startup_policy"] == {
        "session_provisioning": "preprovisioned_one_shot",
        "restore_gate_required": True,
    }
    assert set(example["images"]) == {
        "application",
        "database",
        "gateway",
        "redis",
        "session-backup",
        "data-export",
    }
    assert all("@sha256:" in artifact["reference"] for artifact in example["images"].values())
    assert (
        example["images"]["session-backup"]["reference"]
        == (example["images"]["data-export"]["reference"])
    )
    assert example["secret_manifest"] == "secret-files.json"  # noqa: S105
    assert (ROOT / "deploy/config" / example["secret_manifest"]).is_file()

    deployable = copy.deepcopy(example)
    deployable["deployable"] = True
    deployable["deployment_id"] = "prod-primary"
    deployable["source_commit"] = "a" * 40
    deployable["public_host"] = "keys.example.net"
    for index, artifact in enumerate(deployable["images"].values(), start=1):
        artifact["status"] = "BUILT"
        artifact["reference"] = f"registry.example/image-{index}@sha256:" + f"{index:x}" * 64
    deployable["images"]["data-export"]["reference"] = deployable["images"]["session-backup"][
        "reference"
    ]
    validator.validate(deployable)

    incomplete = copy.deepcopy(deployable)
    incomplete["images"]["data-export"]["status"] = "NOT_BUILT"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(incomplete)


@pytest.mark.unit
def test_secret_manifest_schema_is_content_free_and_consumers_are_exact() -> None:
    schema = json.loads(
        (ROOT / "deploy/config/secret-files.schema.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((ROOT / "deploy/config/secret-files.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.check_schema(schema)
    validator.validate(manifest)

    entries = {item["id"]: item for item in manifest["files"]}
    assert len(entries) == len(manifest["files"]) == 23
    assert len({item["filename"] for item in manifest["files"]}) == 23
    assert entries["postgres_database_password"]["services"] == ["postgres-bootstrap"]
    assert set(entries["app_database_password"]["services"]) == {
        "app",
        "postgres-bootstrap",
    }
    assert set(entries["control_database_password"]["services"]) == {
        "control",
        "postgres-bootstrap",
    }
    assert set(entries["worker_database_password"]["services"]) == {
        "worker",
        "postgres-bootstrap",
    }
    assert set(entries["migrator_database_password"]["services"]) == {
        "migrate",
        "postgres-bootstrap",
    }
    assert set(entries["export_database_password"]["services"]) == {
        "data-export",
        "erasure-ledger-export",
        "postgres-bootstrap",
    }
    assert set(entries["monitor_database_password"]["services"]) == {
        "ops-monitor",
        "postgres-bootstrap",
    }
    for entry in manifest["files"]:
        assert entry["expected_uid"] == 0
        assert 20000 <= entry["expected_gid"] <= 29999
        assert entry["mode"] == "0440"
        assert 0 < entry["max_bytes"] <= 65536
