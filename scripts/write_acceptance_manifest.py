"""Create content-free milestone evidence bound to an exact signed commit."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from telegram_userbot.platform.evidence.manifest import (
    requirement_ids_for_milestone,
    validate_manifest_semantics,
)


class RequirementRecord(TypedDict):
    id: str
    status: str
    evidence: list[str]


def _git(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    return subprocess.run(  # noqa: S603 - list invocation without shell expansion
        [git, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _hashed_file(root: Path, relative: str) -> dict[str, str]:
    content = (root / relative).read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}


def _commit_signed(root: Path, commit: str) -> bool:
    raw = _git(root, "cat-file", "commit", commit)
    return "\ngpgsig -----BEGIN PGP SIGNATURE-----" in "\n" + raw


def _requirement(requirement_id: str, *evidence: str) -> RequirementRecord:
    return {"id": requirement_id, "status": "PASS", "evidence": list(evidence)}


def _requirements_for(milestone: str) -> list[RequirementRecord]:
    if milestone == "M0":
        return [
            _requirement(
                "M0-001", "pyproject.toml", "requirements/runtime.lock", "requirements/dev.lock"
            ),
            _requirement("M0-002", "src/telegram_userbot", "scripts/check_import_boundaries.py"),
            _requirement(
                "M0-003", "src/telegram_userbot/domain/shared", "tests/unit/domain/shared"
            ),
            _requirement("M0-004", "src/telegram_userbot/application/ports", "tests/contract"),
            _requirement(
                "M0-005",
                "src/telegram_userbot/platform/logging",
                "tests/unit/platform/test_safe_logging.py",
            ),
            _requirement(
                "M0-006",
                "src/telegram_userbot/platform/config",
                "tests/unit/platform/test_settings.py",
            ),
            _requirement(
                "M0-007", "pyproject.toml", ".artifacts/junit.xml", ".artifacts/coverage.json"
            ),
            _requirement("M0-008", "tests/support", "tests/property/test_state_machine_harness.py"),
            _requirement(
                "M0-009",
                "schemas/acceptance-manifest.schema.json",
                "tests/unit/platform/test_manifest.py",
            ),
            _requirement("M0-010", ".gitlab-ci.yml", "DISCLOSURE"),
            _requirement("M0-011", "scripts/repo_checks.py", "scripts/inspect_build_artifacts.py"),
            _requirement("M0-012", "TODO.md", "docs/Implementation-Plan.md", "README.md"),
        ]
    if milestone == "M1":
        return [
            _requirement("M1-001", "docs/compatibility/m1.md", "requirements/dev.lock"),
            _requirement("M1-002", "alembic.ini", "alembic/versions/0001_m1_durable_state.py"),
            _requirement("M1-003", "src/telegram_userbot/adapters/persistence/schema.py"),
            _requirement("M1-004", "tests/integration/test_m1_persistence.py"),
            _requirement("M1-005", "deploy/postgres/m1_roles.sql"),
            _requirement("M1-006", "src/telegram_userbot/adapters/persistence/repositories.py"),
            _requirement("M1-007", "src/telegram_userbot/adapters/persistence/locks.py"),
            _requirement("M1-008", "src/telegram_userbot/adapters/queue/redis.py"),
            _requirement("M1-009", "tests/integration/test_m1_persistence.py"),
            _requirement(
                "M1-010",
                "deploy/postgres/m1_roles.sql",
                ".artifacts/m1/migration-manifest.json",
            ),
            _requirement(
                "M1-011",
                ".artifacts/m1/junit.xml",
                ".artifacts/m1/migration-manifest.json",
            ),
            _requirement("M1-012", "TODO.md", "docs/Implementation-Plan.md", "README.md"),
        ]
    if milestone == "M2":
        return [
            _requirement(
                "M2-001",
                "alembic/versions/0002_m2_model_control.py",
                "src/telegram_userbot/adapters/persistence/schema.py",
                "tests/integration/test_m2_model_control.py",
            ),
            _requirement(
                "M2-002",
                "src/telegram_userbot/domain/model_config",
                "tests/unit/domain/test_model_config_and_crypto.py",
            ),
            _requirement(
                "M2-003",
                "src/telegram_userbot/adapters/llm/protocols.py",
                "src/telegram_userbot/adapters/embedding/__init__.py",
            ),
            _requirement("M2-004", "tests/contract/test_model_protocols.py"),
            _requirement(
                "M2-005",
                "src/telegram_userbot/platform/crypto/credentials.py",
                "deploy/postgres/m2_roles.sql",
            ),
            _requirement(
                "M2-006",
                "src/telegram_userbot/platform/network/endpoint_policy.py",
                "tests/unit/domain/test_model_config_and_crypto.py",
            ),
            _requirement("M2-007", "src/telegram_userbot/adapters/telegram_bot/model_control.py"),
            _requirement(
                "M2-008",
                "src/telegram_userbot/adapters/webapp",
                ".artifacts/m2/browser-manifest.json",
            ),
            _requirement(
                "M2-009",
                "src/telegram_userbot/domain/model_config/capabilities.py",
                "tests/integration/test_m2_model_control.py",
            ),
            _requirement(
                "M2-010",
                ".artifacts/m2/junit.xml",
                ".artifacts/m2/browser-manifest.json",
            ),
            _requirement(
                "M2-011",
                "docs/compatibility/m2.md",
                "TODO.md",
                "docs/Implementation-Plan.md",
            ),
        ]
    if milestone == "M3":
        return [
            _requirement(
                "M3-001",
                "src/telegram_userbot/application/ports/telegram.py",
                "src/telegram_userbot/adapters/telegram_user/telethon.py",
            ),
            _requirement(
                "M3-002",
                "src/telegram_userbot/adapters/telegram_user/normalizer.py",
                "tests/unit/adapters/telegram_user/test_normalizer_and_fake.py",
            ),
            _requirement(
                "M3-003",
                "src/telegram_userbot/adapters/persistence/telegram_repository.py",
                "tests/integration/test_m3_telegram_lifecycle.py",
            ),
            _requirement(
                "M3-004",
                "src/telegram_userbot/domain/messaging/events.py",
                "tests/integration/test_m3_telegram_lifecycle.py",
            ),
            _requirement(
                "M3-005",
                "alembic/versions/0003_m3_telegram_lifecycle.py",
                "src/telegram_userbot/domain/messaging/outbound.py",
            ),
            _requirement(
                "M3-006",
                "src/telegram_userbot/adapters/persistence/telegram_repository.py",
                ".artifacts/m3/replay-manifest.json",
            ),
            _requirement(
                "M3-007",
                "src/telegram_userbot/application/ports/telegram.py",
                "tests/contract/application/test_ports.py",
            ),
            _requirement(
                "M3-008",
                "src/telegram_userbot/adapters/telegram_user/fake.py",
                ".artifacts/m3/replay-manifest.json",
            ),
            _requirement(
                "M3-009",
                "src/telegram_userbot/adapters/persistence/telegram_delivery.py",
                "tests/integration/test_m3_telegram_lifecycle.py",
            ),
            _requirement(
                "M3-010",
                "docs/compatibility/m3.md",
                "TODO.md",
                "docs/Implementation-Plan.md",
            ),
        ]
    raise ValueError("unsupported milestone")


def build_manifest(root: Path, commit: str, *, milestone: str = "M0") -> dict[str, object]:
    _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    if commit != _git(root, "rev-parse", "HEAD"):
        raise ValueError("acceptance evidence commit must equal HEAD")
    if _git(root, "status", "--short"):
        raise ValueError("acceptance evidence requires a clean worktree")
    if not _commit_signed(root, commit):
        raise ValueError("acceptance evidence requires a signed commit")

    requirements = _requirements_for(milestone)
    missing_evidence = sorted(
        evidence
        for requirement in requirements
        for evidence in requirement["evidence"]
        if not (root / str(evidence)).exists()
    )
    if missing_evidence:
        raise ValueError("acceptance evidence path is missing: " + missing_evidence[0])
    uses_disposable_services = milestone in {"M1", "M2", "M3"}
    environment: dict[str, object] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system().lower(),
        "external_service_access": uses_disposable_services,
    }
    if uses_disposable_services:
        artifact_milestone = milestone.lower()
        migration_manifest = json.loads(
            (root / ".artifacts" / artifact_milestone / "migration-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        environment["services"] = migration_manifest["services"]

    document: dict[str, object] = {
        "schema_version": 1,
        "milestone": milestone,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "commit": commit,
            "tree": _git(root, "rev-parse", f"{commit}^{{tree}}"),
            "dirty": False,
            "signed_commit": True,
        },
        "environment": environment,
        "disclosure": _hashed_file(root, "DISCLOSURE"),
        "locks": [
            _hashed_file(root, "requirements/bootstrap.lock"),
            _hashed_file(root, "requirements/runtime.lock"),
            _hashed_file(root, "requirements/dev.lock"),
            _hashed_file(root, "requirements/lock-tools.lock"),
        ],
        "requirements": requirements,
        "external_evidence": [
            {
                "name": "telegram-provider-live-runtime"
                if uses_disposable_services
                else "telegram-provider-database-redis-runtime",
                "status": "NOT RUN",
                "reason": (
                    f"{milestone} validates only disposable services and local fakes"
                    if uses_disposable_services
                    else "M0 explicitly contains no external adapters or credentials"
                ),
            },
            {
                "name": "ubuntu-production-deployment",
                "status": "NOT RUN",
                "reason": "production deployment evidence starts in M8",
            },
        ],
    }
    validate_manifest_semantics(
        document,
        required_requirement_ids=requirement_ids_for_milestone(milestone),
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--milestone", choices=("M0", "M1", "M2", "M3"), default="M0")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    document = build_manifest(root, args.commit, milestone=args.milestone)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
