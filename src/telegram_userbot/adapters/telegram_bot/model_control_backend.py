"""Durable Control Bot backend for non-secret model configuration."""

import hashlib
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid7

from telegram_userbot.adapters.persistence.model_repository import (
    ModelConfigurationRepository,
    ModelRepositoryError,
)
from telegram_userbot.adapters.persistence.records import ModelControlDraftRecord
from telegram_userbot.adapters.telegram_bot.model_control import (
    ControlSessionPrompt,
    IssuedKeyLaunch,
    ModelProfileSummary,
)
from telegram_userbot.adapters.webapp.auth import LaunchTokenCodec
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelConfigurationError,
    ModelProtocol,
)
from telegram_userbot.platform.network import (
    HostResolver,
    PublicEndpointPolicy,
    validate_endpoint,
)


class EndpointAdmission(Protocol):
    async def register(self, *, raw_url: str, admin_id: int) -> UUID: ...


class ModelCapabilityProbe(Protocol):
    async def probe(self, *, config: CanonicalModelConfig, now: datetime) -> ModelCapabilities: ...


class PublicEndpointAdmission:
    """Admit immutable public endpoints; private policies use a separate root-owned adapter."""

    def __init__(
        self,
        *,
        repository: ModelConfigurationRepository,
        policy: PublicEndpointPolicy,
        resolver: HostResolver,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._resolver = resolver

    async def register(self, *, raw_url: str, admin_id: int) -> UUID:
        validated = validate_endpoint(raw_url, policy=self._policy, resolver=self._resolver)
        endpoint_id = uuid7()
        return await self._repository.ensure_endpoint(
            endpoint_id=endpoint_id,
            label=urlsplit(validated.base_url).hostname or "model-endpoint",
            base_url=validated.base_url,
            network_policy_id=validated.policy_id,
            network_policy_version=validated.policy_version,
            network_category=validated.category,
            admin_id=admin_id,
        )


class DurableModelControlBackend:
    """Implements the Bot command contract inside the caller's database transaction."""

    def __init__(
        self,
        *,
        repository: ModelConfigurationRepository,
        endpoint_admission: EndpointAdmission,
        capability_probe: ModelCapabilityProbe,
        launch_tokens: LaunchTokenCodec,
        deployment_version: int,
    ) -> None:
        if deployment_version < 1:
            raise ValueError("deployment version must be positive")
        self._repository = repository
        self._endpoint_admission = endpoint_admission
        self._capability_probe = capability_probe
        self._launch_tokens = launch_tokens
        self._deployment_version = deployment_version

    async def list_profiles(self) -> tuple[ModelProfileSummary, ...]:
        records = await self._repository.list_control_profiles()
        return tuple(
            ModelProfileSummary(
                role=LogicalRole(item.logical_role),
                state=item.state,
                protocol=item.protocol,
                model_name=item.model_name,
                endpoint_label=item.endpoint_label,
                credential_status=item.credential_status,
                config_version=item.active_config_version_no,
                profile_version=item.version,
            )
            for item in records
        )

    async def start_config(
        self, *, admin_id: int, role: LogicalRole, now: datetime
    ) -> ControlSessionPrompt:
        record = await self._repository.start_control_session(
            session_id=uuid7(),
            draft_id=uuid7(),
            admin_id=admin_id,
            logical_role=role,
            now=now,
            expires_at=now + timedelta(minutes=15),
            session_nonce_hash=hashlib.sha256(secrets.token_bytes(32)).digest(),
        )
        return _prompt("endpoint", record.draft_version, role)

    async def apply_session_input(
        self, *, admin_id: int, value: str, now: datetime
    ) -> ControlSessionPrompt | None:
        record = await self._repository.get_control_session(admin_id=admin_id, now=now)
        if record is None or record.session_id is None or record.pending_field is None:
            return ControlSessionPrompt("none", "No active model configuration session.", 0)
        clean = value.strip()
        if not clean or len(clean) > 2048:
            return _prompt(
                record.pending_field, record.draft_version, LogicalRole(record.logical_role)
            )
        try:
            updates, next_field = await self._parse_input(
                record=record,
                value=clean,
                admin_id=admin_id,
            )
            candidate = _updated_record(record, updates)
            if next_field is None:
                _canonical_config(candidate)
        except ModelConfigurationError, ModelRepositoryError, ValueError:
            return ControlSessionPrompt(
                record.pending_field,
                f"Invalid {record.pending_field}; try again.",
                record.draft_version,
            )
        new_version = await self._repository.advance_control_session(
            session_id=record.session_id,
            draft_id=record.draft_id,
            expected_draft_version=record.draft_version,
            values=updates,
            next_field=next_field,
            now=now,
        )
        if new_version is None:
            return ControlSessionPrompt("conflict", "Draft changed; restart /model_config.", 0)
        if next_field is None:
            return None
        return _prompt(next_field, new_version, LogicalRole(record.logical_role))

    async def cancel(self, *, admin_id: int, now: datetime) -> bool:
        return await self._repository.cancel_control_session(admin_id=admin_id, now=now)

    async def validate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool:
        draft = await self._repository.get_latest_draft(
            admin_id=admin_id,
            logical_role=role,
            states=("editing",),
            now=now,
        )
        if draft is None or draft.pending_field is not None:
            return False
        try:
            config = _canonical_config(draft)
            capabilities = await self._capability_probe.probe(config=config, now=now)
            snapshot_id = uuid7()
            await self._repository.record_capabilities(
                snapshot_id=snapshot_id,
                endpoint_id=config.endpoint_id,
                protocol=config.protocol,
                model_name=config.model_name,
                capabilities=capabilities,
                metadata={},
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            )
            return await self._repository.validate_draft(
                draft_id=draft.draft_id,
                expected_draft_version=draft.draft_version,
                capability_snapshot_id=snapshot_id,
                now=now,
            )
        except ModelConfigurationError, ModelRepositoryError:
            return False

    async def activate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool:
        draft = await self._repository.get_latest_draft(
            admin_id=admin_id,
            logical_role=role,
            states=("validated",),
            now=now,
        )
        if draft is None:
            return False
        try:
            await self._repository.activate_draft(
                draft_id=draft.draft_id,
                expected_draft_version=draft.draft_version,
                admin_id=admin_id,
                now=now,
            )
        except ModelConfigurationError, ModelRepositoryError:
            return False
        return True

    async def issue_key_launch(
        self, *, admin_id: int, role: LogicalRole, action: str, now: datetime
    ) -> IssuedKeyLaunch:
        profile = next(
            (
                item
                for item in await self._repository.list_control_profiles()
                if item.logical_role == role.value
            ),
            None,
        )
        if profile is None:
            raise ModelRepositoryError("model profile is unavailable")
        if (action == "set" and profile.credential_status == "active") or (
            action in {"replace", "delete"} and profile.credential_status != "active"
        ):
            raise ModelRepositoryError("credential action is unavailable")
        issued = self._launch_tokens.issue()
        await self._repository.create_key_launch(
            launch_id=uuid7(),
            token_hash=issued.digest,
            admin_id=admin_id,
            profile_id=profile.id,
            action=action,
            deployment_version=self._deployment_version,
            expected_credential_version=profile.credential_version,
            now=now,
            expires_at=now + timedelta(minutes=5),
        )
        return IssuedKeyLaunch(role, action, issued.token)

    async def _parse_input(  # noqa: PLR0911,PLR0912 - ordered typed wizard
        self,
        *,
        record: ModelControlDraftRecord,
        value: str,
        admin_id: int,
    ) -> tuple[dict[str, object], str | None]:
        role = LogicalRole(record.logical_role)
        field = record.pending_field
        if field == "endpoint":
            endpoint_id = await self._endpoint_admission.register(raw_url=value, admin_id=admin_id)
            return {
                "endpoint_id": endpoint_id
            }, "protocol" if role is not LogicalRole.EMBEDDING else "model_name"
        if field == "protocol":
            protocol = ModelProtocol(value.lower())
            if protocol is ModelProtocol.EMBEDDING:
                raise ValueError("generation protocol required")
            return {"protocol": protocol.value, "protocol_options": {}}, "model_name"
        if field == "model_name":
            if not value or len(value) > 200 or any(ord(character) < 32 for character in value):
                raise ValueError("invalid model name")
            return {
                "model_name": value
            }, "timeout_seconds" if role is LogicalRole.EMBEDDING else "temperature"
        if field == "temperature":
            temperature = None if value.lower() in {"none", "default"} else float(value)
            if temperature is not None and not 0 <= temperature <= 2:
                raise ValueError("invalid temperature")
            return {"temperature": temperature}, "max_output_tokens"
        if field == "max_output_tokens":
            limit = int(value)
            if not 1 <= limit <= 1_000_000:
                raise ValueError("invalid output limit")
            return {"max_output_tokens": limit}, "timeout_seconds"
        if field == "timeout_seconds":
            timeout = int(value)
            if not 1 <= timeout <= 600:
                raise ValueError("invalid timeout")
            return {"timeout_seconds": timeout}, "enabled"
        if field == "enabled":
            normalized = value.lower()
            if normalized not in {"yes", "no", "true", "false", "on", "off"}:
                raise ValueError("invalid enabled flag")
            return {"enabled": normalized in {"yes", "true", "on"}}, "protocol_options"
        if field == "protocol_options":
            return {"protocol_options": _protocol_options(record, value)}, None
        raise ValueError("unknown control input field")


def _protocol_options(record: ModelControlDraftRecord, value: str) -> dict[str, object]:
    protocol = ModelProtocol(str(record.protocol))
    normalized = value.lower()
    if protocol is ModelProtocol.OPENAI_RESPONSES:
        if normalized == "none":
            return {}
        if normalized not in {"low", "medium", "high"}:
            raise ValueError("invalid reasoning effort")
        return {"reasoning_effort": normalized}
    if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        if normalized not in {"auto", "max_completion_tokens", "max_tokens"}:
            raise ValueError("invalid token field")
        return {"token_limit_field": normalized}
    if protocol is ModelProtocol.ANTHROPIC_MESSAGES:
        if normalized not in {"x_api_key", "bearer"}:
            raise ValueError("invalid auth scheme")
        return {"auth_scheme": normalized}
    if normalized == "auto":
        return {"dimensions": None}
    dimensions = int(value)
    if dimensions <= 0:
        raise ValueError("invalid dimensions")
    return {"dimensions": dimensions}


def _updated_record(
    record: ModelControlDraftRecord, updates: dict[str, object]
) -> ModelControlDraftRecord:
    return replace(record, **cast(Any, updates))


def _canonical_config(record: ModelControlDraftRecord) -> CanonicalModelConfig:
    if (
        record.endpoint_id is None
        or record.protocol is None
        or record.model_name is None
        or record.timeout_seconds is None
        or record.enabled is None
    ):
        raise ModelConfigurationError("model draft is incomplete")
    return CanonicalModelConfig(
        profile_id=record.profile_id,
        logical_role=LogicalRole(record.logical_role),
        endpoint_id=record.endpoint_id,
        credential_id=record.credential_id,
        protocol=ModelProtocol(record.protocol),
        model_name=record.model_name,
        temperature=record.temperature,
        max_output_tokens=record.max_output_tokens,
        timeout_seconds=record.timeout_seconds,
        enabled=record.enabled,
        protocol_options=record.protocol_options,
    )


def _prompt(field: str, version: int, role: LogicalRole) -> ControlSessionPrompt:
    prompts = {
        "endpoint": "Enter the canonical endpoint base URL (public HTTPS by default).",
        "protocol": ("Choose openai_responses, openai_chat_completions, or anthropic_messages."),
        "model_name": "Enter the provider model name.",
        "temperature": "Enter temperature 0..2, or none.",
        "max_output_tokens": "Enter the positive maximum output-token count.",
        "timeout_seconds": "Enter timeout seconds from 1 through 600.",
        "enabled": "Enable this profile after activation? yes or no.",
        "protocol_options": "Enter the controlled protocol option.",
    }
    prompt = prompts[field]
    if field == "protocol_options" and role is LogicalRole.EMBEDDING:
        prompt = "Enter embedding dimensions, or auto."
    return ControlSessionPrompt(field, prompt, version)
