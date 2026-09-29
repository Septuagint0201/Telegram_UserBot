"""Canonical-to-wire model protocol adapters with secret-safe request wrappers."""

import base64
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Never, Protocol, cast

from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    ModelCapabilities,
    ModelProtocol,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

_SAFE_FINISH_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


class ProviderProtocolError(RuntimeError):
    def __init__(  # noqa: PLR0913 - journal-safe provider metadata is explicit
        self,
        code: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        provider_request_id: str | None = None,
        request_may_have_been_sent: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = provider_request_id
        self.request_may_have_been_sent = request_may_have_been_sent


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
    provider_request_id: str | None = None
    retry_after_seconds: int | None = None


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
    provider_request_id: str | None = None
    http_status: int = 200
    retry_after_seconds: int | None = None


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
    provider_request_id: str | None = None
    http_status: int = 200
    retry_after_seconds: int | None = None


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


def build_generation_request(  # noqa: PLR0912 - three explicit provider wire contracts
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
            if capabilities is None or capabilities.chat_token_limit_field is None:
                raise ProviderProtocolError("CHAT_TOKEN_LIMIT_FIELD_UNRESOLVED")
            output_limit_field = capabilities.chat_token_limit_field
        body[cast(str, output_limit_field)] = config.max_output_tokens
        path = "/chat/completions"
        if request.stream:
            body["stream_options"] = {"include_usage": True}
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


def _error_for_status(response: ProviderWireResponse) -> ProviderProtocolError:
    if response.status_code == 429:
        return ProviderProtocolError(
            "PROVIDER_RATE_LIMITED",
            retryable=True,
            http_status=response.status_code,
            retry_after_seconds=response.retry_after_seconds,
            provider_request_id=response.provider_request_id,
        )
    if response.status_code in {408, 409, 425} or response.status_code >= 500:
        return ProviderProtocolError(
            "PROVIDER_TRANSIENT",
            retryable=True,
            http_status=response.status_code,
            retry_after_seconds=response.retry_after_seconds,
            provider_request_id=response.provider_request_id,
        )
    return ProviderProtocolError(
        "PROVIDER_REJECTED",
        http_status=response.status_code,
        retry_after_seconds=response.retry_after_seconds,
        provider_request_id=response.provider_request_id,
    )


def _malformed_embedding() -> Never:
    raise ValueError("embedding response is malformed")


def _usage(raw: Mapping[str, Any], protocol: ModelProtocol) -> ModelUsage:
    try:
        if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
            input_tokens = raw["prompt_tokens"]
            output_tokens = raw["completion_tokens"]
        elif protocol is ModelProtocol.EMBEDDING and "prompt_tokens" in raw:
            input_tokens = raw["prompt_tokens"]
            output_tokens = 0
        else:
            input_tokens = raw["input_tokens"]
            output_tokens = raw["output_tokens"]
        total_tokens = raw.get("total_tokens", input_tokens + output_tokens)
    except (KeyError, TypeError) as error:
        raise ProviderProtocolError("PROVIDER_USAGE_MALFORMED") from error
    if any(
        type(value) is not int or value < 0 for value in (input_tokens, output_tokens, total_tokens)
    ):
        raise ProviderProtocolError("PROVIDER_USAGE_MALFORMED")
    return ModelUsage(
        cast(int, input_tokens),
        cast(int, output_tokens),
        cast(int, total_tokens),
    )


def _stream_text(
    events: Sequence[SensitiveValue[Mapping[str, Any]]], protocol: ModelProtocol
) -> str:
    parts: list[str] = []
    for wrapped in events:
        event = wrapped.reveal_for_use()
        if not isinstance(event, Mapping):
            _stream_malformed()
        part = _stream_event_text(event, protocol)
        if part:
            parts.append(part)
    return "".join(parts)


def _stream_event_text(  # noqa: PLR0911,PLR0912 - three wire contracts stay explicit
    event: Mapping[str, Any], protocol: ModelProtocol
) -> str:
    if event.get("_transport_sse_done") is True:
        return ""
    if protocol is ModelProtocol.OPENAI_RESPONSES:
        if event.get("type") != "response.output_text.delta":
            return ""
        delta = event.get("delta")
        if not isinstance(delta, str):
            _stream_malformed()
        return delta
    if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        choices = event.get("choices", ())
        if not isinstance(choices, Sequence) or isinstance(choices, str | bytes):
            _stream_malformed()
        if not choices:
            return ""
        choice = choices[0]
        if not isinstance(choice, Mapping):
            _stream_malformed()
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            _stream_malformed()
        content = delta.get("content", "")
        if not isinstance(content, str):
            _stream_malformed()
        return content
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, Mapping):
        _stream_malformed()
    text = delta.get("text", "")
    if not isinstance(text, str):
        _stream_malformed()
    return text


def _stream_malformed() -> Never:
    raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED")


def _stream_body(  # noqa: PLR0912,PLR0915 - three explicit terminal contracts
    events: Sequence[SensitiveValue[Mapping[str, Any]]], protocol: ModelProtocol
) -> Mapping[str, Any]:
    """Extract only protocol-defined terminal metadata from ordered SSE events."""

    raw = [wrapped.reveal_for_use() for wrapped in events]
    try:
        if protocol is ModelProtocol.OPENAI_RESPONSES:
            completed = [event for event in raw if event.get("type") == "response.completed"]
            if len(completed) != 1:
                _stream_incomplete()
            completed_index = raw.index(completed[0])
            if any(
                event.get("_transport_sse_done") is not True for event in raw[completed_index + 1 :]
            ):
                _stream_incomplete()
            response = completed[0]["response"]
            if not isinstance(response, Mapping) or response.get("status") != "completed":
                _stream_incomplete()
            if not isinstance(response.get("usage"), Mapping):
                _stream_incomplete()
            return cast(Mapping[str, Any], response)

        if protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
            done = [event for event in raw if event.get("_transport_sse_done") is True]
            if len(done) != 1 or raw[-1].get("_transport_sse_done") is not True:
                _stream_incomplete()
            usage_events = [event for event in raw if isinstance(event.get("usage"), Mapping)]
            finish_reasons: list[object] = []
            for event in raw:
                choices = event.get("choices", ())
                if not isinstance(choices, Sequence) or isinstance(choices, str | bytes):
                    _stream_incomplete()
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        _stream_incomplete()
                    finish_reason = choice.get("finish_reason")
                    if finish_reason is not None:
                        finish_reasons.append(finish_reason)
            if len(usage_events) != 1 or len(finish_reasons) != 1:
                _stream_incomplete()
            return {
                "choices": [{"finish_reason": finish_reasons[0]}],
                "usage": usage_events[0]["usage"],
            }

        starts = [event for event in raw if event.get("type") == "message_start"]
        deltas = [event for event in raw if event.get("type") == "message_delta"]
        stops = [event for event in raw if event.get("type") == "message_stop"]
        non_transport_events = [
            event for event in raw if event.get("_transport_sse_done") is not True
        ]
        if len(starts) != 1 or len(deltas) != 1 or len(stops) != 1:
            _stream_incomplete()
        if not non_transport_events or non_transport_events[-1] is not stops[0]:
            _stream_incomplete()
        start_message = starts[0]["message"]
        if not isinstance(start_message, Mapping):
            _stream_incomplete()
        start_usage = start_message["usage"]
        delta_usage = deltas[0]["usage"]
        delta = deltas[0]["delta"]
        if not isinstance(delta, Mapping):
            _stream_incomplete()
        stop_reason = delta["stop_reason"]
        if not isinstance(start_usage, Mapping) or not isinstance(delta_usage, Mapping):
            _stream_incomplete()
        return {
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": start_usage["input_tokens"],
                "output_tokens": delta_usage["output_tokens"],
            },
        }
    except ProviderProtocolError:
        raise
    except AttributeError, IndexError, KeyError, TypeError:
        raise ProviderProtocolError("PROVIDER_STREAM_INCOMPLETE") from None


def _stream_incomplete() -> Never:
    raise ProviderProtocolError("PROVIDER_STREAM_INCOMPLETE")


def normalize_generation_response(
    protocol: ModelProtocol,
    response: ProviderWireResponse,
) -> NormalizedGeneration:
    if not 200 <= response.status_code < 300:
        raise _error_for_status(response)
    body = (
        _stream_body(response.stream_events, protocol)
        if response.stream_events
        else response.body.reveal_for_use()
    )
    if not isinstance(body, Mapping):
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
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
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED") from error
    if not isinstance(text_value, str) or not text_value:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
    return NormalizedGeneration(
        SensitiveValue(text_value),
        usage,
        finish_reason,
        response.provider_request_id,
        response.status_code,
        response.retry_after_seconds,
    )


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
        value = body.get("status", "completed")
    elif protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        value = body["choices"][0].get("finish_reason", "stop")
    else:
        value = body.get("stop_reason", "end_turn")
    if not isinstance(value, str) or _SAFE_FINISH_REASON.fullmatch(value) is None:
        raise ProviderProtocolError("PROVIDER_FINISH_REASON_MALFORMED")
    return value


def normalize_embedding_response(response: ProviderWireResponse) -> NormalizedEmbedding:
    if not 200 <= response.status_code < 300:
        raise _error_for_status(response)
    body = response.body.reveal_for_use()
    try:
        raw_items = body["data"]
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, str | bytes):
            _malformed_embedding()
        ordered = sorted(raw_items, key=lambda item: item["index"])
        indices = [item["index"] for item in ordered]
        if indices != list(range(len(ordered))) or any(
            isinstance(index, bool) or not isinstance(index, int) for index in indices
        ):
            _malformed_embedding()
        vectors_list: list[tuple[float, ...]] = []
        for item in ordered:
            raw_vector = item["embedding"]
            if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, str | bytes):
                _malformed_embedding()
            if any(type(raw_value) not in {int, float} for raw_value in raw_vector):
                _malformed_embedding()
            vector = tuple(float(raw_value) for raw_value in raw_vector)
            if any(not math.isfinite(value) for value in vector):
                _malformed_embedding()
            vectors_list.append(vector)
        vectors = tuple(vectors_list)
        usage = _usage(cast(Mapping[str, Any], body["usage"]), ModelProtocol.EMBEDDING)
        if not vectors or any(not vector for vector in vectors):
            _malformed_embedding()
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            _malformed_embedding()
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED") from error
    return NormalizedEmbedding(
        vectors,
        usage,
        response.provider_request_id,
        response.status_code,
        response.retry_after_seconds,
    )


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
        normalized = normalize_generation_response(
            config.protocol, await self._transport.send(wire)
        )
        output_limit = config.max_output_tokens
        if output_limit is None or normalized.usage.output_tokens > output_limit:
            raise ProviderProtocolError("PROVIDER_OUTPUT_LIMIT_EXCEEDED")
        return normalized

    async def embed(
        self,
        *,
        config: CanonicalModelConfig,
        request: CanonicalEmbeddingRequest,
        api_key: SensitiveValue[str],
    ) -> NormalizedEmbedding:
        wire = build_embedding_request(config, request, api_key)
        normalized = normalize_embedding_response(await self._transport.send(wire))
        configured_dimensions = config.protocol_options["dimensions"]
        if len(normalized.vectors) != len(request.inputs) or (
            configured_dimensions is not None
            and any(len(vector) != configured_dimensions for vector in normalized.vectors)
        ):
            raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
        return normalized
