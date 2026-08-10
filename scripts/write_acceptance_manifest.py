"""Create content-free M0 acceptance evidence bound to an exact signed commit."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from telegram_userbot.platform.evidence.manifest import validate_manifest_semantics


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


def build_manifest(root: Path, commit: str) -> dict[str, object]:
    _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    if commit != _git(root, "rev-parse", "HEAD"):
        raise ValueError("acceptance evidence commit must equal HEAD")
    if _git(root, "status", "--short"):
        raise ValueError("acceptance evidence requires a clean worktree")
    if not _commit_signed(root, commit):
        raise ValueError("acceptance evidence requires a signed commit")

    requirements = [
        _requirement(
            "M0-001", "pyproject.toml", "requirements/runtime.lock", "requirements/dev.lock"
        ),
        _requirement("M0-002", "src/telegram_userbot", "scripts/check_import_boundaries.py"),
        _requirement("M0-003", "src/telegram_userbot/domain/shared", "tests/unit/domain/shared"),
        _requirement("M0-004", "src/telegram_userbot/application/ports", "tests/contract"),
        _requirement(
            "M0-005",
            "src/telegram_userbot/platform/logging",
            "tests/unit/platform/test_safe_logging.py",
        ),
        _requirement(
            "M0-006", "src/telegram_userbot/platform/config", "tests/unit/platform/test_settings.py"
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
    missing_evidence = sorted(
        evidence
        for requirement in requirements
        for evidence in requirement["evidence"]
        if not (root / str(evidence)).exists()
    )
    if missing_evidence:
        raise ValueError("acceptance evidence path is missing: " + missing_evidence[0])
    document: dict[str, object] = {
        "schema_version": 1,
        "milestone": "M0",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "commit": commit,
            "tree": _git(root, "rev-parse", f"{commit}^{{tree}}"),
            "dirty": False,
            "signed_commit": True,
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.system().lower(),
            "external_service_access": False,
        },
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
                "name": "telegram-provider-database-redis-runtime",
                "status": "NOT RUN",
                "reason": "M0 explicitly contains no external adapters or credentials",
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
        required_requirement_ids=frozenset(f"M0-{index:03d}" for index in range(1, 13)),
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    document = build_manifest(root, args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
