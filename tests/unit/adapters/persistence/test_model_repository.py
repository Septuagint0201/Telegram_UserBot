from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.model_repository import (
    ModelConfigurationRepository,
    ModelRepositoryError,
)
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialKeyring

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeResult:
    def __init__(
        self, *, rows: Sequence[Any] = (), scalar: object = None, rowcount: int = 0
    ) -> None:
        self.rows = list(rows)
        self.scalar = scalar
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        return self.rows[0]

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeSession:
    def __init__(self, results: Sequence[FakeResult] = ()) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()


def repository(*results: FakeResult) -> tuple[ModelConfigurationRepository, FakeSession]:
    fake = FakeSession(results)
    return ModelConfigurationRepository(cast(AsyncSession, fake)), fake


def model_config(
    profile_id: UUID,
    credential_id: UUID,
    endpoint_id: UUID,
) -> CanonicalModelConfig:
    return CanonicalModelConfig(
        profile_id,
        LogicalRole.MAIN_AI,
        endpoint_id,
        credential_id,
        ModelProtocol.OPENAI_RESPONSES,
        "synthetic-model",
        0.3,
        256,
        30,
        True,
        {},
    )


def config_fields(value: CanonicalModelConfig) -> dict[str, object]:
    return {
        "endpoint_id": value.endpoint_id,
        "credential_id": value.credential_id,
        "protocol": value.protocol.value,
        "model_name": value.model_name,
        "temperature": value.temperature,
        "max_output_tokens": value.max_output_tokens,
        "timeout_seconds": value.timeout_seconds,
        "enabled": value.enabled,
        "protocol_options": dict(value.protocol_options),
    }


def capability_row(snapshot_id: UUID, endpoint_id: UUID) -> dict[str, object]:
    return {
        "id": snapshot_id,
        "endpoint_id": endpoint_id,
        "protocol": "openai_responses",
        "model_name": "synthetic-model",
        "supports_text": True,
        "supports_temperature": True,
        "supports_reasoning_effort": True,
        "supports_structured_output": True,
        "supports_stream": True,
        "supports_image": True,
        "max_context_tokens": 32_000,
        "max_output_tokens_limit": 4096,
        "max_images_per_request": 10,
        "max_image_bytes_per_request": 20 * 1024 * 1024,
        "auto_image_tokens": 2048,
        "messages_auto_detail_equivalent": False,
        "supported_input_roles": ["system", "user", "assistant"],
        "chat_token_limit_field": None,
        "embedding_dimensions": [],
    }


def control_draft_row(
    *,
    session_id: UUID | None,
    draft_id: UUID,
    profile_id: UUID,
    credential_id: UUID,
    pending_field: str | None = "endpoint",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "id": draft_id,
        "profile_id": profile_id,
        "logical_role": "main_ai",
        "profile_kind": "generation",
        "credential_id": credential_id,
        "expected_profile_version": 1,
        "draft_version": 1,
        "state": "editing",
        "pending_field": pending_field,
        "endpoint_id": None,
        "protocol": "openai_responses",
        "model_name": None,
        "temperature": 0.2,
        "max_output_tokens": 1024,
        "timeout_seconds": 30,
        "enabled": False,
        "protocol_options": {},
        "capability_snapshot_id": None,
        "expires_at": NOW + timedelta(minutes=15),
    }


def capabilities() -> ModelCapabilities:
    return ModelCapabilities(
        ProfileKind.GENERATION,
        frozenset({ModelProtocol.OPENAI_RESPONSES}),
        True,
        True,
        True,
        True,
        True,
        32_000,
        4096,
        frozenset({"system", "user", "assistant"}),
    )


def profile_row(profile_id: UUID, credential_id: UUID) -> dict[str, object]:
    return {
        "id": profile_id,
        "logical_role": "main_ai",
        "profile_kind": "generation",
        "state": "disabled",
        "active_config_version_no": None,
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "credential_id": credential_id,
        "credential_status": "missing",
        "credential_active_version_no": None,
        "latest_version_no": 0,
        "credential_version": 1,
    }


@pytest.mark.unit
async def test_bootstrap_list_endpoint_and_draft_crud() -> None:
    profile_ids = {role: uuid7() for role in LogicalRole}
    credential_ids = {role: uuid7() for role in LogicalRole}
    profile_id = profile_ids[LogicalRole.MAIN_AI]
    credential_id = credential_ids[LogicalRole.MAIN_AI]
    endpoint_id = uuid7()
    repo, fake = repository(
        *(FakeResult() for _ in range(8)),
        FakeResult(rows=[profile_row(profile_id, credential_id)]),
        FakeResult(),
        FakeResult(),
        FakeResult(scalar=2),
        FakeResult(scalar=None),
    )
    await repo.bootstrap_profiles(profile_ids=profile_ids, credential_ids=credential_ids)
    assert len(await repo.list_profiles()) == 1
    await repo.add_endpoint(
        endpoint_id=endpoint_id,
        label="synthetic",
        base_url="https://api.example.invalid/v1",
        network_policy_id=uuid7(),
        network_policy_version=1,
        network_category="public",
        admin_id=42,
    )
    config = model_config(profile_id, credential_id, endpoint_id)
    draft_id = uuid7()
    await repo.create_draft(
        draft_id=draft_id,
        profile_id=profile_id,
        expected_profile_version=1,
        admin_id=42,
        config=config,
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    assert (
        await repo.replace_draft(
            draft_id=draft_id, expected_draft_version=1, config=config, now=NOW
        )
        == 2
    )
    assert (
        await repo.replace_draft(
            draft_id=draft_id, expected_draft_version=2, config=config, now=NOW
        )
        is None
    )
    assert len(fake.statements) == 13


@pytest.mark.unit
async def test_control_profile_listing_and_endpoint_deduplication() -> None:
    profile_id, credential_id, endpoint_id = uuid7(), uuid7(), uuid7()
    control_profile = {
        **profile_row(profile_id, credential_id),
        "protocol": "openai_responses",
        "model_name": "synthetic-model",
        "endpoint_label": "synthetic",
    }
    existing_endpoint_id = uuid7()
    repo, _ = repository(
        FakeResult(rows=[control_profile]),
        FakeResult(scalar=endpoint_id),
        FakeResult(scalar=None),
        FakeResult(scalar=existing_endpoint_id),
    )
    profiles = await repo.list_control_profiles()
    assert profiles[0].model_name == "synthetic-model"
    network_policy_id = uuid7()
    assert (
        await repo.ensure_endpoint(
            endpoint_id=endpoint_id,
            label="synthetic",
            base_url="https://api.example.invalid/v1",
            network_policy_id=network_policy_id,
            network_policy_version=1,
            network_category="public",
            admin_id=42,
        )
        == endpoint_id
    )
    assert (
        await repo.ensure_endpoint(
            endpoint_id=uuid7(),
            label="synthetic",
            base_url="https://api.example.invalid/v1",
            network_policy_id=network_policy_id,
            network_policy_version=1,
            network_category="public",
            admin_id=42,
        )
        == existing_endpoint_id
    )


@pytest.mark.unit
async def test_control_session_start_copy_advance_and_lookup() -> None:
    profile_id, credential_id, draft_id, session_id = uuid7(), uuid7(), uuid7(), uuid7()
    old_draft_id, old_session_id = uuid7(), uuid7()
    profile = {
        "id": profile_id,
        "logical_role": "main_ai",
        "profile_kind": "generation",
        "active_config_version_no": 1,
        "version": 4,
        "credential_id": credential_id,
    }
    active = {
        **config_fields(model_config(profile_id, credential_id, uuid7())),
        "version_no": 1,
    }
    returned = control_draft_row(
        session_id=session_id,
        draft_id=draft_id,
        profile_id=profile_id,
        credential_id=credential_id,
    )
    repo, _ = repository(
        FakeResult(rows=[profile]),
        FakeResult(rows=[{"id": old_session_id, "draft_id": old_draft_id}]),
        FakeResult(),
        FakeResult(),
        FakeResult(rows=[active]),
        FakeResult(),
        FakeResult(),
        FakeResult(rows=[]),
        FakeResult(rows=[returned]),
        FakeResult(scalar=None),
        FakeResult(scalar=2),
        FakeResult(rowcount=1),
        FakeResult(rows=[{**returned, "pending_field": None}]),
    )
    opened = await repo.start_control_session(
        session_id=session_id,
        draft_id=draft_id,
        admin_id=42,
        logical_role=LogicalRole.MAIN_AI,
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
        session_nonce_hash=b"n" * 32,
    )
    assert opened.model_name == "synthetic-model"
    assert await repo.get_control_session(admin_id=42, now=NOW) is None
    loaded = await repo.get_control_session(admin_id=42, now=NOW)
    assert loaded is not None
    assert loaded.session_id == session_id
    assert (
        await repo.advance_control_session(
            session_id=session_id,
            draft_id=draft_id,
            expected_draft_version=1,
            values={"model_name": "changed"},
            next_field="temperature",
            now=NOW,
        )
        is None
    )
    assert (
        await repo.advance_control_session(
            session_id=session_id,
            draft_id=draft_id,
            expected_draft_version=1,
            values={"model_name": "changed"},
            next_field=None,
            now=NOW,
        )
        == 2
    )
    latest = await repo.get_latest_draft(
        admin_id=42,
        logical_role=LogicalRole.MAIN_AI,
        states=("editing",),
        now=NOW,
    )
    assert latest is not None
    assert latest.pending_field is None


@pytest.mark.unit
async def test_control_session_rejects_invalid_lifetime_and_update_fields() -> None:
    repo, _ = repository()
    with pytest.raises(ModelRepositoryError, match="lifetime"):
        await repo.start_control_session(
            session_id=uuid7(),
            draft_id=uuid7(),
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
            session_nonce_hash=b"n" * 32,
        )
    with pytest.raises(ModelRepositoryError, match="invalid"):
        await repo.advance_control_session(
            session_id=uuid7(),
            draft_id=uuid7(),
            expected_draft_version=1,
            values={"api_key": "forbidden"},
            next_field=None,
            now=NOW,
        )


@pytest.mark.unit
async def test_control_session_cancel_is_idempotent() -> None:
    missing, _ = repository(FakeResult(rows=[]))
    assert not await missing.cancel_control_session(admin_id=42, now=NOW)
    present, _ = repository(
        FakeResult(rows=[{"id": uuid7(), "draft_id": uuid7()}]),
        FakeResult(),
        FakeResult(),
    )
    assert await present.cancel_control_session(admin_id=42, now=NOW)


@pytest.mark.unit
async def test_credential_audit_accepts_only_content_free_actions() -> None:
    repo, fake = repository(FakeResult())
    await repo.audit_credential_mutation(
        admin_id=42,
        logical_role=LogicalRole.MAIN_AI,
        action="replace",
        result="success",
        credential_cas_version=3,
        request_id=uuid7(),
        now=NOW,
    )
    assert len(fake.statements) == 1
    with pytest.raises(ModelRepositoryError, match="audit"):
        await repo.audit_credential_mutation(
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            action="read",
            result="success",
            credential_cas_version=3,
            request_id=uuid7(),
            now=NOW,
        )


@pytest.mark.unit
async def test_bootstrap_and_draft_reject_identity_mismatch() -> None:
    repo, _ = repository()
    with pytest.raises(ModelRepositoryError, match="all logical"):
        await repo.bootstrap_profiles(
            profile_ids={LogicalRole.MAIN_AI: uuid7()},
            credential_ids={LogicalRole.MAIN_AI: uuid7()},
        )
    config = model_config(uuid7(), uuid7(), uuid7())
    with pytest.raises(ModelRepositoryError, match="draft profile"):
        await repo.create_draft(
            draft_id=uuid7(),
            profile_id=uuid7(),
            expected_profile_version=1,
            admin_id=42,
            config=config,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )


@pytest.mark.unit
async def test_capability_record_and_draft_validation_paths() -> None:
    profile_id, credential_id, endpoint_id, draft_id, snapshot_id = (
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
    )
    config = model_config(profile_id, credential_id, endpoint_id)
    draft = {
        "id": draft_id,
        "profile_id": profile_id,
        "logical_role": "main_ai",
        **config_fields(config),
    }
    repo, _ = repository(
        FakeResult(),
        FakeResult(rows=[]),
        FakeResult(rows=[draft]),
        FakeResult(rows=[capability_row(snapshot_id, endpoint_id)]),
        FakeResult(rowcount=1),
    )
    cap = capabilities()
    await repo.record_capabilities(
        snapshot_id=snapshot_id,
        endpoint_id=endpoint_id,
        protocol=ModelProtocol.OPENAI_RESPONSES,
        model_name="synthetic-model",
        capabilities=cap,
        metadata=cap.as_payload(),
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert not await repo.validate_draft(
        draft_id=draft_id,
        expected_draft_version=1,
        capability_snapshot_id=snapshot_id,
        now=NOW,
    )
    assert await repo.validate_draft(
        draft_id=draft_id,
        expected_draft_version=1,
        capability_snapshot_id=snapshot_id,
        now=NOW,
    )


@pytest.mark.unit
async def test_credential_set_delete_and_envelope_load_paths() -> None:
    profile_id, credential_id = uuid7(), uuid7()
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"k" * 32)},
    )
    credential = {
        "id": credential_id,
        "profile_id": profile_id,
        "version": 1,
        "active_version_no": None,
        "latest_version_no": 0,
    }
    repo, _ = repository(
        FakeResult(rows=[]),
        FakeResult(rows=[credential]),
        FakeResult(),
        FakeResult(),
        FakeResult(),
        FakeResult(rows=[]),
        FakeResult(rows=[{**credential, "status": "active", "active_version_no": 1}]),
        FakeResult(),
        FakeResult(),
        FakeResult(),
        FakeResult(),
        FakeResult(rows=[]),
        FakeResult(
            rows=[
                {
                    "credential_id": credential_id,
                    "profile_id": profile_id,
                    "version_no": 1,
                    "algorithm": "aes_256_gcm",
                    "key_version": 1,
                    "aad_schema_version": 1,
                    "nonce": b"n" * 12,
                    "ciphertext": b"c" * 16,
                    "secret_fingerprint": b"f" * 32,
                }
            ]
        ),
    )
    with pytest.raises(ModelRepositoryError, match="conflict"):
        await repo.set_credential(
            profile_id=profile_id,
            logical_role=LogicalRole.MAIN_AI,
            expected_credential_version=1,
            secret=SensitiveValue("SYNTHETIC_KEY"),
            keyring=keyring,
            now=NOW,
        )
    assert (
        await repo.set_credential(
            profile_id=profile_id,
            logical_role=LogicalRole.MAIN_AI,
            expected_credential_version=1,
            secret=SensitiveValue("SYNTHETIC_KEY"),
            keyring=keyring,
            now=NOW,
        )
        == 2
    )
    assert not await repo.delete_credential(
        profile_id=profile_id, expected_credential_version=1, now=NOW
    )
    assert await repo.delete_credential(
        profile_id=profile_id, expected_credential_version=1, now=NOW
    )
    assert (
        await repo.load_credential_envelope(profile_id=profile_id, credential_version_no=99) is None
    )
    loaded = await repo.load_credential_envelope(profile_id=profile_id, credential_version_no=1)
    assert loaded is not None
    assert loaded[0] == credential_id


@pytest.mark.unit
async def test_activate_and_snapshot_preserve_inflight_config_version() -> None:
    profile_id, credential_id, endpoint_id, draft_id, capability_id = (
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
    )
    config = model_config(profile_id, credential_id, endpoint_id)
    draft = {
        "id": draft_id,
        "profile_id": profile_id,
        "expected_profile_version": 1,
        "capability_snapshot_id": capability_id,
        **config_fields(config),
    }
    profile = {
        "id": profile_id,
        "version": 1,
        "active_config_version_no": None,
        "logical_role": "main_ai",
        "profile_kind": "generation",
    }
    credential = {
        "id": credential_id,
        "profile_id": profile_id,
        "active_version_no": 4,
    }
    repo, _ = repository(
        FakeResult(rows=[draft]),
        FakeResult(rows=[profile]),
        FakeResult(rows=[capability_row(capability_id, endpoint_id)]),
        FakeResult(rows=[credential]),
        FakeResult(),
        FakeResult(rowcount=1),
        FakeResult(),
    )
    activated = await repo.activate_draft(
        draft_id=draft_id, expected_draft_version=2, admin_id=42, now=NOW
    )
    assert activated.version_no == 1
    assert activated.credential_version_no == 4
    snapshot_row = {
        "id": activated.id,
        "profile_id": profile_id,
        "version_no": 1,
        "endpoint_id": endpoint_id,
        "credential_id": credential_id,
        "credential_active_version_no": 4,
        "capability_snapshot_id": capability_id,
        "logical_role": "main_ai",
        "profile_kind": "generation",
        **config_fields(config),
        "config_sha256": activated.config_sha256,
    }
    rotated_row = {**snapshot_row, "credential_active_version_no": 5}
    snapshot_repo, _ = repository(
        FakeResult(rows=[]), FakeResult(rows=[snapshot_row]), FakeResult(rows=[rotated_row])
    )
    assert await snapshot_repo.get_active_snapshot(profile_id) is None
    snapshot = await snapshot_repo.get_active_snapshot(profile_id)
    assert snapshot is not None
    assert snapshot.config_sha256 == activated.config_sha256
    rotated = await snapshot_repo.get_active_snapshot(profile_id)
    assert rotated is not None
    assert rotated.id == snapshot.id
    assert rotated.credential_version_no == 5


@pytest.mark.unit
async def test_activation_conflict_and_missing_credential_keep_old_active() -> None:
    profile_id, credential_id, endpoint_id = uuid7(), uuid7(), uuid7()
    draft_id = uuid7()
    config = model_config(profile_id, credential_id, endpoint_id)
    draft = {
        "id": draft_id,
        "profile_id": profile_id,
        "expected_profile_version": 1,
        "capability_snapshot_id": uuid7(),
        **config_fields(config),
    }
    missing, _ = repository(FakeResult(rows=[]))
    with pytest.raises(ModelRepositoryError, match="unavailable"):
        await missing.activate_draft(
            draft_id=draft_id, expected_draft_version=1, admin_id=42, now=NOW
        )
    conflict, _ = repository(
        FakeResult(rows=[draft]),
        FakeResult(
            rows=[
                {
                    "id": profile_id,
                    "version": 2,
                    "active_config_version_no": 8,
                    "logical_role": "main_ai",
                    "profile_kind": "generation",
                }
            ]
        ),
    )
    with pytest.raises(ModelRepositoryError, match="profile version"):
        await conflict.activate_draft(
            draft_id=draft_id, expected_draft_version=1, admin_id=42, now=NOW
        )
    no_key, _ = repository(
        FakeResult(rows=[draft]),
        FakeResult(
            rows=[
                {
                    "id": profile_id,
                    "version": 1,
                    "active_config_version_no": 8,
                    "logical_role": "main_ai",
                    "profile_kind": "generation",
                }
            ]
        ),
        FakeResult(rows=[capability_row(cast(UUID, draft["capability_snapshot_id"]), endpoint_id)]),
        FakeResult(rows=[]),
    )
    with pytest.raises(ModelRepositoryError, match="credential"):
        await no_key.activate_draft(
            draft_id=draft_id, expected_draft_version=1, admin_id=42, now=NOW
        )


@pytest.mark.unit
async def test_key_launch_claim_and_rate_limit_paths() -> None:
    profile_id, credential_id, launch_id = uuid7(), uuid7(), uuid7()
    launch_row = {
        "id": launch_id,
        "profile_id": profile_id,
        "logical_role": "main_ai",
        "credential_id": credential_id,
        "credential_status": "missing",
        "credential_version": 1,
    }
    repo, _ = repository(
        FakeResult(),
        FakeResult(rows=[]),
        FakeResult(rows=[launch_row]),
        FakeResult(rowcount=1),
    )
    await repo.create_key_launch(
        launch_id=launch_id,
        token_hash=b"t" * 32,
        admin_id=42,
        profile_id=profile_id,
        action="set",
        deployment_version=1,
        expected_credential_version=1,
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert (
        await repo.claim_key_launch(
            token_hash=b"t" * 32,
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            action="set",
            deployment_version=1,
            now=NOW,
        )
        is None
    )
    claimed = await repo.claim_key_launch(
        token_hash=b"t" * 32,
        admin_id=42,
        logical_role=LogicalRole.MAIN_AI,
        action="set",
        deployment_version=1,
        now=NOW,
    )
    assert claimed is not None
    assert claimed.credential_status == "missing"
    with pytest.raises(ModelRepositoryError, match="lifetime"):
        await repo.create_key_launch(
            launch_id=uuid7(),
            token_hash=b"bad",
            admin_id=42,
            profile_id=profile_id,
            action="set",
            deployment_version=1,
            expected_credential_version=1,
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

    principal = b"p" * 32
    new, _ = repository(FakeResult(rows=[]), FakeResult())
    assert await new.allow_key_attempt(principal_hash=principal, now=NOW)
    blocked, _ = repository(
        FakeResult(
            rows=[
                {
                    "blocked_until": NOW + timedelta(minutes=1),
                    "window_started_at": NOW,
                    "attempt_count": 10,
                }
            ]
        )
    )
    assert not await blocked.allow_key_attempt(principal_hash=principal, now=NOW)
    expired, _ = repository(
        FakeResult(
            rows=[
                {
                    "blocked_until": None,
                    "window_started_at": NOW - timedelta(minutes=16),
                    "attempt_count": 10,
                }
            ]
        ),
        FakeResult(),
    )
    assert await expired.allow_key_attempt(principal_hash=principal, now=NOW)
    denied, _ = repository(
        FakeResult(
            rows=[
                {
                    "blocked_until": None,
                    "window_started_at": NOW,
                    "attempt_count": 10,
                }
            ]
        ),
        FakeResult(),
    )
    assert not await denied.allow_key_attempt(principal_hash=principal, now=NOW)


@pytest.mark.unit
async def test_payload_parser_rejects_unknown_protocol_value() -> None:
    config = model_config(uuid7(), uuid7(), uuid7())
    draft: Mapping[str, object] = {
        "profile_id": config.profile_id,
        "logical_role": "main_ai",
        **config_fields(config),
        "protocol": "legacy_completions",
    }
    repo, _ = repository(FakeResult(rows=[draft]))

    with pytest.raises(ValueError, match="legacy_completions"):
        await repo.validate_draft(
            draft_id=uuid7(),
            expected_draft_version=1,
            capability_snapshot_id=uuid7(),
            now=NOW,
        )
