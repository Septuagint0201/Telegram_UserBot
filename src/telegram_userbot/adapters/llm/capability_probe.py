"""Explicit-admin synthetic model capability probe with no private input.

The probe validates the exact endpoint and active credential in a short database
snapshot, closes that transaction, and only then performs one synthetic provider
request.  Capability ceilings are deployment safety ceilings, not claims that a
provider exposed authoritative model metadata.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import RowMapping, and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.llm.http_transport import ProviderHTTPTransport
from telegram_userbot.adapters.llm.protocols import (
    CanonicalContent,
    CanonicalEmbeddingRequest,
    CanonicalGenerationRequest,
    CanonicalMessage,
    CanonicalProtocolClient,
    ContentKind,
    ProviderProtocolError,
    ProviderTransport,
)
from telegram_userbot.adapters.persistence.schema import (
    model_credential_versions,
    model_credentials,
    model_endpoints,
)
from telegram_userbot.domain.model_config import (
    MAX_GENERATION_OUTPUT_TOKENS,
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelConfigurationError,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import (
    CredentialBinding,
    CredentialCryptoError,
    CredentialEnvelope,
    CredentialKeyring,
)
from telegram_userbot.platform.network import (
    EndpointPolicyError,
    HostResolver,
    PublicEndpointPolicy,
    SystemHostResolver,
    ValidatedEndpoint,
    validate_endpoint,
)
from telegram_userbot.platform.network.endpoint_policy import EndpointPolicy

_SYNTHETIC_MARKER = "SYNTHETIC_PROBE_OK_V1"
_SYNTHETIC_SYSTEM = "Synthetic capability probe with no user data."
_SYNTHETIC_ASSISTANT = "Synthetic prior assistant turn."
_SYNTHETIC_TEXT = "Synthetic capability probe. Return the requested marker only."
_MIN_GENERATION_INPUT_TOKENS = 2_048
_SYNTHETIC_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_STRUCTURED_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {"status": {"type": "string", "const": _SYNTHETIC_MARKER}},
    "required": ["status"],
    "additionalProperties": False,
}


class SyntheticCapabilityProbeError(ModelConfigurationError):
    """Stable, content-free synthetic probe failure."""


@dataclass(frozen=True, slots=True)
class SyntheticCapabilityCeilings:
    """Local safety ceilings applied after an exact synthetic request succeeds."""

    generation_context_tokens: int = 32_000
    generation_output_tokens: int = MAX_GENERATION_OUTPUT_TOKENS
    embedding_context_tokens: int = 8_192
    max_images_per_request: int = 10
    max_image_bytes_per_request: int = 20 * 1024 * 1024
    auto_image_tokens: int = 2_048

    def __post_init__(self) -> None:
        values = (
            self.generation_context_tokens,
            self.generation_output_tokens,
            self.embedding_context_tokens,
            self.max_images_per_request,
            self.max_image_bytes_per_request,
            self.auto_image_tokens,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("synthetic capability ceilings must be positive")
        if self.generation_context_tokens <= _MIN_GENERATION_INPUT_TOKENS:
            raise ValueError("synthetic generation context ceiling is too small")
        if (
            self.generation_output_tokens
            > self.generation_context_tokens - _MIN_GENERATION_INPUT_TOKENS
        ):
            raise ValueError("synthetic generation output ceiling is too large")


_DEFAULT_CEILINGS = SyntheticCapabilityCeilings()


@dataclass(frozen=True, slots=True)
class _ProbeSnapshot:
    endpoint: ValidatedEndpoint
    policy: EndpointPolicy
    binding: CredentialBinding
    envelope: CredentialEnvelope


class CapabilityTransportFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: ValidatedEndpoint,
        policy: EndpointPolicy,
        resolver: HostResolver,
    ) -> ProviderTransport: ...


def _default_transport_factory(
    *, endpoint: ValidatedEndpoint, policy: EndpointPolicy, resolver: HostResolver
) -> ProviderTransport:
    return ProviderHTTPTransport(
        endpoint=endpoint,
        policy=policy,
        resolver=resolver,
    )


class ProductionModelCapabilityProbe:
    """Run one bounded synthetic request only when Control invokes validation."""

    def __init__(  # noqa: PLR0913 - security and test seams remain explicit
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        keyring: CredentialKeyring,
        resolver: HostResolver,
        ceilings: SyntheticCapabilityCeilings = _DEFAULT_CEILINGS,
        transport_factory: CapabilityTransportFactory = _default_transport_factory,
        total_timeout_seconds: float = 20.0,
    ) -> None:
        if isinstance(total_timeout_seconds, bool) or not 0 < total_timeout_seconds <= 30:
            raise ValueError("capability probe deadline must be in (0, 30]")
        self._sessions = sessions
        self._keyring = keyring
        self._resolver = resolver
        self._ceilings = ceilings
        self._transport_factory = transport_factory
        self._total_timeout_seconds = total_timeout_seconds

    async def probe(
        self,
        *,
        config: CanonicalModelConfig,
        now: datetime,
    ) -> ModelCapabilities:
        if now.tzinfo is None or now.utcoffset() is None:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_TIME_INVALID")
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                snapshot = await self._snapshot(config)
                api_key = self._keyring.decrypt(
                    snapshot.envelope,
                    binding=snapshot.binding,
                )
                client = CanonicalProtocolClient(
                    self._transport_factory(
                        endpoint=snapshot.endpoint,
                        policy=snapshot.policy,
                        resolver=self._resolver,
                    )
                )
                if config.protocol is ModelProtocol.EMBEDDING:
                    return await self._probe_embedding(client, config, api_key)
                return await self._probe_generation(client, config, api_key)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_TIMEOUT") from None
        except (
            CredentialCryptoError,
            EndpointPolicyError,
            ProviderProtocolError,
            ModelConfigurationError,
            ValueError,
        ):
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_FAILED") from None
        except Exception:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_FAILED") from None

    async def _snapshot(self, config: CanonicalModelConfig) -> _ProbeSnapshot:
        credential = model_credential_versions.alias("probe_credential")
        statement = (
            select(
                model_endpoints.c.base_url,
                model_endpoints.c.network_policy_id,
                model_endpoints.c.network_policy_version,
                model_endpoints.c.network_category,
                credential.c.version_no,
                credential.c.algorithm,
                credential.c.key_version,
                credential.c.aad_schema_version,
                credential.c.nonce,
                credential.c.ciphertext,
                credential.c.secret_fingerprint,
            )
            .join(
                model_credentials,
                and_(
                    model_credentials.c.id == config.credential_id,
                    model_credentials.c.profile_id == config.profile_id,
                    model_credentials.c.status == "active",
                    model_credentials.c.active_version_no.is_not(None),
                ),
            )
            .join(
                credential,
                and_(
                    credential.c.credential_id == model_credentials.c.id,
                    credential.c.profile_id == model_credentials.c.profile_id,
                    credential.c.version_no == model_credentials.c.active_version_no,
                    credential.c.destroyed_at.is_(None),
                ),
            )
            .where(model_endpoints.c.id == config.endpoint_id)
        )
        async with self._sessions() as session, session.begin():
            row = (await session.execute(statement)).mappings().one_or_none()
        if row is None:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_SNAPSHOT_UNAVAILABLE")
        return self._snapshot_from_row(config, row)

    def _snapshot_from_row(
        self,
        config: CanonicalModelConfig,
        row: RowMapping,
    ) -> _ProbeSnapshot:
        if row["network_category"] != "public":
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_ENDPOINT_UNSUPPORTED")
        policy = PublicEndpointPolicy(
            cast(Any, row["network_policy_id"]),
            cast(int, row["network_policy_version"]),
        )
        endpoint = validate_endpoint(
            str(row["base_url"]),
            policy=policy,
            resolver=self._resolver,
        )
        if endpoint.category != "public" or endpoint.base_url != row["base_url"]:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_ENDPOINT_DRIFT")
        binding = CredentialBinding(
            config.logical_role,
            config.profile_id,
            config.credential_id,
            cast(int, row["version_no"]),
        )
        envelope = CredentialEnvelope(
            ciphertext=cast(bytes, row["ciphertext"]),
            nonce=cast(bytes, row["nonce"]),
            key_version=cast(int, row["key_version"]),
            aad_schema_version=cast(int, row["aad_schema_version"]),
            secret_fingerprint=cast(bytes, row["secret_fingerprint"]),
            algorithm=str(row["algorithm"]),
        )
        return _ProbeSnapshot(endpoint, policy, binding, envelope)

    async def _probe_generation(
        self,
        client: CanonicalProtocolClient,
        config: CanonicalModelConfig,
        api_key: SensitiveValue[str],
    ) -> ModelCapabilities:
        probe_config, chat_field = _resolved_probe_config(config)
        output_limit = cast(int, config.max_output_tokens)
        if (
            output_limit > self._ceilings.generation_output_tokens
            or output_limit
            > self._ceilings.generation_context_tokens - _MIN_GENERATION_INPUT_TOKENS
        ):
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_LOCAL_LIMIT_EXCEEDED")
        structured = config.logical_role in {
            LogicalRole.MEMORY_AGENT,
            LogicalRole.PROACTIVE_AGENT,
        }
        content = [CanonicalContent(ContentKind.TEXT, SensitiveValue(_SYNTHETIC_TEXT))]
        image = config.logical_role is LogicalRole.MAIN_AI
        if image:
            content.append(
                CanonicalContent(
                    ContentKind.IMAGE,
                    SensitiveValue("[SYNTHETIC_IMAGE]"),
                    image_detail="auto",
                    image_bytes=SensitiveValue(_SYNTHETIC_PNG),
                    image_mime="image/png",
                )
            )
        capabilities = ModelCapabilities(
            profile_kind=ProfileKind.GENERATION,
            supported_protocols=frozenset({config.protocol}),
            supports_text=True,
            supports_temperature=config.temperature is not None,
            supports_structured_output=structured,
            supports_streaming=False,
            supports_images=image,
            max_context_tokens=self._ceilings.generation_context_tokens,
            # The successful request proves only the exact configured value.  It
            # must never let an administrator raise the locally admitted ceiling.
            max_output_tokens_limit=min(
                output_limit,
                self._ceilings.generation_output_tokens,
            ),
            supported_input_roles=frozenset({"system", "user", "assistant"}),
            chat_token_limit_field=chat_field,
            supports_reasoning_effort=(
                config.protocol is ModelProtocol.OPENAI_RESPONSES
                and config.protocol_options.get("reasoning_effort") is not None
            ),
            max_images_per_request=self._ceilings.max_images_per_request,
            max_image_bytes_per_request=self._ceilings.max_image_bytes_per_request,
            auto_image_tokens=self._ceilings.auto_image_tokens,
            messages_auto_detail_equivalent=(
                image and config.protocol is ModelProtocol.ANTHROPIC_MESSAGES
            ),
        )
        normalized = await client.generate(
            config=probe_config,
            request=CanonicalGenerationRequest(
                (
                    CanonicalMessage(
                        "system",
                        (
                            CanonicalContent(
                                ContentKind.TEXT,
                                SensitiveValue(_SYNTHETIC_SYSTEM),
                            ),
                        ),
                    ),
                    CanonicalMessage(
                        "assistant",
                        (
                            CanonicalContent(
                                ContentKind.TEXT,
                                SensitiveValue(_SYNTHETIC_ASSISTANT),
                            ),
                        ),
                    ),
                    CanonicalMessage("user", tuple(content)),
                ),
                stream=False,
                response_schema=_STRUCTURED_SCHEMA if structured else None,
            ),
            api_key=api_key,
            capabilities=capabilities,
        )
        output = normalized.text.reveal_for_use()
        if structured:
            if _strict_probe_json(output) != {"status": _SYNTHETIC_MARKER}:
                raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_OUTPUT_INVALID")
        elif output.strip() != _SYNTHETIC_MARKER:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_OUTPUT_INVALID")
        return capabilities

    async def _probe_embedding(
        self,
        client: CanonicalProtocolClient,
        config: CanonicalModelConfig,
        api_key: SensitiveValue[str],
    ) -> ModelCapabilities:
        normalized = await client.embed(
            config=config,
            request=CanonicalEmbeddingRequest((SensitiveValue(_SYNTHETIC_TEXT),)),
            api_key=api_key,
        )
        if len(normalized.vectors) != 1:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_OUTPUT_INVALID")
        dimension = len(normalized.vectors[0])
        configured = config.protocol_options.get("dimensions")
        if configured is not None and configured != dimension:
            raise SyntheticCapabilityProbeError("MODEL_CAPABILITY_PROBE_OUTPUT_INVALID")
        return ModelCapabilities(
            profile_kind=ProfileKind.EMBEDDING,
            supported_protocols=frozenset({ModelProtocol.EMBEDDING}),
            supports_text=True,
            supports_temperature=False,
            supports_structured_output=False,
            supports_streaming=False,
            supports_images=False,
            max_context_tokens=self._ceilings.embedding_context_tokens,
            max_output_tokens_limit=None,
            supported_input_roles=frozenset({"user"}),
            embedding_dimensions=frozenset({dimension}),
        )


def _resolved_probe_config(
    config: CanonicalModelConfig,
) -> tuple[CanonicalModelConfig, str | None]:
    if config.protocol is not ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        return config, None
    raw = cast(str, config.protocol_options["token_limit_field"])
    resolved = "max_completion_tokens" if raw == "auto" else raw
    return replace(config, protocol_options={"token_limit_field": resolved}), resolved


def _strict_probe_json(raw: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate synthetic probe field")
            output[key] = value
        return output

    parsed = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: None)
    if not isinstance(parsed, dict):
        raise TypeError("synthetic probe output is not an object")
    return parsed


def build_production_capability_probe(  # noqa: PLR0913 - explicit production seams
    sessions: async_sessionmaker[AsyncSession],
    *,
    keyring: CredentialKeyring,
    resolver: HostResolver | None = None,
    ceilings: SyntheticCapabilityCeilings = _DEFAULT_CEILINGS,
    transport_factory: CapabilityTransportFactory = _default_transport_factory,
    total_timeout_seconds: float = 20.0,
) -> ProductionModelCapabilityProbe:
    """Build the reviewed public-endpoint probe without duplicating policy wiring."""

    return ProductionModelCapabilityProbe(
        sessions,
        keyring=keyring,
        resolver=resolver or SystemHostResolver(),
        ceilings=ceilings,
        transport_factory=transport_factory,
        total_timeout_seconds=total_timeout_seconds,
    )


__all__ = [
    "CapabilityTransportFactory",
    "ProductionModelCapabilityProbe",
    "SyntheticCapabilityCeilings",
    "SyntheticCapabilityProbeError",
    "build_production_capability_probe",
]
