import json
import subprocess
import sys
import tarfile
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m8_base_image_lock_is_valid_and_platform_specific() -> None:
    schema = json.loads((ROOT / "deploy/images/image-lock.schema.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "deploy/images/base-images.lock.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        lock
    )

    assert lock["target_platform"] == {"os": "linux", "architecture": "amd64"}
    assert lock["production_artifacts"] == {
        "status": "NOT_BUILT",
        "images": [],
        "sbom_status": "NOT_GENERATED",
        "license_status": "NOT_GENERATED",
    }
    assert {item["id"] for item in lock["base_images"]} == {
        "python_runtime",
        "postgres_pgvector",
        "redis",
        "caddy_runtime",
    }
    for item in lock["base_images"]:
        assert item["reference"].endswith(item["platform_digest"])
        assert "latest" not in item["reference"]


@pytest.mark.unit
def test_runtime_dockerfile_is_locked_non_root_and_has_no_fake_command() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock = json.loads((ROOT / "deploy/images/base-images.lock.json").read_text(encoding="utf-8"))
    python_reference = next(
        item["reference"] for item in lock["base_images"] if item["id"] == "python_runtime"
    )
    assert f'ARG PYTHON_BASE="{python_reference}"' in dockerfile
    assert "--require-hashes" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "SOURCE_COMMIT" in dockerfile
    assert "/var/lib/telegram-userbot/session" in dockerfile
    assert "/var/lib/telegram-userbot/media" in dockerfile
    assert "COPY --chown=10001:10001 DISCLOSURE /opt/app/DISCLOSURE" in dockerfile
    assert "telegram-userbot-check" not in dockerfile
    assert ":latest" not in dockerfile

    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for sensitive in (".git", ".handoff", ".env*", "*.session*", "secrets"):
        assert sensitive in ignored.splitlines()


@pytest.mark.unit
def test_gateway_dockerfile_clears_upstream_file_capability_and_is_non_root() -> None:
    dockerfile = (ROOT / "deploy/caddy/Dockerfile").read_text(encoding="utf-8")
    lock = json.loads((ROOT / "deploy/images/base-images.lock.json").read_text(encoding="utf-8"))
    caddy_reference = next(
        item["reference"] for item in lock["base_images"] if item["id"] == "caddy_runtime"
    )
    assert f'ARG CADDY_BASE="{caddy_reference}"' in dockerfile
    assert "SOURCE_COMMIT" in dockerfile
    assert "setcap -r /usr/bin/caddy" in dockerfile
    assert 'test -z "$(getcap /usr/bin/caddy)"' in dockerfile
    assert dockerfile.rstrip().endswith("USER 1000:1000")
    assert ":latest" not in dockerfile


@pytest.mark.unit
def test_image_dockerfiles_use_posix_source_commit_guard_and_package_ops_scripts() -> None:
    dockerfile_paths = (
        ROOT / "Dockerfile",
        ROOT / "deploy/ops/Dockerfile",
        ROOT / "deploy/postgres/Dockerfile",
        ROOT / "deploy/caddy/Dockerfile",
    )
    for path in dockerfile_paths:
        dockerfile = path.read_text(encoding="utf-8")
        # Docker RUN defaults to /bin/sh; Bash-only ${#...} would fail on Debian dash.
        assert "${#SOURCE_COMMIT}" not in dockerfile
        assert "printf '%s' \"${SOURCE_COMMIT:-}\" | wc -c" in dockerfile
        assert 'case "${SOURCE_COMMIT:-}" in *[!0-9a-f]*)' in dockerfile

    operations = (ROOT / "deploy/ops/Dockerfile").read_text(encoding="utf-8")
    postgres = (ROOT / "deploy/postgres/Dockerfile").read_text(encoding="utf-8")
    assert 'ARG PGBACKREST_VERSION="2.59.1-1.pgdg12+1"' in postgres
    assert (
        'ARG PGBACKREST_DEB_SHA256="ecea2337fe53ec1f86d87db21ad46747c7ff898bd877288452b90374e80a6'
        'fb9"' in postgres
    )
    assert 'apt-get download "pgbackrest=${PGBACKREST_VERSION}"' in postgres
    assert "sha256sum --check --status" in postgres
    assert "pgbackrest_*.deb" in postgres
    assert "2.45-1" not in postgres
    assert (
        'ARG APP_IMAGE="invalid.invalid/telegram-userbot-app@sha256:' + "0" * 64 + '"' in operations
    )
    assert "FROM ${APP_IMAGE}" in operations
    for script in (
        "session_backup.py",
        "session_restore.py",
        "data_export.py",
        "monitor.py",
        "restore_gate.py",
    ):
        assert f"deploy/ops/{script} /opt/ops/{script}" in operations
    assert "mkdir -p /session" in operations
    assert "chown 10001:10001 /session" in operations
    assert "chmod 0700 /session" in operations
    assert operations.rstrip().endswith('USER 10001:10001\nENTRYPOINT ["/opt/venv/bin/python"]')


@pytest.mark.unit
def test_sdist_contains_review_and_image_build_inputs(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed interpreter and build module
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    archives = tuple(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], mode="r:gz") as archive:
        members = {
            "/".join(Path(name).parts[1:])
            for name in archive.getnames()
            if len(Path(name).parts) > 1
        }
    assert {
        ".dockerignore",
        "DISCLOSURE",
        "Dockerfile",
        "deploy/caddy/Dockerfile",
    } <= members


@pytest.mark.unit
def test_python_sbom_and_license_inventory_contract(tmp_path: Path) -> None:
    output = tmp_path / "python-inventory.cdx.json"
    image_reference = "registry.invalid/telegram-userbot@sha256:" + "a" * 64
    source_commit = "b" * 40
    subprocess.run(  # noqa: S603 - fixed interpreter and reviewed repository script
        [
            sys.executable,
            str(ROOT / "deploy/sbom/generate_python_inventory.py"),
            "--lock",
            str(ROOT / "requirements/runtime.lock"),
            "--image-reference",
            image_reference,
            "--source-commit",
            source_commit,
            "--created-at",
            "2026-08-23T00:00:00Z",
            "--output",
            str(output),
        ],
        check=True,
    )
    inventory = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "deploy/sbom/python-inventory.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        inventory
    )

    assert inventory["metadata"]["component"]["bom-ref"] == image_reference
    assert {component["name"] for component in inventory["components"]} >= {
        "alembic",
        "sqlalchemy",
        "telethon",
    }
    serialized = output.read_text(encoding="utf-8").lower()
    for forbidden in ("authorization", "api_key", "telegram session", "private key"):
        assert forbidden not in serialized
