"""Create content-free milestone evidence bound to an exact signed commit."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict

from telegram_userbot.platform.evidence import JUnitStatus, evidence_status, load_junit_results
from telegram_userbot.platform.evidence.manifest import (
    requirement_ids_for_milestone,
    validate_manifest_semantics,
)


class RequirementRecord(TypedDict):
    id: str
    status: str
    evidence: list[str]
    tests: NotRequired[list[str]]
    reason: NotRequired[str]


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


def _tested_requirement(
    requirement_id: str,
    *evidence: str,
    test_ids: tuple[str, ...],
    junit_results: dict[str, JUnitStatus],
) -> RequirementRecord:
    status, reason = evidence_status(junit_results, test_ids)
    record: RequirementRecord = {
        "id": requirement_id,
        "status": status,
        "evidence": list(evidence),
        "tests": list(test_ids),
    }
    if reason is not None:
        record["reason"] = reason
    return record


def _requirements_for(milestone: str, *, root: Path) -> list[RequirementRecord]:
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
    if milestone == "M4":
        junit_results = load_junit_results(root / ".artifacts/m4/junit.xml")
        return [
            _tested_requirement(
                "M4-001",
                "src/telegram_userbot/domain/conversation/mode.py",
                "tests/unit/domain/test_conversation.py",
                test_ids=(
                    "tests.unit.domain.test_conversation::"
                    "test_mode_priority_snapshots_and_policy_blocks",
                    "tests.unit.domain.test_conversation::"
                    "test_final_gate_fails_closed_for_every_stale_dimension",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-002",
                "src/telegram_userbot/domain/conversation/turn.py",
                "alembic/versions/0004_m4_orchestrator.py",
                "alembic/versions/0005_m4_control_result.py",
                test_ids=(
                    "tests.unit.domain.test_conversation::"
                    "test_debounce_grace_and_work_snapshot_validation",
                    "tests.unit.domain.test_conversation::"
                    "test_splitter_is_deterministic_and_respects_combining_boundaries",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-003",
                "src/telegram_userbot/adapters/persistence/orchestrator_repository.py",
                "tests/integration/test_m4_conversation_orchestrator.py",
                test_ids=(
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_auto_turn_run_send_gate_and_reconciliation",
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[edit]",
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_multichunk_continuation_orders_chunks_and_rejects_strong_invalidations[human]",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-004",
                "tests/integration/test_m4_conversation_orchestrator.py",
                ".artifacts/m4/race-manifest.json",
                test_ids=(
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_generation_grace_and_late_result_discard",
                    "tests.unit.platform.test_conversation_runtime::"
                    "test_orchestrated_ingest_routes_all_event_classes",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-005",
                "src/telegram_userbot/processes/conversation_runtime.py",
                "tests/unit/adapters/persistence/test_orchestrator_repository.py",
                test_ids=(
                    "tests.unit.adapters.persistence.test_orchestrator_repository::"
                    "test_new_incoming_routes_collect_ready_and_generation_grace",
                    "tests.unit.adapters.persistence.test_orchestrator_repository::"
                    "test_generation_lease_renewal_obeys_owner_mode_and_run_state",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-006",
                "src/telegram_userbot/processes/conversation_runtime.py",
                "tests/unit/platform/test_conversation_runtime.py",
                test_ids=(
                    "tests.unit.platform.test_conversation_runtime::"
                    "test_runtime_auto_success_and_provider_failure",
                    "tests.unit.platform.test_conversation_runtime::"
                    "test_runtime_feedback_and_dispatch_fail_closed_branches",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-007",
                "src/telegram_userbot/adapters/persistence/orchestrator_repository.py",
                "src/telegram_userbot/adapters/telegram_bot/conversation_control.py",
                "src/telegram_userbot/adapters/telegram_bot/conversation_control_backend.py",
                test_ids=(
                    "tests.unit.adapters.test_conversation_control_backend::"
                    "test_control_backend_only_enqueues_and_replays_without_state_reads",
                    "tests.unit.adapters.test_conversation_control_backend::"
                    "test_app_processor_executes_routes_and_persists_terminal_versions",
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_queued_conversation_control_executes_reply_pending_once",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-008",
                "src/telegram_userbot/domain/conversation/draft.py",
                "tests/integration/test_m4_conversation_orchestrator.py",
                test_ids=(
                    "tests.integration.test_m4_conversation_orchestrator::"
                    "test_mode_flip_blocks_auto_and_copilot_tokens_are_one_time",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-009",
                "src/telegram_userbot/adapters/telegram_bot/conversation_control.py",
                "tests/unit/adapters/test_conversation_control.py",
                test_ids=(
                    "tests.unit.adapters.test_conversation_control::"
                    "test_conversation_commands_route_account_and_opaque_contact_scope",
                    "tests.unit.adapters.test_conversation_control_backend::"
                    "test_target_token_is_bound_to_admin_expiry_and_integrity",
                ),
                junit_results=junit_results,
            ),
            _tested_requirement(
                "M4-010",
                ".artifacts/m4/junit.xml",
                ".artifacts/m4/race-manifest.json",
                test_ids=(
                    "tests.unit.platform.test_m4_race_manifest::"
                    "test_m4_race_manifest_is_junit_derived_content_free_and_deadline_distinct",
                    "tests.unit.platform.test_m4_race_manifest::"
                    "test_m4_race_manifest_fails_closed_for_missing_or_failed_junit_case",
                ),
                junit_results=junit_results,
            ),
            (
                _tested_requirement(
                    "M4-011",
                    "docs/compatibility/m4.md",
                    "TODO.md",
                    "docs/Implementation-Plan.md",
                    test_ids=(
                        "tests.unit.test_m4_documentation_status::"
                        "test_m4_status_documents_are_consistent",
                    ),
                    junit_results=junit_results,
                )
                if "- [x] **M4-011" in (root / "TODO.md").read_text(encoding="utf-8")
                else {
                    "id": "M4-011",
                    "status": "NOT RUN",
                    "evidence": [],
                    "reason": "M4 awaits its signed GitLab Linux evidence before closeout",
                }
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

    requirements = _requirements_for(milestone, root=root)
    missing_evidence = sorted(
        evidence
        for requirement in requirements
        for evidence in requirement["evidence"]
        if not (root / str(evidence)).exists()
    )
    if missing_evidence:
        raise ValueError("acceptance evidence path is missing: " + missing_evidence[0])
    uses_disposable_services = milestone in {"M1", "M2", "M3", "M4"}
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
    parser.add_argument("--milestone", choices=("M0", "M1", "M2", "M3", "M4"), default="M0")
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
