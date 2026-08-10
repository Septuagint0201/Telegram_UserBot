from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

import pytest

from telegram_userbot.adapters.persistence.model_repository import ModelConfigurationRepository
from telegram_userbot.adapters.persistence.records import (
    ModelConfigSnapshotRecord,
    ModelControlDraftRecord,
    ModelControlProfileRecord,
)
from telegram_userbot.adapters.telegram_bot import DurableModelControlBackend
from telegram_userbot.adapters.webapp import LaunchTokenCodec
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class EndpointAdmissionFake:
    def __init__(self) -> None:
        self.endpoint_id = uuid7()
        self.values: list[str] = []

    async def register(self, *, raw_url: str, admin_id: int) -> UUID:
        assert admin_id == 42
        self.values.append(raw_url)
        return self.endpoint_id


class CapabilityProbeFake:
    def __init__(self) -> None:
        self.configs: list[CanonicalModelConfig] = []

    async def probe(self, *, config: CanonicalModelConfig, now: datetime) -> ModelCapabilities:
        assert now == NOW
        self.configs.append(config)
        return ModelCapabilities(
            ProfileKind.GENERATION,
            frozenset({ModelProtocol.ANTHROPIC_MESSAGES}),
            True,
            True,
            True,
            True,
            True,
            32_000,
            4096,
            frozenset({"system", "user", "assistant"}),
        )


class RepositoryFake:
    def __init__(self) -> None:
        self.profile_id = uuid7()
        self.credential_id = uuid7()
        self.draft: ModelControlDraftRecord | None = None
        self.validated = False
        self.activated = False
        self.launch: dict[str, Any] | None = None

    async def list_control_profiles(self) -> tuple[ModelControlProfileRecord, ...]:
        return (
            ModelControlProfileRecord(
                self.profile_id,
                "main_ai",
                "generation",
                "disabled",
                None,
                1,
                self.credential_id,
                "missing",
                1,
                None,
                None,
                None,
            ),
        )

    async def start_control_session(self, **values: Any) -> ModelControlDraftRecord:
        role = values["logical_role"]
        embedding = role is LogicalRole.EMBEDDING
        self.draft = ModelControlDraftRecord(
            values["session_id"],
            values["draft_id"],
            self.profile_id,
            role.value,
            "embedding" if embedding else "generation",
            self.credential_id,
            1,
            1,
            "editing",
            "endpoint",
            None,
            "embedding" if embedding else "openai_responses",
            None,
            None if embedding else 0.2,
            None if embedding else 1024,
            30,
            False,
            {},
            None,
            values["expires_at"],
        )
        return self.draft

    async def get_control_session(self, **_: Any) -> ModelControlDraftRecord | None:
        return self.draft

    async def cancel_control_session(self, **_: Any) -> bool:
        self.draft = None
        return True

    async def advance_control_session(self, **values: Any) -> int | None:
        assert self.draft is not None
        version = self.draft.draft_version + 1
        self.draft = replace(
            self.draft,
            **values["values"],
            draft_version=version,
            pending_field=values["next_field"],
        )
        return version

    async def get_latest_draft(self, **values: Any) -> ModelControlDraftRecord | None:
        assert self.draft is not None
        return self.draft if self.draft.state in values["states"] else None

    async def record_capabilities(self, **_: Any) -> None:
        return None

    async def validate_draft(self, **_: Any) -> bool:
        assert self.draft is not None
        self.draft = replace(self.draft, state="validated", draft_version=9)
        self.validated = True
        return True

    async def activate_draft(self, **_: Any) -> ModelConfigSnapshotRecord:
        assert self.draft is not None
        assert self.draft.endpoint_id is not None
        self.activated = True
        return ModelConfigSnapshotRecord(
            uuid7(),
            self.profile_id,
            1,
            self.draft.endpoint_id,
            self.credential_id,
            1,
            uuid7(),
            {},
            b"h" * 32,
        )

    async def create_key_launch(self, **values: Any) -> None:
        self.launch = values


def backend(
    repository: RepositoryFake,
) -> tuple[DurableModelControlBackend, EndpointAdmissionFake, CapabilityProbeFake]:
    endpoint = EndpointAdmissionFake()
    probe = CapabilityProbeFake()
    return (
        DurableModelControlBackend(
            repository=cast(ModelConfigurationRepository, repository),
            endpoint_admission=endpoint,
            capability_probe=probe,
            launch_tokens=LaunchTokenCodec(SensitiveValue(b"p" * 32)),
            deployment_version=3,
        ),
        endpoint,
        probe,
    )


@pytest.mark.unit
async def test_durable_backend_completes_controlled_generation_draft_and_activation() -> None:
    repository = RepositoryFake()
    service, endpoint, probe = backend(repository)
    first = await service.start_config(admin_id=42, role=LogicalRole.MAIN_AI, now=NOW)
    assert first.field_name == "endpoint"
    inputs = (
        "https://api.example.invalid/v1",
        "anthropic_messages",
        "synthetic-model",
        "0.3",
        "512",
        "45",
        "yes",
        "x_api_key",
    )
    final = None
    for value in inputs:
        final = await service.apply_session_input(admin_id=42, value=value, now=NOW)
    assert final is None
    assert endpoint.values == ["https://api.example.invalid/v1"]
    assert await service.validate(admin_id=42, role=LogicalRole.MAIN_AI, now=NOW)
    assert probe.configs[0].protocol is ModelProtocol.ANTHROPIC_MESSAGES
    assert probe.configs[0].protocol_options["auth_scheme"] == "x_api_key"
    assert await service.activate(admin_id=42, role=LogicalRole.MAIN_AI, now=NOW)
    assert repository.validated
    assert repository.activated


@pytest.mark.unit
async def test_durable_backend_lists_status_and_issues_key_only_launch() -> None:
    repository = RepositoryFake()
    service, _, _ = backend(repository)
    profiles = await service.list_profiles()
    assert profiles[0].role is LogicalRole.MAIN_AI
    launch = await service.issue_key_launch(
        admin_id=42,
        role=LogicalRole.MAIN_AI,
        action="set",
        now=NOW,
    )
    assert len(launch.token.reveal_for_use()) == 43
    assert repository.launch is not None
    assert repository.launch["deployment_version"] == 3
    assert repository.launch["expires_at"] == NOW + timedelta(minutes=5)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("protocol", "option", "expected_options"),
    [
        ("openai_responses", "none", {}),
        (
            "openai_chat_completions",
            "auto",
            {"token_limit_field": "auto"},
        ),
    ],
)
async def test_durable_backend_completes_other_generation_protocol_wizards(
    protocol: str,
    option: str,
    expected_options: dict[str, object],
) -> None:
    repository = RepositoryFake()
    service, _, _ = backend(repository)
    await service.start_config(admin_id=42, role=LogicalRole.MAIN_AI, now=NOW)
    inputs = (
        "https://api.example.invalid/v1",
        protocol,
        "synthetic-model",
        "none",
        "256",
        "30",
        "off",
        option,
    )
    for value in inputs:
        await service.apply_session_input(admin_id=42, value=value, now=NOW)
    assert repository.draft is not None
    assert repository.draft.protocol_options == expected_options
    assert repository.draft.enabled is False


@pytest.mark.unit
async def test_durable_backend_embedding_wizard_skips_generation_fields() -> None:
    repository = RepositoryFake()
    service, _, _ = backend(repository)
    await service.start_config(admin_id=42, role=LogicalRole.EMBEDDING, now=NOW)
    prompts = [
        await service.apply_session_input(admin_id=42, value=value, now=NOW)
        for value in (
            "https://api.example.invalid/v1",
            "synthetic-embedding-model",
            "20",
            "yes",
            "auto",
        )
    ]
    assert [item.field_name for item in prompts[:-1] if item is not None] == [
        "model_name",
        "timeout_seconds",
        "enabled",
        "protocol_options",
    ]
    assert prompts[-1] is None
    assert repository.draft is not None
    assert repository.draft.temperature is None
    assert repository.draft.max_output_tokens is None
    assert repository.draft.protocol_options == {"dimensions": None}


@pytest.mark.unit
async def test_durable_backend_rejects_invalid_wizard_values_without_advancing() -> None:
    repository = RepositoryFake()
    service, _, _ = backend(repository)
    await service.start_config(admin_id=42, role=LogicalRole.MAIN_AI, now=NOW)
    pairs = (
        ("", "https://api.example.invalid/v1", "endpoint"),
        ("embedding", "openai_responses", "protocol"),
        ("bad\x01name", "synthetic-model", "model_name"),
        ("3", "default", "temperature"),
        ("0", "512", "max_output_tokens"),
        ("0", "30", "timeout_seconds"),
        ("maybe", "yes", "enabled"),
        ("ultra", "high", "protocol_options"),
    )
    for invalid, valid, field in pairs:
        rejected = await service.apply_session_input(admin_id=42, value=invalid, now=NOW)
        assert rejected is not None
        assert rejected.field_name == field
        await service.apply_session_input(admin_id=42, value=valid, now=NOW)
    assert repository.draft is not None
    assert repository.draft.pending_field is None
    assert repository.draft.protocol_options == {"reasoning_effort": "high"}
