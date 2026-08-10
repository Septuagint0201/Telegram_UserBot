from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from telegram_userbot.adapters.persistence.model_repository import (
    ModelConfigurationRepository,
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
pytestmark = pytest.mark.asyncio(loop_scope="session")


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


@pytest.mark.integration
async def test_three_generation_profiles_and_embedding_are_independent_and_versioned(
    db_session: AsyncSession,
) -> None:
    profile_ids = {role: uuid7() for role in LogicalRole}
    credential_ids = {role: uuid7() for role in LogicalRole}
    repository = ModelConfigurationRepository(db_session)
    await repository.bootstrap_profiles(
        profile_ids=profile_ids,
        credential_ids=credential_ids,
    )
    profiles = await repository.list_profiles()
    assert {item.logical_role for item in profiles} == {role.value for role in LogicalRole}
    assert sum(item.profile_kind == "generation" for item in profiles) == 3
    assert sum(item.profile_kind == "embedding" for item in profiles) == 1

    profile_id = profile_ids[LogicalRole.MAIN_AI]
    credential_id = credential_ids[LogicalRole.MAIN_AI]
    endpoint_id = uuid7()
    await repository.add_endpoint(
        endpoint_id=endpoint_id,
        label="synthetic-public",
        base_url="https://api.example.invalid/v1",
        network_policy_id=uuid7(),
        network_policy_version=1,
        network_category="public",
        admin_id=42,
    )
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"k" * 32)},
    )
    assert (
        await repository.set_credential(
            profile_id=profile_id,
            logical_role=LogicalRole.MAIN_AI,
            expected_credential_version=1,
            secret=SensitiveValue("SYNTHETIC_PROVIDER_KEY"),
            keyring=keyring,
            now=NOW,
        )
        == 2
    )
    config = CanonicalModelConfig(
        profile_id,
        LogicalRole.MAIN_AI,
        endpoint_id,
        credential_id,
        ModelProtocol.OPENAI_RESPONSES,
        "synthetic-model",
        0.2,
        512,
        30,
        True,
        {},
    )
    draft_id, snapshot_id = uuid7(), uuid7()
    await repository.create_draft(
        draft_id=draft_id,
        profile_id=profile_id,
        expected_profile_version=1,
        admin_id=42,
        config=config,
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    await repository.record_capabilities(
        snapshot_id=snapshot_id,
        endpoint_id=endpoint_id,
        protocol=ModelProtocol.OPENAI_RESPONSES,
        model_name="synthetic-model",
        capabilities=capabilities(),
        metadata={},
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert await repository.validate_draft(
        draft_id=draft_id,
        expected_draft_version=1,
        capability_snapshot_id=snapshot_id,
        now=NOW,
    )
    activated = await repository.activate_draft(
        draft_id=draft_id,
        expected_draft_version=2,
        admin_id=42,
        now=NOW,
    )
    snapshot = await repository.get_active_snapshot(profile_id)
    assert snapshot == activated
    assert snapshot.credential_version_no == 1
    assert (
        await repository.set_credential(
            profile_id=profile_id,
            logical_role=LogicalRole.MAIN_AI,
            expected_credential_version=2,
            secret=SensitiveValue("SYNTHETIC_REPLACEMENT_KEY"),
            keyring=keyring,
            now=NOW + timedelta(seconds=1),
        )
        == 3
    )
    rotated_snapshot = await repository.get_active_snapshot(profile_id)
    assert rotated_snapshot is not None
    assert rotated_snapshot.id == activated.id
    assert rotated_snapshot.credential_version_no == 2
    assert (
        await repository.load_credential_envelope(
            profile_id=profile_id,
            credential_version_no=1,
        )
        is not None
    )
    assert (
        await repository.load_credential_envelope(
            profile_id=profile_id,
            credential_version_no=2,
        )
        is not None
    )
    immutable_updates = (
        ("UPDATE model_endpoints SET label = 'changed' WHERE id = :id", endpoint_id),
        (
            "UPDATE model_capability_snapshots SET status = 'invalid' WHERE id = :id",
            snapshot_id,
        ),
        ("UPDATE model_config_versions SET version_no = 99 WHERE id = :id", activated.id),
    )
    for statement, row_id in immutable_updates:
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"id": row_id})
    assert await repository.delete_credential(
        profile_id=profile_id,
        expected_credential_version=3,
        now=NOW + timedelta(seconds=2),
    )
    assert (
        await repository.load_credential_envelope(
            profile_id=profile_id,
            credential_version_no=1,
        )
        is None
    )
    assert (
        await repository.set_credential(
            profile_id=profile_id,
            logical_role=LogicalRole.MAIN_AI,
            expected_credential_version=4,
            secret=SensitiveValue("SYNTHETIC_RECREATED_KEY"),
            keyring=keyring,
            now=NOW + timedelta(seconds=3),
        )
        == 5
    )
    recreated = await repository.list_profiles()
    main_profile = next(item for item in recreated if item.id == profile_id)
    assert main_profile.credential_active_version_no == 3


@pytest.mark.integration
async def test_launch_token_is_one_time_and_deployment_version_bound(
    db_session: AsyncSession,
) -> None:
    repository = ModelConfigurationRepository(db_session)
    profile_ids = {role: uuid7() for role in LogicalRole}
    credential_ids = {role: uuid7() for role in LogicalRole}
    await repository.bootstrap_profiles(profile_ids=profile_ids, credential_ids=credential_ids)
    launch_id = uuid7()
    await repository.create_key_launch(
        launch_id=launch_id,
        token_hash=b"l" * 32,
        admin_id=42,
        profile_id=profile_ids[LogicalRole.MAIN_AI],
        action="set",
        deployment_version=7,
        expected_credential_version=1,
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert (
        await repository.claim_key_launch(
            token_hash=b"l" * 32,
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            action="set",
            deployment_version=6,
            now=NOW,
        )
        is None
    )
    assert (
        await repository.claim_key_launch(
            token_hash=b"l" * 32,
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            action="set",
            deployment_version=7,
            now=NOW,
        )
        is not None
    )
    assert (
        await repository.claim_key_launch(
            token_hash=b"l" * 32,
            admin_id=42,
            logical_role=LogicalRole.MAIN_AI,
            action="set",
            deployment_version=7,
            now=NOW,
        )
        is None
    )


@pytest.mark.integration
async def test_control_configuration_session_is_durable_cas_and_cancellable(
    db_session: AsyncSession,
) -> None:
    repository = ModelConfigurationRepository(db_session)
    profile_ids = {role: uuid7() for role in LogicalRole}
    credential_ids = {role: uuid7() for role in LogicalRole}
    await repository.bootstrap_profiles(profile_ids=profile_ids, credential_ids=credential_ids)
    session_id, draft_id, endpoint_id = uuid7(), uuid7(), uuid7()
    await repository.add_endpoint(
        endpoint_id=endpoint_id,
        label="synthetic-control",
        base_url="https://control.example.invalid/v1",
        network_policy_id=uuid7(),
        network_policy_version=1,
        network_category="public",
        admin_id=42,
    )
    opened = await repository.start_control_session(
        session_id=session_id,
        draft_id=draft_id,
        admin_id=42,
        logical_role=LogicalRole.MAIN_AI,
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
        session_nonce_hash=b"s" * 32,
    )
    assert opened.pending_field == "endpoint"
    assert opened.protocol == "openai_responses"
    loaded = await repository.get_control_session(admin_id=42, now=NOW)
    assert loaded is not None
    assert loaded.session_id == session_id
    assert (
        await repository.advance_control_session(
            session_id=session_id,
            draft_id=draft_id,
            expected_draft_version=1,
            values={"endpoint_id": endpoint_id},
            next_field="protocol",
            now=NOW,
        )
        == 2
    )
    assert await repository.cancel_control_session(admin_id=42, now=NOW)
    assert await repository.get_control_session(admin_id=42, now=NOW) is None


@pytest.mark.integration
async def test_runtime_roles_cannot_select_ciphertext_directly(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT * FROM model_credential_versions"))
        finally:
            await transaction.rollback()
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
            assert await connection.scalar(text("SELECT COUNT(*) FROM model_credentials")) == 0
            result = await connection.execute(
                text("SELECT * FROM get_model_credential_version(:profile_id, 1)"),
                {"profile_id": uuid7()},
            )
            assert result.one_or_none() is None
        finally:
            await transaction.rollback()
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_control_runtime"))
            assert await connection.scalar(text("SELECT COUNT(*) FROM model_credentials")) == 0
        finally:
            await transaction.rollback()
