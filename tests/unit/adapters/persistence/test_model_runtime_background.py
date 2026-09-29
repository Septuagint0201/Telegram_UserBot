from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.model_runtime import (
    ModelRuntimeRepository,
    ModelRuntimeSnapshotError,
)
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue


@pytest.mark.unit
def test_m8_replaces_candidate_key_after_dependent_foreign_keys_are_removed() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")

    decision_fk_drop = migration.index(
        "ALTER TABLE proactive_decisions DROP CONSTRAINT IF EXISTS\n"
        "          fk_proactive_decisions_candidate_scope;"
    )
    job_fk_drop = migration.index(
        "ALTER TABLE proactive_jobs DROP CONSTRAINT IF EXISTS\n"
        "          fk_proactive_jobs_candidate_scope;"
    )
    candidate_key_drop = migration.index(
        "ALTER TABLE proactive_candidates DROP CONSTRAINT IF EXISTS\n"
        "          uq_proactive_candidates_conversation_scope;"
    )
    candidate_key_add = migration.index(
        "ALTER TABLE proactive_candidates ADD CONSTRAINT\n"
        "          uq_proactive_candidates_conversation_scope UNIQUE"
    )
    decision_fk_add = migration.index(
        "ALTER TABLE proactive_decisions ADD CONSTRAINT\n"
        "          fk_proactive_decisions_candidate_scope"
    )
    job_fk_add = migration.index(
        "ALTER TABLE proactive_jobs ADD CONSTRAINT fk_proactive_jobs_candidate_scope"
    )

    assert decision_fk_drop < candidate_key_drop
    assert job_fk_drop < candidate_key_drop
    assert candidate_key_drop < candidate_key_add < decision_fk_add < job_fk_add


@pytest.mark.unit
def test_m8_credential_accessor_keeps_test_database_owner_when_migrator_role_is_absent() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")

    owner_transfer = migration.index(
        "ALTER FUNCTION public.get_model_credential_version_by_id(uuid, uuid, uuid)\n"
        "              OWNER TO telegram_userbot_migrator;"
    )
    role_guard = migration.index(
        "SELECT 1 FROM pg_roles WHERE rolname = 'telegram_userbot_migrator'"
    )
    public_revoke = migration.index(
        "REVOKE ALL ON FUNCTION public.get_model_credential_version_by_id(uuid, uuid, uuid)\n"
        "          FROM PUBLIC;"
    )

    assert role_guard < owner_transfer < public_revoke


@pytest.mark.unit
def test_m8_credential_accessor_scopes_generation_credentials_to_runtime_owner() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")

    app_scope = migration.index("session_user = 'telegram_userbot_app_login'")
    worker_scope = migration.index("session_user = 'telegram_userbot_worker_login'")
    app_purposes = migration.index("('conversation_reply', 'copilot_reactive_draft')")
    proactive_final = migration.index("run.purpose = 'proactive_final'")

    assert app_scope < app_purposes < worker_scope < proactive_final
    assert "SECURITY DEFINER changes current_user to the migrator" in migration
    assert (
        "'proactive_final')) OR\n             (run.logical_role = 'memory_agent'" not in migration
    )


@pytest.mark.parametrize("role", [LogicalRole.MEMORY_AGENT, LogicalRole.PROACTIVE_AGENT])
@pytest.mark.parametrize(
    "operation",
    ["prepare_or_load_generation", "prepare_generation", "load_prepared_generation"],
)
async def test_background_runtime_fails_closed_until_dedicated_pipeline_exists(
    role: LogicalRole, operation: str
) -> None:
    repository = ModelRuntimeRepository(None, new_uuid=lambda: UUID(int=1))  # type: ignore[arg-type]

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_BACKGROUND_RUNTIME_UNAVAILABLE"):
        await getattr(repository, operation)(
            run_id=UUID(int=2),
            expected_logical_role=role,
            input_hmac_key=SensitiveValue(b"k" * 32),
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
