"""Canonical model configuration independent of provider wire field names."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID


class ModelConfigurationError(ValueError):
    """A stable, content-free configuration validation failure."""


class LogicalRole(StrEnum):
    MAIN_AI = "main_ai"
    MEMORY_AGENT = "memory_agent"
    PROACTIVE_AGENT = "proactive_agent"
    EMBEDDING = "embedding"


class ProfileKind(StrEnum):
    GENERATION = "generation"
    EMBEDDING = "embedding"


class ModelProtocol(StrEnum):
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    EMBEDDING = "embedding"


GENERATION_ROLES = frozenset(
    {LogicalRole.MAIN_AI, LogicalRole.MEMORY_AGENT, LogicalRole.PROACTIVE_AGENT}
)
GENERATION_PROTOCOLS = frozenset(
    {
        ModelProtocol.OPENAI_RESPONSES,
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        ModelProtocol.ANTHROPIC_MESSAGES,
    }
)


def profile_kind_for(role: LogicalRole) -> ProfileKind:
    return ProfileKind.EMBEDDING if role is LogicalRole.EMBEDDING else ProfileKind.GENERATION


def _normalized_options(protocol: ModelProtocol, raw: Mapping[str, object]) -> Mapping[str, object]:
    options = dict(raw)
    if protocol is ModelProtocol.OPENAI_RESPONSES:
        allowed = {"reasoning_effort"}
        unknown = set(options) - allowed
        effort = options.get("reasoning_effort")
        if unknown or (effort is not None and effort not in {"low", "medium", "high"}):
            raise ModelConfigurationError("invalid Responses protocol options")
    elif protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        allowed = {"token_limit_field"}
        unknown = set(options) - allowed
        token_field = options.get("token_limit_field", "auto")
        if unknown or token_field not in {"auto", "max_completion_tokens", "max_tokens"}:
            raise ModelConfigurationError("invalid Chat Completions protocol options")
        options["token_limit_field"] = token_field
    elif protocol is ModelProtocol.ANTHROPIC_MESSAGES:
        allowed = {"api_version", "auth_scheme", "request_path"}
        unknown = set(options) - allowed
        api_version = options.get("api_version", "2023-06-01")
        auth_scheme = options.get("auth_scheme", "x_api_key")
        request_path = options.get("request_path", "/messages")
        if unknown or not isinstance(api_version, str) or len(api_version) != 10:
            raise ModelConfigurationError("invalid Messages protocol options")
        if auth_scheme not in {"x_api_key", "bearer"} or request_path != "/messages":
            raise ModelConfigurationError("invalid Messages protocol options")
        options.update(
            api_version=api_version,
            auth_scheme=auth_scheme,
            request_path=request_path,
        )
    else:
        allowed = {"dimensions", "encoding_format", "batch_size"}
        unknown = set(options) - allowed
        dimensions = options.get("dimensions")
        encoding = options.get("encoding_format", "float")
        batch_size = options.get("batch_size", 64)
        if unknown:
            raise ModelConfigurationError("invalid Embedding protocol options")
        if dimensions is not None and (
            not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0
        ):
            raise ModelConfigurationError("invalid embedding dimensions")
        if encoding not in {"float", "base64"}:
            raise ModelConfigurationError("invalid embedding encoding")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 2048
        ):
            raise ModelConfigurationError("invalid embedding batch size")
        options.update(dimensions=dimensions, encoding_format=encoding, batch_size=batch_size)
    return MappingProxyType(options)


@dataclass(frozen=True, slots=True)
class CanonicalModelConfig:
    profile_id: UUID
    logical_role: LogicalRole
    endpoint_id: UUID
    credential_id: UUID
    protocol: ModelProtocol
    model_name: str
    temperature: float | None
    max_output_tokens: int | None
    timeout_seconds: int
    enabled: bool
    protocol_options: Mapping[str, object]

    def __post_init__(self) -> None:
        kind = profile_kind_for(self.logical_role)
        if kind is ProfileKind.GENERATION and self.protocol not in GENERATION_PROTOCOLS:
            raise ModelConfigurationError("generation profile requires a generation protocol")
        if kind is ProfileKind.EMBEDDING and self.protocol is not ModelProtocol.EMBEDDING:
            raise ModelConfigurationError("embedding profile requires embedding protocol")
        model_name = self.model_name.strip()
        if not model_name or len(model_name) > 200 or any(ord(ch) < 32 for ch in model_name):
            raise ModelConfigurationError("model name is invalid")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ModelConfigurationError("temperature must be between 0 and 2")
        if kind is ProfileKind.GENERATION:
            if (
                self.max_output_tokens is None
                or isinstance(self.max_output_tokens, bool)
                or not 1 <= self.max_output_tokens <= 1_000_000
            ):
                raise ModelConfigurationError("generation output limit is invalid")
        elif self.temperature is not None or self.max_output_tokens is not None:
            raise ModelConfigurationError("embedding profile cannot use generation parameters")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 600:
            raise ModelConfigurationError("timeout must be between 1 and 600 seconds")
        object.__setattr__(self, "model_name", model_name)
        object.__setattr__(
            self,
            "protocol_options",
            _normalized_options(self.protocol, self.protocol_options),
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "endpoint_id": str(self.endpoint_id),
            "credential_id": str(self.credential_id),
            "logical_role": self.logical_role.value,
            "protocol": self.protocol.value,
            "model_name": self.model_name,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "protocol_options": dict(self.protocol_options),
        }
