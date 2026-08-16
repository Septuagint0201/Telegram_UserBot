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


def _requirements_for(  # noqa: PLR0911 - milestone mappings remain explicit
    milestone: str, *, root: Path
) -> list[RequirementRecord]:
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
    if milestone == "M5":
        junit_results = load_junit_results(root / ".artifacts/m5/junit.xml")
        m5_mapping: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
            (
                "M5-001",
                (
                    "src/telegram_userbot/adapters/media/validation.py",
                    "tests/unit/adapters/test_media.py",
                ),
                (
                    "tests.unit.adapters.test_media::test_image_ingestion_validates_magic_decode_limits_and_hash",
                    "tests.unit.adapters.test_media::test_image_ingestion_fails_closed_for_size_dimension_pixel_and_timeout",
                ),
            ),
            (
                "M5-002",
                (
                    "src/telegram_userbot/adapters/media/storage.py",
                    "tests/unit/adapters/test_media.py",
                ),
                (
                    "tests.unit.adapters.test_media::test_private_store_is_atomic_hash_verified_and_provider_copy_clears_exif",
                    "tests.unit.adapters.test_media::test_media_cleanup_respects_expiry_references_and_missing_files",
                ),
            ),
            (
                "M5-003",
                (
                    "src/telegram_userbot/domain/context/policy.py",
                    "tests/property/test_m5_context_budget.py",
                ),
                (
                    "tests.property.test_m5_context_budget::test_effective_context_budget_never_exceeds_policy_or_model_window",
                    "tests.property.test_m5_context_budget::test_current_turn_is_never_partially_truncated",
                    "tests.unit.domain.test_context::test_context_soft_quotas_borrow_deterministically_and_image_reserve_must_match",
                ),
            ),
            (
                "M5-004",
                (
                    "src/telegram_userbot/domain/context/selection.py",
                    "tests/unit/domain/test_context.py",
                ),
                (
                    "tests.unit.domain.test_context::test_structured_and_semantic_selection_are_stable_exact_and_single_space",
                    "tests.unit.domain.test_context::test_dedup_prefers_recent_then_structured_summary_and_semantic",
                ),
            ),
            (
                "M5-005",
                (
                    "src/telegram_userbot/domain/context/manifest.py",
                    "tests/unit/domain/test_context.py",
                ),
                (
                    "tests.unit.domain.test_context::test_prompt_injection_stays_in_data_boundary_and_manifest_rebuilds",
                ),
            ),
            (
                "M5-006",
                (
                    "src/telegram_userbot/domain/context/manifest.py",
                    "src/telegram_userbot/adapters/persistence/context_repository.py",
                    "tests/integration/test_m5_context_media.py",
                ),
                (
                    "tests.unit.domain.test_context::test_prompt_injection_stays_in_data_boundary_and_manifest_rebuilds",
                    "tests.unit.domain.test_context::test_memory_and_summary_sources_keep_typed_manifest_identity",
                    "tests.integration.test_m5_context_media::test_m5_manifest_persists_content_free_and_preview_is_one_time",
                ),
            ),
            (
                "M5-007",
                (
                    "src/telegram_userbot/adapters/llm/protocols.py",
                    "tests/contract/test_model_protocols.py",
                ),
                (
                    "tests.contract.test_model_protocols::test_generation_wire_contracts_are_protocol_specific_and_secret_safe[openai_responses-/responses-max_output_tokens]",
                    "tests.contract.test_model_protocols::test_generation_wire_contracts_are_protocol_specific_and_secret_safe[openai_chat_completions-/chat/completions-max_completion_tokens]",
                    "tests.contract.test_model_protocols::test_generation_wire_contracts_are_protocol_specific_and_secret_safe[anthropic_messages-/messages-max_tokens]",
                    "tests.contract.test_model_protocols::test_image_wire_fails_closed_without_capability_or_messages_auto_equivalence",
                ),
            ),
            (
                "M5-008",
                (
                    "src/telegram_userbot/adapters/telegram_bot/context_control.py",
                    "tests/unit/adapters/test_context_control.py",
                ),
                (
                    "tests.unit.adapters.test_context_control::test_context_command_returns_metadata_only",
                ),
            ),
            (
                "M5-009",
                (
                    "src/telegram_userbot/adapters/telegram_bot/context_control_backend.py",
                    "tests/unit/adapters/test_context_control.py",
                    "tests/integration/test_m5_context_media.py",
                ),
                (
                    "tests.unit.adapters.test_context_control::test_context_preview_requires_private_admin_and_one_time_confirmation",
                    "tests.unit.adapters.test_context_control::test_preview_send_unknown_is_explicit_and_not_retried",
                    "tests.unit.adapters.test_context_control_backend::test_exact_manifest_rebuilder_checks_vector_content_render_and_eligibility",
                    "tests.unit.adapters.persistence.test_context_repository::test_context_preview_consume_delivery_and_deletion_state_branches",
                    "tests.integration.test_m5_context_media::test_m5_manifest_persists_content_free_and_preview_is_one_time",
                    "tests.integration.test_m5_context_media::test_m5_control_role_executes_preview_function_without_direct_content_access",
                ),
            ),
            (
                "M5-010",
                ("src/telegram_userbot/adapters/media/storage.py", "deploy/postgres/m5_roles.sql"),
                (
                    "tests.unit.adapters.test_media::test_media_cleanup_respects_expiry_references_and_missing_files",
                ),
            ),
            (
                "M5-011",
                ("docs/compatibility/m5.md", "TODO.md", "docs/Implementation-Plan.md"),
                (
                    "tests.unit.test_m5_documentation_status::test_m5_status_documents_are_consistent",
                ),
            ),
        )
        return [
            _tested_requirement(
                requirement_id,
                *evidence,
                test_ids=test_ids,
                junit_results=junit_results,
            )
            for requirement_id, evidence, test_ids in m5_mapping
        ]
    if milestone == "M6":
        junit_results = load_junit_results(root / ".artifacts/m6/junit.xml")
        m6_mapping: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
            (
                "M6-001",
                ("src/telegram_userbot/domain/memory/trigger.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_trigger_conditions_are_independent_or_and_compensation_is_visible",
                ),
            ),
            (
                "M6-002",
                (
                    "src/telegram_userbot/adapters/persistence/memory_repository.py",
                    "tests/integration/test_m6_memory_pipeline.py",
                ),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_generation_running_range_is_immutable_and_new_events_get_next_generation",
                    "tests.unit.domain.test_m6_memory::"
                    "test_trigger_generation_guards_and_fencing_fail_closed",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_pending_range_claim_is_immutable_and_later_event_gets_next_generation",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_threshold_retry_and_expired_lease_are_runnable",
                ),
            ),
            (
                "M6-003",
                (
                    "src/telegram_userbot/domain/memory/models.py",
                    "src/telegram_userbot/domain/memory/fakes.py",
                    "src/telegram_userbot/adapters/persistence/memory_repository.py",
                    "tests/integration/test_m6_memory_pipeline.py",
                ),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_strict_parser_rejects_unknown_fields_and_validates_current_evidence",
                    "tests.unit.domain.test_m6_memory::"
                    "test_validator_rejects_malformed_evidence_and_unsafe_proposals",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_manifest_membership_is_content_free_and_hash_bound",
                    "tests.unit.domain.test_m6_memory::"
                    "test_memory_proposal_storage_identity_is_run_scoped",
                ),
            ),
            (
                "M6-004",
                ("src/telegram_userbot/domain/memory/validation.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_strict_parser_rejects_unknown_fields_and_validates_current_evidence",
                    "tests.unit.domain.test_m6_memory::"
                    "test_validator_rejects_malformed_evidence_and_unsafe_proposals",
                ),
            ),
            (
                "M6-005",
                ("src/telegram_userbot/domain/memory/validation.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_confidence_visual_only_and_low_confidence_never_auto_accept",
                    "tests.unit.domain.test_m6_memory::"
                    "test_validator_rejects_malformed_evidence_and_unsafe_proposals",
                ),
            ),
            (
                "M6-006",
                ("src/telegram_userbot/domain/memory/lifecycle.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_memory_store_acceptance_is_idempotent_and_forget_redacts_every_version",
                    "tests.unit.domain.test_m6_memory::"
                    "test_memory_store_supersede_creates_a_new_identity_and_version_ids_do_not_collide",
                ),
            ),
            (
                "M6-007",
                ("src/telegram_userbot/domain/memory/summary.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_summary_membership_period_and_late_source_invalidation",
                ),
            ),
            (
                "M6-008",
                ("src/telegram_userbot/domain/memory/embedding.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_embedding_shadow_activation_isolated_and_dimension_checked",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_embedding_indexes_and_dimension_binding_match_architecture",
                ),
            ),
            (
                "M6-009",
                (
                    "src/telegram_userbot/adapters/telegram_bot/memory_control.py",
                    "src/telegram_userbot/adapters/telegram_bot/memory_control_backend.py",
                ),
                (
                    "tests.unit.adapters.test_memory_control::"
                    "test_memory_control_is_private_metadata_only_and_uses_second_confirmation",
                    "tests.unit.adapters.test_memory_control::"
                    "test_memory_target_tokens_are_private_principal_bound_and_expiring",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_durable_control_backend_empty_scope_is_metadata_only",
                ),
            ),
            (
                "M6-010",
                ("src/telegram_userbot/domain/memory/reconciliation.py",),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_erasure_ledger_replays_without_reopening_derived_state_and_freshness_is_explicit",
                ),
            ),
            (
                "M6-011",
                (
                    "src/telegram_userbot/domain/memory/reconciliation.py",
                    "src/telegram_userbot/adapters/telegram_bot/memory_control_backend.py",
                ),
                (
                    "tests.unit.domain.test_m6_memory::"
                    "test_erasure_ledger_replays_without_reopening_derived_state_and_freshness_is_explicit",
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_status_marks_revision_or_token_threshold_as_stale",
                ),
            ),
            (
                "M6-012",
                (
                    "alembic/versions/0007_m6_memory_pipeline.py",
                    "alembic/versions/0009_m5_m6_account_scope_constraints.py",
                    "alembic/versions/0010_account_scope_refs.py",
                    "alembic/versions/0011_scope_context_erasure.py",
                    "alembic/versions/0012_worker_lease_retry.py",
                    "deploy/postgres/m6_roles.sql",
                    "docs/compatibility/m6.md",
                    "tests/integration/test_m6_account_scope_constraints.py",
                    "tests/integration/test_account_scope_references.py",
                    "TODO.md",
                    "docs/Implementation-Plan.md",
                ),
                (
                    "tests.integration.test_m6_memory_pipeline::"
                    "test_m6_roles_keep_control_out_of_derived_truth_and_allow_review_commands",
                    "tests.integration.test_m6_account_scope_constraints::"
                    "test_m5_m6_cross_account_references_are_rejected",
                    "tests.integration.test_account_scope_references::"
                    "test_remaining_account_owned_references_reject_cross_scope",
                    "tests.unit.test_m6_documentation_status::"
                    "test_m6_status_documents_are_consistent",
                ),
            ),
        )
        return [
            _tested_requirement(
                requirement_id,
                *evidence,
                test_ids=test_ids,
                junit_results=junit_results,
            )
            for requirement_id, evidence, test_ids in m6_mapping
        ]
    if milestone == "M7":
        junit_results = load_junit_results(root / ".artifacts/m7/junit.xml")
        m7_mapping: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
            (
                "M7-001",
                (
                    "src/telegram_userbot/domain/proactive/jobs.py",
                    "src/telegram_userbot/adapters/persistence/proactive_repository.py",
                    "alembic/versions/0008_m7_proactive_pipeline.py",
                    "alembic/versions/0012_worker_lease_retry.py",
                ),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_due_jobs_are_idempotent_and_expired_leases_requeue",
                    "tests.integration.test_m7_proactive_pipeline::"
                    "test_m7_concurrent_job_replay_and_same_owner_reclaim_are_fenced",
                ),
            ),
            (
                "M7-002",
                ("src/telegram_userbot/domain/proactive/rules.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_rules_materialize_allowlisted_facts_and_group_only_overlaps",
                ),
            ),
            (
                "M7-003",
                ("src/telegram_userbot/domain/proactive/time.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_dst_gap_fold_and_overnight_quiet_boundaries_are_explicit",
                ),
            ),
            (
                "M7-004",
                ("src/telegram_userbot/domain/proactive/time.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_dst_gap_fold_and_overnight_quiet_boundaries_are_explicit",
                ),
            ),
            (
                "M7-005",
                (
                    "src/telegram_userbot/domain/proactive/budget.py",
                    "src/telegram_userbot/adapters/persistence/proactive_repository.py",
                ),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_budget_is_atomic_idempotent_reaped_and_unknown_is_charged",
                    "tests.integration.test_m7_proactive_pipeline::"
                    "test_m7_concurrent_budget_replay_counts_one_hold",
                ),
            ),
            (
                "M7-006",
                ("src/telegram_userbot/domain/proactive/validation.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_agent_parser_is_strict_and_defer_stays_in_window",
                ),
            ),
            (
                "M7-007",
                ("src/telegram_userbot/domain/proactive/pipeline.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_context_is_text_only_and_candidate_local",
                ),
            ),
            (
                "M7-008",
                ("src/telegram_userbot/domain/proactive/rules.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_rules_cover_event_start_end_reconnect_and_filter_suppressions",
                ),
            ),
            (
                "M7-009",
                ("src/telegram_userbot/domain/proactive/pipeline.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_final_gate_maps_modes_and_rechecks_all_snapshots",
                ),
            ),
            (
                "M7-010",
                ("src/telegram_userbot/domain/proactive/budget.py",),
                (
                    "tests.unit.domain.test_m7_proactive::test_m7_budget_is_atomic_idempotent_reaped_and_unknown_is_charged",
                ),
            ),
            (
                "M7-011",
                (
                    "deploy/postgres/m7_roles.sql",
                    "src/telegram_userbot/adapters/persistence/schema.py",
                ),
                (
                    "tests.integration.test_m7_proactive_pipeline::test_m7_roles_keep_control_out_of_candidate_and_decision_truth",
                ),
            ),
            (
                "M7-012",
                (
                    "alembic/versions/0008_m7_proactive_pipeline.py",
                    "alembic/versions/0009_m5_m6_account_scope_constraints.py",
                    "alembic/versions/0010_account_scope_refs.py",
                    "alembic/versions/0011_scope_context_erasure.py",
                    "alembic/versions/0012_worker_lease_retry.py",
                    "deploy/postgres/m7_roles.sql",
                    "docs/compatibility/m7.md",
                    "tests/integration/test_m6_account_scope_constraints.py",
                    "tests/integration/test_account_scope_references.py",
                    "TODO.md",
                    "docs/Implementation-Plan.md",
                ),
                (
                    "tests.integration.test_m7_proactive_pipeline::test_m7_schema_inventory_constraints_and_head",
                    "tests.integration.test_m6_account_scope_constraints::"
                    "test_m5_m6_cross_account_references_are_rejected",
                    "tests.integration.test_account_scope_references::"
                    "test_remaining_account_owned_references_reject_cross_scope",
                    "tests.unit.test_m7_documentation_status::test_m7_status_documents_are_consistent",
                ),
            ),
        )
        return [
            _tested_requirement(
                requirement_id,
                *evidence,
                test_ids=test_ids,
                junit_results=junit_results,
            )
            for requirement_id, evidence, test_ids in m7_mapping
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
    uses_disposable_services = milestone in {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}
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
    parser.add_argument(
        "--milestone",
        choices=("M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"),
        default="M0",
    )
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
