"""Canonical-to-wire model protocol adapters with secret-safe request wrappers."""

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast

from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    ModelCapabilities,
    ModelProtocol,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


class ProviderProtocolError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ContentKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class CanonicalContent:
    kind: ContentKind
    value: SensitiveValue[str]
    image_detail: str | None = None
    image_bytes: SensitiveValue[bytes] | None = field(default=None, repr=False)
    image_mime: str | None = None

    def __post_init__(self) -> None:
        value = self.value.reveal_for_use()
        if not value or len(value) > 2_000_000:
            raise ProviderProtocolError("MODEL_INPUT_INVALID")
        if self.kind is ContentKind.TEXT:
            if (
                self.image_detail is not None
                or self.image_bytes is not None
                or self.image_mime is not None
            ):
                raise ProviderProtocolError("TEXT_DETAIL_FORBIDDEN")
            return
        if self.image_detail != "auto":
            raise ProviderProtocolError("IMAGE_DETAIL_MUST_BE_AUTO")
        if self.kind is ContentKind.IMAGE and (
            self.image_bytes is None
            or self.image_mime
            not in {
                "image/jpeg",
                "image/png",
                "image/webp",
            }
        ):
            raise ProviderProtocolError("IMAGE_PAYLOAD_INVALID")


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    role: str
    content: tuple[CanonicalContent, ...]

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant"} or not self.content:
            raise ProviderProtocolError("MODEL_MESSAGE_INVALID")


@dataclass(frozen=True, slots=True)
class CanonicalGenerationRequest:
    messages: tuple[CanonicalMessage, ...]
    stream: bool = False
    response_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ProviderProtocolError("MODEL_INPUT_EMPTY")
        if self.response_schema is not None:
            object.__setattr__(
                self, "response_schema", MappingProxyType(dict(self.response_schema))
            )


@dataclass(frozen=True, slots=True)
class ProviderWireRequest:
    method: str
    path: str
    headers: Mapping[str, SensitiveValue[str]] = field(repr=False)
    body: SensitiveValue[dict[str, Any]] = field(repr=False)
    timeout_seconds: int
    stream: bool


@dataclass(frozen=True, slots=True)
class ProviderWireResponse:
    status_code: int
    body: SensitiveValue[Mapping[str, Any]] = field(repr=False)
    stream_events: tuple[SensitiveValue[Mapping[str, Any]], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class NormalizedGeneration:
    text: SensitiveValue[str] = field(repr=False)
    usage: ModelUsage
    finish_reason: str


@dataclass(frozen=True, slots=True)
class CanonicalEmbeddingRequest:
    inputs: tuple[SensitiveValue[str], ...]

    def __post_init__(self) -> None:
        if not self.inputs or any(not item.reveal_for_use() for item in self.inputs):
            raise ProviderProtocolError("EMBEDDING_INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class NormalizedEmbedding:
    vectors: tuple[tuple[float, ...], ...]
    usage: ModelUsage


class ProviderTransport(Protocol):
    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse: ...


def _text(value: CanonicalContent) -> str:
    return value.value.reveal_for_use()


def _image_base64(content: CanonicalContent) -> str:
    if content.image_bytes is None:
        raise ProviderProtocolError("IMAGE_PAYLOAD_INVALID")
    return base64.b64encode(content.image_bytes.reveal_for_use()).decode("ascii")


def _image_data_url(content: CanonicalContent) -> str:
    return f"data:{content.image_mime};base64,{_image_base64(content)}"


def _image_mime(content: CanonicalContent) -> str:
    if content.image_mime is not None:
        return content.image_mime
    raise ProviderProtocolError("IMAGE_PAYLOAD_INVALID")


def _responses_content(content: CanonicalContent) -> dict[str, object]:
    if content.kind is ContentKind.TEXT:
        return {"type": "input_text", "text": _text(content)}
    return {"type": "input_image", "image_url": _image_data_url(content), "detail": "auto"}


def _chat_content(content: CanonicalContent) -> dict[str, object]:
    if content.kind is ContentKind.TEXT:
        return {"type": "text", "text": _text(content)}
    return {
        "type": "image_url",
        "image_url": {"url": _image_data_url(content), "detail": "auto"},
    }


def _messages_content(content: CanonicalContent) -> dict[str, object]:
    if content.kind is ContentKind.TEXT:
        return {"type": "text", "text": _text(content)}
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _image_mime(content),
            "data": _image_base64(content),
        },
    }


def _validate_image_capability(
    request: CanonicalGenerationRequest, capabilities: ModelCapabilities | None
) -> None:
    images = [
        part
        for message in request.messages
        for part in message.content
        if part.kind is ContentKind.IMAGE
    ]
    if not images:
        return
    if capabilities is None:
        raise ProviderProtocolError("IMAGE_CAPABILITY_REQUIRED")
    if not capabilities.supports_images or len(images) > capabilities.max_images_per_request:
        raise ProviderProtocolError("IMAGE_CAPABILITY_UNSUPPORTED")
    total_bytes = sum(
        len(part.image_bytes.reveal_for_use()) if part.image_bytes is not None else len(_text(part))
        for part in images
    )
    if total_bytes > capabilities.max_image_bytes_per_request:
        raise ProviderProtocolError("IMAGE_REQUEST_TOO_LARGE")


def _generation_headers(
    config: CanonicalModelConfig, api_key: SensitiveValue[str]
) -> Mapping[str, SensitiveValue[str]]:
    key = api_key.reveal_for_use()
    if not key:
        raise ProviderProtocolError("MODEL_CREDENTIAL_MISSING")
    if config.protocol is ModelProtocol.ANTHROPIC_MESSAGES:
        auth_scheme = config.protocol_options["auth_scheme"]
        authentication = (
            {"x-api-key": SensitiveValue(key)}
            if auth_scheme == "x_api_key"
            else {"authorization": SensitiveValue(f"Bearer {key}")}
        )
        return MappingProxyType(
            {
                **authentication,
                "anthropic-version": SensitiveValue(
                    cast(str, config.protocol_options["api_version"])
                ),
                "content-type": SensitiveValue("application/json"),
            }
        )
    return MappingProxyType(
        {
            "authorization": SensitiveValue(f"Bearer {key}"),
            "content-type": SensitiveValue("application/json"),
        }
    )


def build_generation_request(
    config: CanonicalModelConfig,
    request: CanonicalGenerationRequest,
    api_key: SensitiveValue[str],
    capabilities: ModelCapabilities | None = None,
) -> ProviderWireRequest:
    if config.protocol is ModelProtocol.EMBEDDING:
        raise ProviderProtocolError("GENERATION_PROTOCOL_REQUIRED")
    _validate_image_capability(request, capabilities)
    if (
        config.protocol is ModelProtocol.ANTHROPIC_MESSAGES
        and any(
            part.kind is ContentKind.IMAGE
            for message in request.messages
            for part in message.content
        )
        and capabilities is not None
        and not capabilities.messages_auto_detail_equivalent
    ):
        raise ProviderProtocolError("IMAGE_AUTO_DETAIL_UNSUPPORTED")
    if config.protocol is ModelProtocol.OPENAI_RESPONSES:
        body: dict[str, Any] = {
            "model": config.model_name,
            "input": [
                {
                    "role": message.role,
                    "content": [_responses_content(part) for part in message.content],
                }
                for message in request.messages
            ],
            "max_output_tokens": config.max_output_tokens,
            "stream": request.stream,
        }
        path = "/responses"
        if config.protocol_options.get("reasoning_effort") is not None:
            body["reasoning"] = {"effort": config.protocol_options["reasoning_effort"]}
        if request.response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "canonical_response",
                    "schema": dict(request.response_schema),
                    "strict": True,
                }
            }
    elif config.protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        body = {
            "model": config.model_name,
            "messages": [
                {
                    "role": message.role,
                    "content": [_chat_content(part) for part in message.content],
                }
                for message in request.messages
            ],
            "stream": request.stream,
        }
        output_limit_field = config.protocol_options["token_limit_field"]
        if output_limit_field == "auto":
            raise ProviderProtocolError("CHAT_TOKEN_LIMIT_FIELD_UNRESOLVED")
        body[cast(str, output_limit_field)] = config.max_output_tokens
        path = "/chat/completions"
        if request.response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "canonical_response",
                    "schema": dict(request.response_schema),
                    "strict": True,
                },
            }
    else:
        system_parts = [
            _messages_content(part)
            for message in request.messages
            if message.role in {"system", "developer"}
            for part in message.content
        ]
        body = {
            "model": config.model_name,
            "messages": [
                {
                    "role": message.role,
                    "content": [_messages_content(part) for part in message.content],
                }
                for message in request.messages
                if message.role in {"user", "assistant"}
            ],
            "max_tokens": config.max_output_tokens,
            "stream": request.stream,
        }
        if system_parts:
            body["system"] = system_parts
        path = cast(str, config.protocol_options["request_path"])
        if request.response_schema is not None:
            body["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": dict(request.response_schema),
                }
            }
    if config.temperature is not None:
        body["temperature"] = config.temperature
    return ProviderWireRequest(
        "POST",
        path,
        _generation_headers(config, api_key),
        SensitiveValue(body),
        config.timeout_seconds,
        request.stream,
    )


def build_embedding_request(
    config: CanonicalModelConfig,
    request: CanonicalEmbeddingRequest,
    api_key: SensitiveValue[str],
) -> ProviderWireRequest:
    if config.protocol is not ModelProtocol.EMBEDDING:
        raise ProviderProtocolError("EMBEDDING_PROTOCOL_REQUIRED")
    body: dict[str, object] = {
        "model": config.model_name,
        "input": [item.reveal_for_use() for item in request.inputs],
        "encoding_format": config.protocol_options["encoding_format"],
    }
    if config.protocol_options["dimensions"] is not None:
        body["dimensions"] = config.protocol_options["dimensions"]
    return ProviderWireRequest(
        "POST",
        "/embeddings",
        _generation_headers(config, api_key),
        SensitiveValue(body),
        config.timeout_seconds,
        False,
    )


def _error_for_status(status_code: int) -> ProviderProtocolError:
    if status_code == 429:
        return ProviderProtocolError("PROVIDER_RATE_LIMITED", retryable=True)
    if status_code in {408, 409, 425} or status_code >= 500:
        return ProviderProtocolError("PROVIDER_TRANSIENT", retryable=True)
    return ProviderProtocolError("PROVIDER_REJECTED")


def _usage(raw: Mapping[str, Any], protocol: ModelProtocol) -> ModelUsage:
    try:
        if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
            input_tokens = int(raw["prompt_tokens"])
            output_tokens = int(raw["completion_tokens"])
        else:
            input_tokens = int(raw["input_tokens"])
            output_tokens = int(raw["output_tokens"])
        total_tokens = int(raw.get("total_tokens", input_tokens + output_tokens))
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderProtocolError("PROVIDER_USAGE_MALFORMED") from error
    if min(input_tokens, output_tokens, total_tokens) < 0:
        raise ProviderProtocolError("PROVIDER_USAGE_MALFORMED")
    return ModelUsage(input_tokens, output_tokens, total_tokens)


def _stream_text(
    events: Sequence[SensitiveValue[Mapping[str, Any]]], protocol: ModelProtocol
) -> str:
    parts: list[str] = []
    try:
        for wrapped in events:
            event = wrapped.reveal_for_use()
            if protocol is ModelProtocol.OPENAI_RESPONSES:
                if event.get("type") == "response.output_text.delta":
                    parts.append(cast(str, event["delta"]))
            elif protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
                parts.append(cast(str, event["choices"][0]["delta"].get("content", "")))
            elif event.get("type") == "content_block_delta":
                parts.append(cast(str, event["delta"].get("text", "")))
    except (IndexError, KeyError, TypeError) as error:
        raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED") from error
    return "".join(parts)


def normalize_generation_response(
    protocol: ModelProtocol,
    response: ProviderWireResponse,
) -> NormalizedGeneration:
    if not 200 <= response.status_code < 300:
        raise _error_for_status(response.status_code)
    body = response.body.reveal_for_use()
    try:
        if response.stream_events:
            text_value = _stream_text(response.stream_events, protocol)
        elif protocol is ModelProtocol.OPENAI_RESPONSES:
            text_value = cast(str, body.get("output_text") or _responses_output_text(body))
        elif protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
            text_value = cast(str, body["choices"][0]["message"]["content"])
        else:
            text_value = "".join(
                cast(str, item["text"])
                for item in cast(Sequence[Mapping[str, object]], body["content"])
                if item.get("type") == "text"
            )
        usage = _usage(cast(Mapping[str, Any], body["usage"]), protocol)
        finish_reason = _finish_reason(body, protocol)
    except (IndexError, KeyError, TypeError) as error:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED") from error
    if not isinstance(text_value, str) or not text_value:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
    return NormalizedGeneration(SensitiveValue(text_value), usage, finish_reason)


def _responses_output_text(body: Mapping[str, Any]) -> str:
    return "".join(
        cast(str, content["text"])
        for output in cast(Sequence[Mapping[str, Any]], body["output"])
        if output.get("type") == "message"
        for content in cast(Sequence[Mapping[str, object]], output["content"])
        if content.get("type") == "output_text"
    )


def _finish_reason(body: Mapping[str, Any], protocol: ModelProtocol) -> str:
    if protocol is ModelProtocol.OPENAI_RESPONSES:
        return cast(str, body.get("status", "completed"))
    if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        return cast(str, body["choices"][0].get("finish_reason", "stop"))
    return cast(str, body.get("stop_reason", "end_turn"))


def normalize_embedding_response(response: ProviderWireResponse) -> NormalizedEmbedding:
    if not 200 <= response.status_code < 300:
        raise _error_for_status(response.status_code)
    body = response.body.reveal_for_use()
    try:
        ordered = sorted(body["data"], key=lambda item: item["index"])
        vectors = tuple(tuple(float(value) for value in item["embedding"]) for item in ordered)
        usage = _usage(cast(Mapping[str, Any], body["usage"]), ModelProtocol.EMBEDDING)
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED") from error
    if not vectors or any(not vector for vector in vectors):
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
    return NormalizedEmbedding(vectors, usage)


class CanonicalProtocolClient:
    def __init__(self, transport: ProviderTransport) -> None:
        self._transport = transport

    async def generate(
        self,
        *,
        config: CanonicalModelConfig,
        request: CanonicalGenerationRequest,
        api_key: SensitiveValue[str],
        capabilities: ModelCapabilities | None = None,
    ) -> NormalizedGeneration:
        wire = build_generation_request(config, request, api_key, capabilities)
        return normalize_generation_response(config.protocol, await self._transport.send(wire))

    async def embed(
        self,
        *,
        config: CanonicalModelConfig,
        request: CanonicalEmbeddingRequest,
        api_key: SensitiveValue[str],
    ) -> NormalizedEmbedding:
        wire = build_embedding_request(config, request, api_key)
        return normalize_embedding_response(await self._transport.send(wire))
