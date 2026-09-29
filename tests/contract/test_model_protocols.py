import asyncio
import base64
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast
from uuid import uuid7

import pytest

from telegram_userbot.adapters.llm import (
    CanonicalContent,
    CanonicalEmbeddingRequest,
    CanonicalGenerationRequest,
    CanonicalMessage,
    CanonicalProtocolClient,
    ContentKind,
    ProviderProtocolError,
    ProviderWireRequest,
    ProviderWireResponse,
    build_embedding_request,
    build_generation_request,
    normalize_embedding_response,
    normalize_generation_response,
)
from telegram_userbot.adapters.llm.protocols import (
    _image_base64,
    _image_mime,
    _stream_body,
    _stream_event_text,
    _stream_text,
)
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


def config(protocol: ModelProtocol) -> CanonicalModelConfig:
    role = LogicalRole.EMBEDDING if protocol is ModelProtocol.EMBEDDING else LogicalRole.MAIN_AI
    options: dict[str, object]
    if protocol is ModelProtocol.OPENAI_RESPONSES:
        options = {"reasoning_effort": "low"}
    elif protocol is ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        options = {"token_limit_field": "max_completion_tokens"}
    elif protocol is ModelProtocol.ANTHROPIC_MESSAGES:
        options = {"auth_scheme": "x_api_key"}
    else:
        options = {"dimensions": 2, "encoding_format": "float"}
    return CanonicalModelConfig(
        uuid7(),
        role,
        uuid7(),
        uuid7(),
        protocol,
        "synthetic-model",
        None if role is LogicalRole.EMBEDDING else 0.2,
        None if role is LogicalRole.EMBEDDING else 200,
        30,
        True,
        options,
    )


def generation(*, stream: bool = False) -> CanonicalGenerationRequest:
    return CanonicalGenerationRequest(
        (
            CanonicalMessage(
                "system",
                (CanonicalContent(ContentKind.TEXT, SensitiveValue("SYNTHETIC_SYSTEM")),),
            ),
            CanonicalMessage(
                "user",
                (
                    CanonicalContent(ContentKind.TEXT, SensitiveValue("SYNTHETIC_INPUT")),
                    CanonicalContent(
                        ContentKind.IMAGE,
                        SensitiveValue("media-object:synthetic"),
                        "auto",
                        SensitiveValue(b"SYNTHETIC_IMAGE_BYTES"),
                        "image/png",
                    ),
                ),
            ),
        ),
        stream=stream,
        response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )


def image_capabilities(protocol: ModelProtocol) -> ModelCapabilities:
    return ModelCapabilities(
        ProfileKind.GENERATION,
        frozenset({protocol}),
        True,
        True,
        True,
        True,
        True,
        32_000,
        4096,
        frozenset({"system", "developer", "user", "assistant"}),
        messages_auto_detail_equivalent=protocol is ModelProtocol.ANTHROPIC_MESSAGES,
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "path", "limit_field"),
    [
        (ModelProtocol.OPENAI_RESPONSES, "/responses", "max_output_tokens"),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            "/chat/completions",
            "max_completion_tokens",
        ),
        (ModelProtocol.ANTHROPIC_MESSAGES, "/messages", "max_tokens"),
    ],
)
def test_generation_wire_contracts_are_protocol_specific_and_secret_safe(
    protocol: ModelProtocol, path: str, limit_field: str
) -> None:
    wire = build_generation_request(
        config(protocol),
        generation(),
        SensitiveValue("SYNTHETIC_API_KEY"),
        image_capabilities(protocol),
    )
    body = wire.body.reveal_for_use()
    assert wire.path == path
    assert body[limit_field] == 200
    assert body["temperature"] == 0.2
    assert "SYNTHETIC_INPUT" not in repr(wire)
    assert "SYNTHETIC_API_KEY" not in repr(wire)
    assert not path.endswith("/completions") or path == "/chat/completions"
    serialized = str(body)
    assert base64.b64encode(b"SYNTHETIC_IMAGE_BYTES").decode() in serialized
    if protocol is ModelProtocol.ANTHROPIC_MESSAGES:
        assert "system" in body
        assert "output_config" in body
        assert "x-api-key" in wire.headers
    elif protocol is ModelProtocol.OPENAI_RESPONSES:
        assert body["reasoning"] == {"effort": "low"}
        assert "text" in body
    else:
        assert "response_format" in body


@pytest.mark.contract
def test_generation_wire_rejects_legacy_or_unresolved_contracts() -> None:
    chat = CanonicalModelConfig(
        uuid7(),
        LogicalRole.MAIN_AI,
        uuid7(),
        uuid7(),
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        "synthetic",
        0.1,
        10,
        10,
        True,
        {"token_limit_field": "auto"},
    )
    with pytest.raises(ProviderProtocolError, match="UNRESOLVED"):
        build_generation_request(
            chat,
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
            image_capabilities(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
        )


@pytest.mark.contract
def test_generation_wire_resolves_optional_chat_fields_and_omits_optional_values() -> None:
    chat = replace(
        config(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
        protocol_options={"token_limit_field": "auto"},
    )
    no_schema = replace(generation(), response_schema=None)
    request = build_generation_request(
        chat,
        no_schema,
        SensitiveValue("SYNTHETIC_KEY"),
        replace(
            image_capabilities(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
            chat_token_limit_field=cast(
                str,
                config(ModelProtocol.OPENAI_CHAT_COMPLETIONS).protocol_options["token_limit_field"],
            ),
        ),
    )
    body = request.body.reveal_for_use()
    assert body["max_completion_tokens"] == 200
    assert "response_format" not in body

    messages = replace(config(ModelProtocol.ANTHROPIC_MESSAGES), temperature=None)
    user_only = CanonicalGenerationRequest(
        (CanonicalMessage("user", (CanonicalContent(ContentKind.TEXT, SensitiveValue("input")),)),),
        response_schema=None,
    )
    messages_request = build_generation_request(
        messages,
        user_only,
        SensitiveValue("SYNTHETIC_KEY"),
        image_capabilities(ModelProtocol.ANTHROPIC_MESSAGES),
    )
    messages_body = messages_request.body.reveal_for_use()
    assert "system" not in messages_body
    assert "output_config" not in messages_body
    assert "temperature" not in messages_body


@pytest.mark.contract
def test_protocol_image_helpers_fail_closed_for_incomplete_image_objects() -> None:
    incomplete = cast(Any, type("IncompleteImage", (), {"image_bytes": None})())
    with pytest.raises(ProviderProtocolError, match="IMAGE_PAYLOAD_INVALID"):
        _image_base64(incomplete)

    incomplete_mime = cast(Any, type("IncompleteImage", (), {"image_mime": None})())
    with pytest.raises(ProviderProtocolError, match="IMAGE_PAYLOAD_INVALID"):
        _image_mime(incomplete_mime)


@pytest.mark.contract
def test_stream_text_rejects_non_mapping_and_malformed_protocol_events() -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_MALFORMED"):
        _stream_text((SensitiveValue(cast(Any, "not-a-mapping")),), ModelProtocol.OPENAI_RESPONSES)

    malformed = (
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, {"choices": "not-a-sequence"}),
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, {"choices": [42]}),
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, {"choices": [{"delta": "not-a-map"}]}),
        (ModelProtocol.ANTHROPIC_MESSAGES, {"type": "content_block_delta", "delta": "bad"}),
    )
    for protocol, event in malformed:
        with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_MALFORMED"):
            _stream_event_text(event, protocol)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "events"),
    [
        (ModelProtocol.OPENAI_RESPONSES, ({"type": "response.output_text.delta", "delta": "x"},)),
        (
            ModelProtocol.OPENAI_RESPONSES,
            ({"type": "response.completed", "response": "bad"},),
        ),
        (
            ModelProtocol.OPENAI_RESPONSES,
            (
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "usage": "bad"},
                },
            ),
        ),
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, ({"choices": "bad"},)),
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, ({"choices": [42]},)),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            ({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},),
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            ({"type": "message_start", "message": {"usage": {}}},),
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            (
                {"type": "message_start", "message": "bad"},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
                {"type": "message_stop"},
            ),
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            (
                {"type": "message_start", "message": {"usage": {}}},
                {"type": "message_delta", "delta": "bad", "usage": {}},
                {"type": "message_stop"},
            ),
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            (
                {"type": "message_start", "message": {"usage": "bad"}},
                {"type": "message_delta", "delta": {}, "usage": {}},
                {"type": "message_stop"},
            ),
        ),
    ],
)
def test_stream_terminal_metadata_rejects_incomplete_sequences(
    protocol: ModelProtocol, events: tuple[Mapping[str, object], ...]
) -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(tuple(SensitiveValue(event) for event in events), protocol)


@pytest.mark.contract
def test_stream_terminal_metadata_rejects_missing_message_stop_and_invalid_chat_metadata() -> None:
    responses = (
        SensitiveValue({"type": "message_start", "message": {"usage": {}}}),
        SensitiveValue({"type": "message_delta", "delta": {}, "usage": {}}),
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(responses, ModelProtocol.ANTHROPIC_MESSAGES)

    chat_events = (
        SensitiveValue({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        SensitiveValue({"_transport_sse_done": True}),
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(chat_events, ModelProtocol.OPENAI_CHAT_COMPLETIONS)


@pytest.mark.contract
def test_stream_terminal_metadata_rejects_validly_terminated_malformed_events() -> None:
    chat_done = (
        SensitiveValue({"choices": "bad"}),
        SensitiveValue({"_transport_sse_done": True}),
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(chat_done, ModelProtocol.OPENAI_CHAT_COMPLETIONS)

    chat_choice = (
        SensitiveValue({"choices": [42]}),
        SensitiveValue({"_transport_sse_done": True}),
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(chat_choice, ModelProtocol.OPENAI_CHAT_COMPLETIONS)

    messages_usage = (
        SensitiveValue({"type": "message_start", "message": {"usage": "bad"}}),
        SensitiveValue(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {},
            }
        ),
        SensitiveValue({"type": "message_stop"}),
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_INCOMPLETE"):
        _stream_body(messages_usage, ModelProtocol.ANTHROPIC_MESSAGES)


@pytest.mark.contract
def test_generation_and_embedding_normalizers_reject_non_mapping_payload_types() -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(200, SensitiveValue(cast(Any, "not-a-mapping"))),
        )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_embedding_response(
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {"data": "not-a-sequence", "usage": {"input_tokens": 1, "output_tokens": 0}}
                ),
            )
        )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_embedding_response(
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "data": [{"index": 0, "embedding": "not-a-vector"}],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    }
                ),
            )
        )
    with pytest.raises(ProviderProtocolError, match="DETAIL"):
        CanonicalContent(
            ContentKind.IMAGE,
            SensitiveValue("media-object:synthetic"),
            "high",
            SensitiveValue(b"SYNTHETIC_IMAGE_BYTES"),
            "image/png",
        )
    with pytest.raises(ProviderProtocolError, match="GENERATION_PROTOCOL"):
        build_generation_request(
            config(ModelProtocol.EMBEDDING),
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
            image_capabilities(ModelProtocol.OPENAI_RESPONSES),
        )


@pytest.mark.contract
def test_image_wire_fails_closed_without_capability_or_messages_auto_equivalence() -> None:
    with pytest.raises(ProviderProtocolError, match="CAPABILITY_REQUIRED"):
        build_generation_request(
            config(ModelProtocol.OPENAI_RESPONSES),
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
        )
    with pytest.raises(ProviderProtocolError, match="AUTO_DETAIL_UNSUPPORTED"):
        build_generation_request(
            config(ModelProtocol.ANTHROPIC_MESSAGES),
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
            image_capabilities(ModelProtocol.OPENAI_RESPONSES),
        )


@pytest.mark.contract
def test_canonical_request_and_image_capability_boundaries_fail_closed() -> None:
    text = CanonicalContent(ContentKind.TEXT, SensitiveValue("SYNTHETIC_INPUT"))
    text_only = CanonicalGenerationRequest((CanonicalMessage("user", (text,)),))

    with pytest.raises(ProviderProtocolError, match="MODEL_INPUT_INVALID"):
        CanonicalContent(ContentKind.TEXT, SensitiveValue(""))
    with pytest.raises(ProviderProtocolError, match="TEXT_DETAIL_FORBIDDEN"):
        CanonicalContent(ContentKind.TEXT, SensitiveValue("SYNTHETIC_INPUT"), "auto")
    with pytest.raises(ProviderProtocolError, match="IMAGE_PAYLOAD_INVALID"):
        CanonicalContent(
            ContentKind.IMAGE,
            SensitiveValue("media-object:synthetic"),
            "auto",
            None,
            "image/png",
        )
    with pytest.raises(ProviderProtocolError, match="MODEL_MESSAGE_INVALID"):
        CanonicalMessage("tool", (text,))
    with pytest.raises(ProviderProtocolError, match="MODEL_MESSAGE_INVALID"):
        CanonicalMessage("user", ())
    with pytest.raises(ProviderProtocolError, match="MODEL_INPUT_EMPTY"):
        CanonicalGenerationRequest(())
    with pytest.raises(ProviderProtocolError, match="EMBEDDING_INPUT_INVALID"):
        CanonicalEmbeddingRequest(())

    without_images = build_generation_request(
        config(ModelProtocol.OPENAI_RESPONSES),
        text_only,
        SensitiveValue("SYNTHETIC_KEY"),
    )
    assert without_images.path == "/responses"
    with pytest.raises(ProviderProtocolError, match="MODEL_CREDENTIAL_MISSING"):
        build_generation_request(
            config(ModelProtocol.OPENAI_RESPONSES), text_only, SensitiveValue("")
        )
    with pytest.raises(ProviderProtocolError, match="IMAGE_CAPABILITY_UNSUPPORTED"):
        build_generation_request(
            config(ModelProtocol.OPENAI_RESPONSES),
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
            replace(image_capabilities(ModelProtocol.OPENAI_RESPONSES), supports_images=False),
        )
    with pytest.raises(ProviderProtocolError, match="IMAGE_REQUEST_TOO_LARGE"):
        build_generation_request(
            config(ModelProtocol.OPENAI_RESPONSES),
            generation(),
            SensitiveValue("SYNTHETIC_KEY"),
            replace(
                image_capabilities(ModelProtocol.OPENAI_RESPONSES),
                max_image_bytes_per_request=1,
            ),
        )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "body", "expected"),
    [
        (
            ModelProtocol.OPENAI_RESPONSES,
            {
                "output_text": "SYNTHETIC_OUTPUT",
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            },
            "completed",
        ),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            {
                "choices": [{"message": {"content": "SYNTHETIC_OUTPUT"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
            "stop",
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            {
                "content": [{"type": "text", "text": "SYNTHETIC_OUTPUT"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
            "end_turn",
        ),
    ],
)
def test_generation_response_normalization(
    protocol: ModelProtocol, body: Mapping[str, object], expected: str
) -> None:
    normalized = normalize_generation_response(
        protocol,
        ProviderWireResponse(200, SensitiveValue(body)),
    )
    assert normalized.text.reveal_for_use() == "SYNTHETIC_OUTPUT"
    assert normalized.usage.total_tokens == 5
    assert normalized.finish_reason == expected
    assert "SYNTHETIC_OUTPUT" not in repr(normalized)


@pytest.mark.contract
def test_stream_error_and_malformed_response_contracts() -> None:
    response = ProviderWireResponse(
        200,
        SensitiveValue({"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}),
        (
            SensitiveValue({"type": "response.output_text.delta", "delta": "SYNTHETIC_"}),
            SensitiveValue({"type": "response.output_text.delta", "delta": "OUTPUT"}),
            SensitiveValue(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "total_tokens": 3,
                        },
                    },
                }
            ),
        ),
    )
    assert (
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES, response
        ).text.reveal_for_use()
        == "SYNTHETIC_OUTPUT"
    )
    for status, code, retryable in (
        (429, "RATE_LIMITED", True),
        (503, "TRANSIENT", True),
        (400, "REJECTED", False),
    ):
        with pytest.raises(ProviderProtocolError, match=code) as raised:
            normalize_generation_response(
                ModelProtocol.OPENAI_RESPONSES,
                ProviderWireResponse(status, SensitiveValue({})),
            )
        assert raised.value.retryable is retryable
    with pytest.raises(ProviderProtocolError, match="MALFORMED"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(200, SensitiveValue({"usage": {}})),
        )


@pytest.mark.contract
def test_chat_stream_requests_terminal_usage_chunk() -> None:
    stream_request = replace(generation(), stream=True)
    request = build_generation_request(
        config(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
        stream_request,
        SensitiveValue("SYNTHETIC_KEY"),
        image_capabilities(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
    )
    assert request.body.reveal_for_use()["stream_options"] == {"include_usage": True}


@pytest.mark.contract
def test_embedding_wire_and_response_contract() -> None:
    wire = build_embedding_request(
        config(ModelProtocol.EMBEDDING),
        CanonicalEmbeddingRequest((SensitiveValue("SYNTHETIC_A"), SensitiveValue("SYNTHETIC_B"))),
        SensitiveValue("SYNTHETIC_KEY"),
    )
    assert wire.path == "/embeddings"
    assert wire.body.reveal_for_use()["dimensions"] == 2
    normalized = normalize_embedding_response(
        ProviderWireResponse(
            200,
            SensitiveValue(
                {
                    "data": [
                        {"index": 1, "embedding": [0, 1]},
                        {"index": 0, "embedding": [1, 0]},
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 0},
                }
            ),
        )
    )
    assert normalized.vectors == ((1.0, 0.0), (0.0, 1.0))


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 1, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [1, float("nan")]}],
        [{"index": 0, "embedding": [True, 0]}],
        [{"index": 0, "embedding": ["1", 0]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1]}],
    ],
)
def test_embedding_response_rejects_noncontiguous_nonfinite_or_mismatched_vectors(
    data: list[dict[str, object]],
) -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_embedding_response(
            ProviderWireResponse(
                200,
                SensitiveValue({"data": data, "usage": {"input_tokens": 1, "output_tokens": 0}}),
            )
        )


@pytest.mark.contract
def test_stream_usage_and_embedding_error_boundaries_fail_closed() -> None:
    chat_stream = ProviderWireResponse(
        200,
        SensitiveValue(
            {
                "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        (
            SensitiveValue(
                {"choices": [{"delta": {"content": "SYNTHETIC_CHAT"}, "finish_reason": None}]}
            ),
            SensitiveValue({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            SensitiveValue(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ),
            SensitiveValue({"_transport_sse_done": True}),
        ),
    )
    assert (
        normalize_generation_response(
            ModelProtocol.OPENAI_CHAT_COMPLETIONS, chat_stream
        ).text.reveal_for_use()
        == "SYNTHETIC_CHAT"
    )
    messages_stream = ProviderWireResponse(
        200,
        SensitiveValue(
            {
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ),
        (
            SensitiveValue(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 1, "output_tokens": 0}},
                }
            ),
            SensitiveValue(
                {
                    "type": "content_block_delta",
                    "delta": {"text": "SYNTHETIC_MESSAGES"},
                }
            ),
            SensitiveValue(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                }
            ),
            SensitiveValue({"type": "message_stop"}),
        ),
    )
    assert (
        normalize_generation_response(
            ModelProtocol.ANTHROPIC_MESSAGES, messages_stream
        ).text.reveal_for_use()
        == "SYNTHETIC_MESSAGES"
    )

    with pytest.raises(ProviderProtocolError, match="PROVIDER_USAGE_MALFORMED"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "output_text": "SYNTHETIC_OUTPUT",
                        "usage": {"input_tokens": -1, "output_tokens": 1},
                    }
                ),
            ),
        )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
            ),
        )
    with pytest.raises(ProviderProtocolError, match="EMBEDDING_PROTOCOL_REQUIRED"):
        build_embedding_request(
            config(ModelProtocol.OPENAI_RESPONSES),
            CanonicalEmbeddingRequest((SensitiveValue("SYNTHETIC_INPUT"),)),
            SensitiveValue("SYNTHETIC_KEY"),
        )
    embedding_without_dimensions = replace(
        config(ModelProtocol.EMBEDDING),
        protocol_options={"dimensions": None, "encoding_format": "float"},
    )
    assert (
        "dimensions"
        not in build_embedding_request(
            embedding_without_dimensions,
            CanonicalEmbeddingRequest((SensitiveValue("SYNTHETIC_INPUT"),)),
            SensitiveValue("SYNTHETIC_KEY"),
        ).body.reveal_for_use()
    )
    with pytest.raises(ProviderProtocolError, match="PROVIDER_TRANSIENT"):
        normalize_embedding_response(ProviderWireResponse(503, SensitiveValue({})))
    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        normalize_embedding_response(
            ProviderWireResponse(
                200,
                SensitiveValue({"data": [], "usage": {"input_tokens": 1, "output_tokens": 0}}),
            )
        )


@pytest.mark.contract
@pytest.mark.parametrize("invalid_usage", [True, "1", 1.0])
def test_usage_requires_exact_nonnegative_integers(invalid_usage: object) -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_USAGE_MALFORMED"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "output_text": "SYNTHETIC_OUTPUT",
                        "status": "completed",
                        "usage": {
                            "input_tokens": invalid_usage,
                            "output_tokens": 1,
                        },
                    }
                ),
            ),
        )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "body"),
    [
        (
            ModelProtocol.OPENAI_RESPONSES,
            {
                "output": [{"type": "message", "content": 42}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            {
                "choices": "not-a-choice-array",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            {
                "content": 42,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ],
)
def test_three_protocol_nonstream_malformed_payloads_fail_with_stable_error(
    protocol: ModelProtocol,
    body: Mapping[str, object],
) -> None:
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_RESPONSE_MALFORMED$"):
        normalize_generation_response(protocol, ProviderWireResponse(200, SensitiveValue(body)))


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "response"),
    [
        (
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(
                200,
                SensitiveValue({}),
                (
                    SensitiveValue({"type": "response.output_text.delta", "delta": 42}),
                    SensitiveValue(
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        }
                    ),
                ),
            ),
        ),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            ProviderWireResponse(
                200,
                SensitiveValue({}),
                (
                    SensitiveValue(
                        {"choices": [{"delta": {"content": 42}, "finish_reason": None}]}
                    ),
                    SensitiveValue({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
                    SensitiveValue(
                        {
                            "choices": [],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                        }
                    ),
                    SensitiveValue({"_transport_sse_done": True}),
                ),
            ),
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            ProviderWireResponse(
                200,
                SensitiveValue({}),
                (
                    SensitiveValue(
                        {
                            "type": "message_start",
                            "message": {"usage": {"input_tokens": 1, "output_tokens": 0}},
                        }
                    ),
                    SensitiveValue({"type": "content_block_delta", "delta": {"text": 42}}),
                    SensitiveValue(
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 1},
                        }
                    ),
                    SensitiveValue({"type": "message_stop"}),
                ),
            ),
        ),
    ],
)
def test_three_protocol_streams_map_malformed_deltas_to_stable_errors(
    protocol: ModelProtocol,
    response: ProviderWireResponse,
) -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_STREAM_MALFORMED"):
        normalize_generation_response(protocol, response)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("protocol", "body"),
    [
        (
            ModelProtocol.OPENAI_RESPONSES,
            {
                "output_text": "SYNTHETIC_OUTPUT",
                "status": True,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            {
                "choices": [{"message": {"content": "SYNTHETIC_OUTPUT"}, "finish_reason": ""}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            {
                "content": [{"type": "text", "text": "SYNTHETIC_OUTPUT"}],
                "stop_reason": "contains whitespace",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ],
)
def test_finish_reason_is_a_bounded_content_free_string(
    protocol: ModelProtocol,
    body: Mapping[str, object],
) -> None:
    with pytest.raises(ProviderProtocolError, match="PROVIDER_FINISH_REASON_MALFORMED"):
        normalize_generation_response(protocol, ProviderWireResponse(200, SensitiveValue(body)))


class BlockingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        assert request.path == "/responses"
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class StaticTransport:
    def __init__(self, response: ProviderWireResponse) -> None:
        self.response = response

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        del request
        return self.response


@pytest.mark.contract
@pytest.mark.asyncio
async def test_canonical_client_enforces_requested_generation_output_ceiling() -> None:
    client = CanonicalProtocolClient(
        StaticTransport(
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "output_text": "SYNTHETIC_OUTPUT",
                        "status": "completed",
                        "usage": {"input_tokens": 1, "output_tokens": 201},
                    }
                ),
            )
        )
    )

    with pytest.raises(ProviderProtocolError, match="PROVIDER_OUTPUT_LIMIT_EXCEEDED"):
        await client.generate(
            config=config(ModelProtocol.OPENAI_RESPONSES),
            request=generation(),
            api_key=SensitiveValue("SYNTHETIC_KEY"),
            capabilities=image_capabilities(ModelProtocol.OPENAI_RESPONSES),
        )


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1, 0]}],
        [
            {"index": 0, "embedding": [1, 0, 0]},
            {"index": 1, "embedding": [0, 1, 0]},
        ],
    ],
)
async def test_canonical_client_binds_embedding_count_and_dimensions_to_request(
    data: list[dict[str, object]],
) -> None:
    client = CanonicalProtocolClient(
        StaticTransport(
            ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "data": data,
                        "usage": {"input_tokens": 2, "output_tokens": 0},
                    }
                ),
            )
        )
    )

    with pytest.raises(ProviderProtocolError, match="PROVIDER_RESPONSE_MALFORMED"):
        await client.embed(
            config=config(ModelProtocol.EMBEDDING),
            request=CanonicalEmbeddingRequest(
                (SensitiveValue("SYNTHETIC_A"), SensitiveValue("SYNTHETIC_B"))
            ),
            api_key=SensitiveValue("SYNTHETIC_KEY"),
        )


@pytest.mark.contract
async def test_canonical_client_propagates_best_effort_cancellation() -> None:
    transport = BlockingTransport()
    client = CanonicalProtocolClient(transport)
    task = asyncio.create_task(
        client.generate(
            config=config(ModelProtocol.OPENAI_RESPONSES),
            request=generation(),
            api_key=SensitiveValue("SYNTHETIC_KEY"),
            capabilities=image_capabilities(ModelProtocol.OPENAI_RESPONSES),
        )
    )
    await transport.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.cancelled
