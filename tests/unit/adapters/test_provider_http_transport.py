from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpcore
import pytest

from telegram_userbot.adapters.llm import (
    HttpcoreProviderNetworkSender,
    PinnedProviderRequest,
    ProviderHTTPTransport,
    ProviderNetworkError,
    ProviderProtocolError,
    ProviderWireRequest,
    ProviderWireResponse,
    normalize_generation_response,
)
from telegram_userbot.domain.model_config import ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.network import PublicEndpointPolicy, ValidatedEndpoint

POLICY = PublicEndpointPolicy(UUID("00000000-0000-0000-0000-000000000801"), 1)


class SequenceResolver:
    def __init__(self, *answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    def resolve(
        self, hostname: str, port: int
    ) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        self.calls.append((hostname, port))
        raw = self._answers.pop(0) if self._answers else ("203.0.113.80",)
        return frozenset(ipaddress.ip_address(value) for value in raw)


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Sequence[tuple[bytes, bytes]] = ((b"content-type", b"application/json"),)
    chunks: tuple[bytes, ...] = (b'{"ok":true}',)
    started: asyncio.Event | None = None
    release: asyncio.Event | None = None
    closed: bool = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class StubSender:
    outcomes: list[StubResponse | ProviderNetworkError]
    requests: list[PinnedProviderRequest] = field(default_factory=list)

    async def send(self, request: PinnedProviderRequest) -> StubResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderNetworkError):
            raise outcome
        return outcome


def endpoint() -> ValidatedEndpoint:
    return ValidatedEndpoint(
        policy_id=POLICY.policy_id,
        policy_version=POLICY.version,
        base_url="https://provider.example/v1",
        scheme="https",
        hostname="provider.example",
        port=443,
        base_path="/v1",
        category="public",
        resolved_addresses=("203.0.113.80",),
    )


def wire(
    *,
    stream: bool = False,
    timeout_seconds: int = 30,
    path: str = "/responses",
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, SensitiveValue[str]] | None = None,
) -> ProviderWireRequest:
    return ProviderWireRequest(
        "POST",
        path,
        headers
        or {
            "authorization": SensitiveValue("Bearer SYNTHETIC_SECRET"),
            "content-type": SensitiveValue("application/json"),
        },
        SensitiveValue(dict(body or {"input": "SYNTHETIC_PROMPT"})),
        timeout_seconds,
        stream,
    )


def transport(
    resolver: SequenceResolver, sender: StubSender, *, attempts: int = 2
) -> ProviderHTTPTransport:
    return ProviderHTTPTransport(
        endpoint=endpoint(),
        policy=POLICY,
        resolver=resolver,
        sender=sender,
        connect_attempts=attempts,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pins_tcp_ip_while_preserving_original_host_and_sni_contract() -> None:
    response = StubResponse(chunks=(b'{"output":"SYNTHETIC"}',))
    sender = StubSender([response])
    result = await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire())

    assert result.body.reveal_for_use() == {"output": "SYNTHETIC"}
    pinned = sender.requests[0]
    assert pinned.approved_addresses == ("8.8.8.8",)
    assert pinned.original_hostname == "provider.example"
    assert pinned.target == "/v1/responses"
    headers = {name: value.reveal_for_use() for name, value in pinned.headers}
    assert headers[b"host"] == b"provider.example"
    assert headers[b"accept"] == b"application/json"
    assert b"SYNTHETIC_SECRET" not in repr(pinned).encode()
    assert b"SYNTHETIC_PROMPT" not in repr(pinned).encode()
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ipv6_literal_uses_bracketed_http_host_authority() -> None:
    ipv6 = "2606:4700:4700::1111"
    ipv6_endpoint = ValidatedEndpoint(
        policy_id=POLICY.policy_id,
        policy_version=POLICY.version,
        base_url=f"https://[{ipv6}]/v1",
        scheme="https",
        hostname=ipv6,
        port=443,
        base_path="/v1",
        category="public",
        resolved_addresses=(ipv6,),
    )
    sender = StubSender([StubResponse()])
    await ProviderHTTPTransport(
        endpoint=ipv6_endpoint,
        policy=POLICY,
        resolver=SequenceResolver(),
        sender=sender,
    ).send(wire())
    headers = {name: value.reveal_for_use() for name, value in sender.requests[0].headers}
    assert headers[b"host"] == b"[2606:4700:4700::1111]"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revalidates_before_safe_connect_retry_and_uses_only_newly_approved_ip() -> None:
    response = StubResponse()
    sender = StubSender(
        [
            ProviderNetworkError("CONNECT", retryable_before_send=True),
            response,
        ]
    )
    resolver = SequenceResolver(("8.8.8.8",), ("1.1.1.1",))

    await transport(resolver, sender).send(wire())

    assert resolver.calls == [("provider.example", 443), ("provider.example", 443)]
    assert [item.approved_addresses for item in sender.requests] == [
        ("8.8.8.8",),
        ("1.1.1.1",),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dns_rebinding_to_private_address_fails_before_network_sender() -> None:
    sender = StubSender([StubResponse()])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_ENDPOINT_REJECTED$") as raised:
        await transport(SequenceResolver(("127.0.0.1",)), sender).send(wire())

    assert not raised.value.retryable
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_does_not_replay_post_after_non_connect_network_failure() -> None:
    sender = StubSender([ProviderNetworkError("READ_FAILED"), StubResponse()])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_NETWORK_FAILED$") as raised:
        await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire())

    assert raised.value.retryable
    assert len(sender.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_sender_exception_is_replaced_with_stable_redacted_error() -> None:
    class BrokenSender:
        async def send(self, request: PinnedProviderRequest) -> StubResponse:
            del request
            raise RuntimeError("SYNTHETIC_SECRET SYNTHETIC_PROMPT")

    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_NETWORK_FAILED$") as raised:
        await ProviderHTTPTransport(
            endpoint=endpoint(),
            policy=POLICY,
            resolver=SequenceResolver(("8.8.8.8",)),
            sender=BrokenSender(),
        ).send(wire())
    assert "SYNTHETIC" not in repr(raised.value)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["responses", "/", "/../responses", "/%2e%2e/responses", "/a//b", "/a?x=1"],
)
async def test_rejects_ambiguous_or_non_relative_request_targets(path: str) -> None:
    sender = StubSender([StubResponse()])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_INVALID$"):
        await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire(path=path))
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_smuggling_headers_and_oversized_request_without_secret_leak() -> None:
    sender = StubSender([StubResponse()])
    bad_headers = {
        "authorization": SensitiveValue("Bearer SYNTHETIC_SECRET"),
        "content-type": SensitiveValue("application/json"),
        "connection": SensitiveValue("keep-alive"),
    }
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_INVALID$") as raised:
        await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire(headers=bad_headers))
    assert "SYNTHETIC_SECRET" not in repr(raised.value)

    huge = {"input": "x" * (32 * 1024 * 1024)}
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_TOO_LARGE$"):
        await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire(body=huge))
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_content_type_headers_and_body_limits_fail_closed_and_close() -> None:
    cases = [
        StubResponse(headers=((b"content-type", b"text/plain"),)),
        StubResponse(
            headers=(
                (b"content-type", b"application/json"),
                (b"content-length", str(32 * 1024 * 1024 + 1).encode()),
            )
        ),
        StubResponse(headers=tuple((f"x-{index}".encode(), b"v") for index in range(129))),
        StubResponse(chunks=(b"[]",)),
    ]
    for response in cases:
        sender = StubSender([response])
        with pytest.raises(ProviderProtocolError):
            await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire())
        assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"value":NaN}'])
async def test_json_rejects_duplicate_fields_and_nonfinite_numbers(body: bytes) -> None:
    response = StubResponse(chunks=(body,))
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_RESPONSE_MALFORMED$"):
        await transport(SequenceResolver(("8.8.8.8",)), StubSender([response])).send(wire())
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redirect_is_never_followed_and_response_is_closed() -> None:
    response = StubResponse(
        status_code=307,
        headers=((b"location", b"http://127.0.0.1/secret"),),
        chunks=(),
    )
    sender = StubSender([response])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REDIRECT_FORBIDDEN$"):
        await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire())
    assert len(sender.requests) == 1
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_incremental_sse_parser_preserves_events_done_marker_and_bounds() -> None:
    terminal = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        },
    }
    payload = (
        b": heartbeat\r\n\r\n"
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","delta":"SYNTHETIC"}\n\n'
        + b"data: "
        + json.dumps(terminal, separators=(",", ":")).encode()
        + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    response = StubResponse(
        headers=((b"content-type", b"text/event-stream; charset=utf-8"),),
        chunks=(payload[:13], payload[13:57], payload[57:]),
    )
    sender = StubSender([response])
    result = await transport(SequenceResolver(("8.8.8.8",)), sender).send(wire(stream=True))

    events = [item.reveal_for_use() for item in result.stream_events]
    assert events[0]["delta"] == "SYNTHETIC"
    assert events[1] == terminal
    assert events[2] == {"_transport_sse_done": True}
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_rejects_data_after_done_and_closes() -> None:
    response = StubResponse(
        headers=((b"content-type", b"text/event-stream"),),
        chunks=(b'data: [DONE]\n\ndata: {"late":true}\n\n',),
    )
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_MALFORMED$"):
        await transport(SequenceResolver(("8.8.8.8",)), StubSender([response])).send(
            wire(stream=True)
        )
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_stream_without_secret_in_error() -> None:
    started = asyncio.Event()
    response = StubResponse(
        headers=((b"content-type", b"text/event-stream"),),
        chunks=(),
        started=started,
        release=asyncio.Event(),
    )
    task = asyncio.create_task(
        transport(SequenceResolver(("8.8.8.8",)), StubSender([response])).send(wire(stream=True))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_total_timeout_has_stable_content_free_error() -> None:
    started = asyncio.Event()
    response = StubResponse(started=started, release=asyncio.Event())
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_HTTP_TIMEOUT$") as raised:
        await transport(SequenceResolver(("8.8.8.8",)), StubSender([response])).send(
            wire(timeout_seconds=1)
        )
    assert raised.value.retryable
    assert response.closed
    assert "SYNTHETIC" not in repr(raised.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_sender_closes_short_lived_pool_when_connect_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingPool:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False
            self.request: Any = None

        async def handle_async_request(self, request: Any) -> Any:
            self.request = request
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    pool = BlockingPool()
    constructor: dict[str, Any] = {}

    def pool_factory(**kwargs: Any) -> BlockingPool:
        constructor.update(kwargs)
        return pool

    monkeypatch.setattr(httpcore, "AsyncConnectionPool", pool_factory)
    pinned = PinnedProviderRequest(
        method=b"POST",
        scheme="https",
        original_hostname="provider.example",
        port=443,
        target="/v1/responses",
        approved_addresses=("8.8.8.8",),
        headers=(
            (b"host", SensitiveValue(b"provider.example")),
            (b"content-type", SensitiveValue(b"application/json")),
        ),
        body=SensitiveValue(b'{"input":"SYNTHETIC"}'),
        timeout_seconds=30.0,
        stream=False,
    )
    task = asyncio.create_task(HttpcoreProviderNetworkSender().send(pinned))
    await pool.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pool.closed
    assert constructor["retries"] == 0
    assert constructor.get("proxy") is None
    assert pool.request.url.host == b"provider.example"
    assert pool.request.extensions["sni_hostname"] == "provider.example"


@pytest.mark.unit
def test_protocol_specific_stream_terminal_contracts() -> None:
    chat_events = (
        SensitiveValue({"choices": [{"delta": {"content": "SYNTHETIC"}, "finish_reason": None}]}),
        SensitiveValue({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        SensitiveValue(
            {
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        SensitiveValue({"_transport_sse_done": True}),
    )
    chat = normalize_generation_response(
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        ProviderWireResponse(200, SensitiveValue({}), chat_events),
    )
    assert chat.text.reveal_for_use() == "SYNTHETIC"
    assert chat.usage.total_tokens == 2

    messages_events = (
        SensitiveValue(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 0}},
            }
        ),
        SensitiveValue({"type": "content_block_delta", "delta": {"text": "SYNTHETIC"}}),
        SensitiveValue(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            }
        ),
        SensitiveValue({"type": "message_stop"}),
        SensitiveValue({"_transport_sse_done": True}),
    )
    messages = normalize_generation_response(
        ModelProtocol.ANTHROPIC_MESSAGES,
        ProviderWireResponse(200, SensitiveValue({}), messages_events),
    )
    assert messages.text.reveal_for_use() == "SYNTHETIC"
    assert messages.finish_reason == "end_turn"

    incomplete = chat_events[:-1]
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_INCOMPLETE$"):
        normalize_generation_response(
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            ProviderWireResponse(200, SensitiveValue({}), incomplete),
        )

    responses_late_data = (
        SensitiveValue({"type": "response.output_text.delta", "delta": "SYNTHETIC"}),
        SensitiveValue(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            }
        ),
        SensitiveValue({"type": "response.output_text.delta", "delta": "LATE"}),
    )
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_INCOMPLETE$"):
        normalize_generation_response(
            ModelProtocol.OPENAI_RESPONSES,
            ProviderWireResponse(200, SensitiveValue({}), responses_late_data),
        )

    messages_late_data = (
        *messages_events,
        SensitiveValue({"type": "content_block_delta", "delta": {"text": "LATE"}}),
    )
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_INCOMPLETE$"):
        normalize_generation_response(
            ModelProtocol.ANTHROPIC_MESSAGES,
            ProviderWireResponse(200, SensitiveValue({}), messages_late_data),
        )
