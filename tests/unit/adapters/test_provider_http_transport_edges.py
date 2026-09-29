from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any, cast
from uuid import UUID

import httpcore
import pytest

from telegram_userbot.adapters.llm import http_transport
from telegram_userbot.adapters.llm.http_transport import (
    MAX_RESPONSE_BODY_BYTES,
    HttpcoreProviderNetworkSender,
    PinnedProviderRequest,
    ProviderHTTPTransport,
    ProviderNetworkError,
    _ApprovedAddressBackend,
    _HttpcoreResponse,
    _retry_after_seconds,
)
from telegram_userbot.adapters.llm.protocols import ProviderProtocolError, ProviderWireRequest
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.network.endpoint_policy import (
    PublicEndpointPolicy,
    TransportSecurityContract,
    ValidatedEndpoint,
)

POLICY = PublicEndpointPolicy(UUID("00000000-0000-0000-0000-000000000811"), 1)


class Resolver:
    def resolve(
        self, hostname: str, port: int
    ) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        assert (hostname, port) == ("provider.example", 443)
        return frozenset({ipaddress.ip_address("8.8.8.8")})


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Sequence[tuple[bytes, bytes]] = ((b"content-type", b"application/json"),)
    chunks: tuple[object, ...] = (b'{"ok":true}',)
    closed: bool = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield cast(bytes, chunk)

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class StubSender:
    outcomes: list[StubResponse | ProviderNetworkError | Exception]
    requests: list[PinnedProviderRequest] = field(default_factory=list)

    async def send(self, request: PinnedProviderRequest) -> StubResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
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
        resolved_addresses=("8.8.8.8",),
    )


def wire(  # noqa: PLR0913 - test helper exposes independent wire dimensions
    *,
    method: str = "POST",
    path: str = "/responses",
    headers: Mapping[str, SensitiveValue[str]] | None = None,
    body: object | None = None,
    timeout_seconds: int = 30,
    stream: bool = False,
) -> ProviderWireRequest:
    return ProviderWireRequest(
        method=method,
        path=path,
        headers=headers
        or {
            "authorization": SensitiveValue("Bearer SYNTHETIC_SECRET"),
            "content-type": SensitiveValue("application/json"),
        },
        body=SensitiveValue(cast(dict[str, Any], body or {"input": "SYNTHETIC"})),
        timeout_seconds=timeout_seconds,
        stream=stream,
    )


def transport(sender: StubSender, *, attempts: int = 2) -> ProviderHTTPTransport:
    return ProviderHTTPTransport(
        endpoint=endpoint(),
        policy=POLICY,
        resolver=Resolver(),
        sender=sender,
        connect_attempts=attempts,
    )


def pinned(
    *, scheme: str = "https", addresses: tuple[str, ...] = ("8.8.8.8",)
) -> PinnedProviderRequest:
    return PinnedProviderRequest(
        method=b"POST",
        scheme=scheme,
        original_hostname="provider.example",
        port=443,
        target="/v1/responses",
        approved_addresses=addresses,
        headers=((b"content-type", SensitiveValue(b"application/json")),),
        body=SensitiveValue(b'{"input":"SYNTHETIC"}'),
        timeout_seconds=30.0,
        stream=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize("addresses", [(), ("8.8.8.8", "1.1.1.1")])
def test_pinned_request_rejects_zero_or_multiple_tcp_destinations(
    addresses: tuple[str, ...],
) -> None:
    with pytest.raises(ProviderNetworkError, match=r"^PROVIDER_DESTINATION_INVALID$"):
        pinned(addresses=addresses)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinned_backend_only_connects_to_the_revalidated_address() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.tcp_calls: list[tuple[str, int, float | None]] = []
            self.sleeps: list[float] = []

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore protocol signature
            local_address: str | None = None,
            socket_options: object | None = None,
        ) -> httpcore.AsyncNetworkStream:
            del local_address, socket_options
            self.tcp_calls.append((host, port, timeout))
            return cast(httpcore.AsyncNetworkStream, object())

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

    delegate = Delegate()
    backend = _ApprovedAddressBackend(
        expected_hostname="provider.example",
        expected_port=443,
        approved_address="8.8.8.8",
        delegate=cast(httpcore.AsyncNetworkBackend, delegate),
    )

    await backend.connect_tcp("provider.example", 443, timeout=2.5)
    await backend.sleep(0.1)
    assert delegate.tcp_calls == [("8.8.8.8", 443, 2.5)]
    assert delegate.sleeps == [0.1]

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("attacker.example", 443)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("synthetic-provider.sock")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code", "retryable_before_send"),
    [
        (httpcore.ConnectError("synthetic"), "PROVIDER_CONNECT_FAILED", True),
        (httpcore.ReadTimeout("synthetic"), "PROVIDER_HTTP_TIMEOUT", False),
        (httpcore.ProtocolError("synthetic"), "PROVIDER_NETWORK_FAILED", False),
        (RuntimeError("synthetic"), "PROVIDER_NETWORK_FAILED", False),
    ],
)
async def test_httpcore_sender_closes_its_short_lived_pool_for_all_connect_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
    retryable_before_send: bool,
) -> None:
    class FailingPool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.closed = False

        async def handle_async_request(self, request: httpcore.Request) -> httpcore.Response:
            del request
            raise failure

        async def aclose(self) -> None:
            self.closed = True

    pool = FailingPool()
    monkeypatch.setattr(httpcore, "AsyncConnectionPool", lambda **_kwargs: pool)

    with pytest.raises(ProviderNetworkError, match=f"^{code}$") as raised:
        await HttpcoreProviderNetworkSender().send(pinned())

    assert raised.value.retryable_before_send is retryable_before_send
    assert pool.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_sender_uses_no_tls_context_for_http_when_a_private_policy_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def handle_async_request(self, request: httpcore.Request) -> httpcore.Response:
            del request
            raise httpcore.ConnectError("synthetic")

        async def aclose(self) -> None:
            return None

    pool = FailingPool()
    captured: dict[str, Any] = {}

    def pool_factory(**kwargs: Any) -> FailingPool:
        captured.update(kwargs)
        return pool

    monkeypatch.setattr(httpcore, "AsyncConnectionPool", pool_factory)

    with pytest.raises(ProviderNetworkError, match=r"^PROVIDER_CONNECT_FAILED$"):
        await HttpcoreProviderNetworkSender().send(pinned(scheme="http"))
    assert captured["ssl_context"] is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_factory", "code"),
    [
        (
            lambda: _raising_stream(httpcore.ReadTimeout("synthetic")),
            "PROVIDER_HTTP_TIMEOUT",
        ),
        (
            lambda: _raising_stream(httpcore.ProtocolError("synthetic")),
            "PROVIDER_NETWORK_FAILED",
        ),
    ],
)
async def test_httpcore_response_converts_stream_failures_and_closes_once(
    stream_factory: object, code: str
) -> None:
    class CoreResponse:
        status = 200
        headers: tuple[tuple[bytes, bytes], ...] = ()

        def __init__(self, stream: AsyncIterator[bytes]) -> None:
            self.stream = stream
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    stream = cast(AsyncIterator[bytes], cast(Any, stream_factory)())
    core_response = CoreResponse(stream)
    pool = Pool()
    response = _HttpcoreResponse(
        cast(httpcore.Response, core_response), cast(httpcore.AsyncConnectionPool, pool)
    )

    with pytest.raises(ProviderNetworkError, match=f"^{code}$"):
        await anext(response.iter_bytes())
    await response.aclose()
    await response.aclose()
    assert core_response.closed
    assert pool.close_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_response_yields_bytes_and_owns_pool_shutdown() -> None:
    async def stream() -> AsyncIterator[bytes]:
        yield b"first"
        yield b"second"

    class CoreResponse:
        status = 200
        headers: tuple[tuple[bytes, bytes], ...] = ()

        def __init__(self) -> None:
            self.stream = stream()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Pool:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    core_response = CoreResponse()
    pool = Pool()
    response = _HttpcoreResponse(
        cast(httpcore.Response, core_response), cast(httpcore.AsyncConnectionPool, pool)
    )

    assert [chunk async for chunk in response.iter_bytes()] == [b"first", b"second"]
    await response.aclose()
    assert core_response.closed
    assert pool.closed


async def _raise_stream_error(error: Exception) -> None:
    raise error


async def _raising_stream(error: Exception) -> AsyncIterator[bytes]:
    await _raise_stream_error(error)
    yield b""  # pragma: no cover - preserves the AsyncIterator contract


@pytest.mark.unit
@pytest.mark.parametrize("attempts", [False, 0, 3])
def test_transport_constructor_rejects_invalid_connect_attempt_count(attempts: object) -> None:
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_TRANSPORT_INVALID$"):
        ProviderHTTPTransport(
            endpoint=endpoint(),
            policy=POLICY,
            resolver=Resolver(),
            sender=StubSender([StubResponse()]),
            connect_attempts=cast(int, attempts),
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_request",
    [
        wire(method="GET"),
        wire(timeout_seconds=0),
        wire(stream=cast(bool, "not-bool")),
        ProviderWireRequest(
            "POST",
            "/responses",
            cast(Mapping[str, SensitiveValue[str]], []),
            SensitiveValue({"input": "SYNTHETIC"}),
            30,
            False,
        ),
        ProviderWireRequest(
            "POST",
            "/responses",
            {"content-type": SensitiveValue("application/json")},
            SensitiveValue(cast(dict[str, Any], [])),
            30,
            False,
        ),
    ],
)
async def test_transport_rejects_invalid_wire_request_shapes_before_network_io(
    wire_request: ProviderWireRequest,
) -> None:
    sender = StubSender([StubResponse()])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_INVALID$"):
        await transport(sender).send(wire_request)
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_request",
    [
        wire(path="/r\u00e9ponses"),
        wire(
            headers={
                "content-type": SensitiveValue("application/json"),
                "x": SensitiveValue("bad\n"),
            }
        ),
        wire(
            headers={
                "content-type": SensitiveValue("application/json"),
                "x": SensitiveValue("\u00fc"),
            }
        ),
        wire(body={"input": float("nan")}),
    ],
)
async def test_transport_rejects_ambiguous_encoding_before_network_io(
    wire_request: ProviderWireRequest,
) -> None:
    sender = StubSender([StubResponse()])
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_INVALID$"):
        await transport(sender).send(wire_request)
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_rejects_a_target_that_exceeds_the_combined_base_path_budget() -> None:
    sender = StubSender([StubResponse()])
    request = wire(path="/" + "a" * 2047)

    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_REQUEST_INVALID$"):
        await transport(sender).send(request)
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "code"),
    [
        (
            TransportSecurityContract(False, "provider.example", ("8.8.8.8",)),
            "PROVIDER_ENDPOINT_REJECTED",
        ),
        (
            TransportSecurityContract(True, "provider.example", ()),
            "PROVIDER_ENDPOINT_REJECTED",
        ),
    ],
)
async def test_transport_rejects_inconsistent_revalidation_contract(
    monkeypatch: pytest.MonkeyPatch, contract: TransportSecurityContract, code: str
) -> None:
    monkeypatch.setattr(
        http_transport,
        "build_transport_security_contract",
        lambda *_args, **_kwargs: contract,
    )
    sender = StubSender([StubResponse()])
    with pytest.raises(ProviderProtocolError, match=f"^{code}$"):
        await transport(sender).send(wire())
    assert sender.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_json_provider_error_keeps_bounded_metadata_without_body_content() -> None:
    response = StubResponse(
        status_code=429,
        headers=(
            (b"content-type", b"text/plain"),
            (b"x-request-id", b"provider-123"),
            (b"retry-after", b"7"),
        ),
        chunks=(b"SYNTHETIC_ERROR_BODY",),
    )
    result = await transport(StubSender([response])).send(wire())

    assert result.status_code == 429
    assert result.body.reveal_for_use() == {}
    assert result.provider_request_id == "provider-123"
    assert result.retry_after_seconds == 7
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (600, ((b"content-type", b"application/json"),), "PROVIDER_RESPONSE_MALFORMED"),
        (200, ((b"bad header", b"value"),), "PROVIDER_RESPONSE_HEADERS_INVALID"),
        (
            200,
            ((b"content-type", b"application/json"), (b"content-length", b"not-a-number")),
            "PROVIDER_RESPONSE_HEADERS_INVALID",
        ),
        (
            200,
            ((b"content-type", b"application/json"), (b"content-length", b"-1")),
            "PROVIDER_RESPONSE_HEADERS_INVALID",
        ),
    ],
)
async def test_transport_rejects_malformed_response_status_and_headers(
    status: int, headers: Sequence[tuple[bytes, bytes]], code: str
) -> None:
    response = StubResponse(status_code=status, headers=headers)
    with pytest.raises(ProviderProtocolError, match=f"^{code}$"):
        await transport(StubSender([response])).send(wire())
    assert response.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_enforces_response_header_and_body_budgets_without_allocating_content() -> (
    None
):
    class TooLargeChunk(bytes):
        def __len__(self) -> int:
            return MAX_RESPONSE_BODY_BYTES + 1

    too_many_header_bytes = StubResponse(
        headers=(
            (b"content-type", b"application/json"),
            (b"x-bounded", b"x" * (64 * 1024)),
        )
    )
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_RESPONSE_HEADERS_TOO_LARGE$"):
        await transport(StubSender([too_many_header_bytes])).send(wire())
    assert too_many_header_bytes.closed

    too_large_body = StubResponse(chunks=(TooLargeChunk(b"x"),))
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_RESPONSE_TOO_LARGE$"):
        await transport(StubSender([too_large_body])).send(wire())
    assert too_large_body.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_allows_empty_json_success_and_tolerates_missing_error_media_type() -> None:
    empty_success = StubResponse(chunks=())
    result = await transport(StubSender([empty_success])).send(wire())
    assert result.body.reveal_for_use() == {}
    assert empty_success.closed

    content_free_error = StubResponse(status_code=503, headers=(), chunks=(b"ignored",))
    result = await transport(StubSender([content_free_error])).send(wire())
    assert result.body.reveal_for_use() == {}
    assert content_free_error.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_rejects_invalid_stream_content_type_and_duplicate_done_marker() -> None:
    wrong_type = StubResponse(headers=((b"content-type", b"application/json"),))
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_CONTENT_TYPE_INVALID$"):
        await transport(StubSender([wrong_type])).send(wire(stream=True))
    assert wrong_type.closed

    duplicate_done = StubResponse(
        headers=((b"content-type", b"text/event-stream"),),
        chunks=(b"data: [DONE]\n\ndata: [DONE]\n\n",),
    )
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_STREAM_MALFORMED$"):
        await transport(StubSender([duplicate_done])).send(wire(stream=True))
    assert duplicate_done.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_maps_sender_timeout_to_retryable_content_free_protocol_error() -> None:
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_HTTP_TIMEOUT$") as raised:
        await transport(StubSender([ProviderNetworkError("PROVIDER_HTTP_TIMEOUT")])).send(wire())
    assert raised.value.retryable
    assert raised.value.request_may_have_been_sent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_maps_response_iteration_and_unexpected_consumer_errors() -> None:
    class NetworkFailureResponse:
        status_code = 200
        headers: Sequence[tuple[bytes, bytes]] = ((b"content-type", b"application/json"),)

        def __init__(self) -> None:
            self.closed = False

        async def iter_bytes(self) -> AsyncIterator[bytes]:
            await _raise_stream_error(ProviderNetworkError("PROVIDER_HTTP_TIMEOUT"))
            yield b""  # pragma: no cover - preserves the AsyncIterator contract

        async def aclose(self) -> None:
            self.closed = True

    class BrokenResponse:
        headers: Sequence[tuple[bytes, bytes]] = ()

        def __init__(self) -> None:
            self.closed = False

        @property
        def status_code(self) -> int:
            raise RuntimeError("synthetic consumer failure")

        async def iter_bytes(self) -> AsyncIterator[bytes]:
            yield b""

        async def aclose(self) -> None:
            self.closed = True

    network_failure = NetworkFailureResponse()
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_HTTP_TIMEOUT$") as raised:
        await transport(StubSender([cast(StubResponse, network_failure)])).send(wire())
    assert raised.value.retryable
    assert network_failure.closed

    broken = BrokenResponse()
    with pytest.raises(ProviderProtocolError, match=r"^PROVIDER_NETWORK_FAILED$") as raised:
        await transport(StubSender([cast(StubResponse, broken)])).send(wire())
    assert raised.value.retryable
    assert raised.value.request_may_have_been_sent
    assert broken.closed


@pytest.mark.unit
def test_retry_after_parses_rfc7231_date_without_accepting_out_of_range_values() -> None:
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    valid = format_datetime(now + timedelta(seconds=42), usegmt=True).encode()
    assert _retry_after_seconds({b"retry-after": [valid]}, now=now) == 42
    naive = b"Thu, 04 Sep 2026 00:00:42"
    assert _retry_after_seconds({b"retry-after": [naive]}, now=now) == 42
    assert _retry_after_seconds({b"retry-after": [b"not-a-date"]}, now=now) is None
    assert _retry_after_seconds({b"retry-after": [b"86401"]}, now=now) is None
