"""Admission-critical model capability snapshots and activation checks."""

from dataclasses import dataclass

from telegram_userbot.domain.model_config.config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelConfigurationError,
    ModelProtocol,
    ProfileKind,
    profile_kind_for,
)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    profile_kind: ProfileKind
    supported_protocols: frozenset[ModelProtocol]
    supports_text: bool
    supports_temperature: bool
    supports_structured_output: bool
    supports_streaming: bool
    supports_images: bool
    max_context_tokens: int
    max_output_tokens_limit: int | None
    supported_input_roles: frozenset[str]
    chat_token_limit_field: str | None = None
    embedding_dimensions: frozenset[int] = frozenset()
    supports_reasoning_effort: bool = False
    max_images_per_request: int = 10
    max_image_bytes_per_request: int = 20 * 1024 * 1024
    auto_image_tokens: int = 2_048
    messages_auto_detail_equivalent: bool = False

    def __post_init__(self) -> None:
        if not self.supported_protocols or self.max_context_tokens <= 0:
            raise ModelConfigurationError("capability snapshot is incomplete")
        if self.profile_kind is ProfileKind.GENERATION:
            if self.max_output_tokens_limit is None or self.max_output_tokens_limit <= 0:
                raise ModelConfigurationError("generation output capability is incomplete")
        elif self.max_output_tokens_limit is not None:
            raise ModelConfigurationError("embedding capability has generation output limit")
        if self.chat_token_limit_field not in {None, "max_completion_tokens", "max_tokens"}:
            raise ModelConfigurationError("invalid resolved Chat token field")
        if (
            min(
                self.max_images_per_request,
                self.max_image_bytes_per_request,
                self.auto_image_tokens,
            )
            <= 0
        ):
            raise ModelConfigurationError("image capability limits are incomplete")

    def as_payload(self) -> dict[str, object]:
        return {
            "profile_kind": self.profile_kind.value,
            "supported_protocols": sorted(item.value for item in self.supported_protocols),
            "supports_text": self.supports_text,
            "supports_temperature": self.supports_temperature,
            "supports_structured_output": self.supports_structured_output,
            "supports_streaming": self.supports_streaming,
            "supports_images": self.supports_images,
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens_limit": self.max_output_tokens_limit,
            "supported_input_roles": sorted(self.supported_input_roles),
            "chat_token_limit_field": self.chat_token_limit_field,
            "embedding_dimensions": sorted(self.embedding_dimensions),
            "max_images_per_request": self.max_images_per_request,
            "max_image_bytes_per_request": self.max_image_bytes_per_request,
            "auto_image_tokens": self.auto_image_tokens,
            "messages_auto_detail_equivalent": self.messages_auto_detail_equivalent,
        }


def validate_activation(config: CanonicalModelConfig, capabilities: ModelCapabilities) -> None:
    expected_kind = profile_kind_for(config.logical_role)
    if capabilities.profile_kind is not expected_kind:
        raise ModelConfigurationError("capability kind does not match profile")
    if config.protocol not in capabilities.supported_protocols:
        raise ModelConfigurationError("configured protocol is unsupported")
    if expected_kind is ProfileKind.GENERATION and not capabilities.supports_text:
        raise ModelConfigurationError("generation role requires text capability")
    if config.temperature is not None and not capabilities.supports_temperature:
        raise ModelConfigurationError("configured temperature is unsupported")
    if (
        config.protocol is ModelProtocol.OPENAI_RESPONSES
        and config.protocol_options.get("reasoning_effort") is not None
        and not capabilities.supports_reasoning_effort
    ):
        raise ModelConfigurationError("configured reasoning effort is unsupported")
    if (
        config.max_output_tokens is not None
        and capabilities.max_output_tokens_limit is not None
        and config.max_output_tokens > capabilities.max_output_tokens_limit
    ):
        raise ModelConfigurationError("configured output limit exceeds capability")
    if config.logical_role in {LogicalRole.MEMORY_AGENT, LogicalRole.PROACTIVE_AGENT} and not (
        capabilities.supports_structured_output
    ):
        raise ModelConfigurationError("role requires structured output capability")
    if config.protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        configured = config.protocol_options.get("token_limit_field")
        if configured == "auto" and capabilities.chat_token_limit_field is None:
            raise ModelConfigurationError("Chat token limit field must be resolved")
        if configured not in {"auto", capabilities.chat_token_limit_field}:
            raise ModelConfigurationError("Chat token limit field conflicts with capability")
    if config.logical_role is LogicalRole.EMBEDDING:
        dimensions = config.protocol_options.get("dimensions")
        if dimensions is not None and dimensions not in capabilities.embedding_dimensions:
            raise ModelConfigurationError("embedding dimensions are unsupported")
