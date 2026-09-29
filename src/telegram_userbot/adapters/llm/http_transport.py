"""Bounded provider HTTP transport with per-attempt endpoint revalidation.

The transport deliberately creates a short-lived httpcore pool for every attempt.
Its network backend receives the original hostname from httpcore but opens the TCP
socket only to the single IP address approved for that attempt.  Consequently the
HTTP Host header and TLS SNI/certificate verification retain the original hostname
without allowing the operating system resolver to make a second, unvalidated choice.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

import httpcore

from telegram_userbot.adapters.llm.protocols import (
    ProviderProtocolError,
    ProviderWireRequest,
    ProviderWireResponse,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.network.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyError,
    HostResolver,
    ValidatedEndpoint,
    build_transport_security_contract,
)

MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_HEADER_BYTES = 64 * 1024
MAX_RESPONSE_HEADERS = 128
MAX_SSE_EVENT_BYTES = 1024 * 1024
MAX_SSE_EVENTS = 65_536
MAX_PATH_BYTES = 2_048
MAX_REQUEST_HEADER_BYTES = 16 * 1024
MAX_REQUEST_HEADERS = 32
CONNECT_ATTEMPTS = 2
MAX_PROVIDER_REQUEST_ID_BYTES = 128
MAX_RETRY_AFTER_SECONDS = 86_400

_HEADER_NAME = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SAFE_REQUEST_ID = re.compile(rb"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REQUEST_ID_HEADERS = (
    b"x-request-id",
    b"request-id",
    b"openai-request-id",
    b"anthropic-request-id",
)
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        b"connection",
        b"content-length",
        b"cookie",
        b"expect",
        b"host",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)


class ProviderNetworkError(RuntimeError):
    """Content-free error raised by the injectable, pinned network sender."""

    def __init__(self, code: str, *, retryable_before_send: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable_before_send = retryable_before_send


@dataclass(frozen=True, slots=True)
class PinnedProviderRequest:
    """A request whose TCP destination and TLS identity have separate fields."""

    method: bytes
    scheme: str
    original_hostname: str
    port: int
    target: str
    approved_addresses: tuple[str, ...]
    headers: tuple[tuple[bytes, SensitiveValue[bytes]], ...] = field(repr=False)
    body: SensitiveValue[bytes] = field(repr=False)
    timeout_seconds: float
    stream: bool

    def __post_init__(self) -> None:
        if len(self.approved_addresses) != 1:
            raise ProviderNetworkError("PROVIDER_DESTINATION_INVALID")


class ProviderNetworkResponse(Protocol):
    status_code: int
    headers: Sequence[tuple[bytes, bytes]]

    def iter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class ProviderNetworkSender(Protocol):
    async def send(self, request: PinnedProviderRequest) -> ProviderNetworkResponse: ...


class _ApprovedAddressBackend(httpcore.AsyncNetworkBackend):
    """Delegate I/O while replacing DNS resolution with one approved IP literal."""

    def __init__(
        self,
        *,
        expected_hostname: str,
        expected_port: int,
        approved_address: str,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._expected_hostname = expected_hostname
        self._expected_port = expected_port
        self._approved_address = approved_address
        self._delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host, port) != (self._expected_hostname, self._expected_port):
            raise httpcore.ConnectError("provider destination rejected")
        return await self._delegate.connect_tcp(
            self._approved_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("provider destination rejected")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _HttpcoreResponse:
    def __init__(self, response: httpcore.Response, pool: httpcore.AsyncConnectionPool) -> None:
        self.status_code = response.status
        self.headers: Sequence[tuple[bytes, bytes]] = tuple(response.headers)
        self._response = response
        self._pool = pool
        self._closed = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in cast(AsyncIterator[bytes], self._response.stream):
                yield chunk
        except httpcore.TimeoutException:
            raise ProviderNetworkError("PROVIDER_HTTP_TIMEOUT") from None
        except httpcore.NetworkError, httpcore.ProtocolError:
            raise ProviderNetworkError("PROVIDER_NETWORK_FAILED") from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        finally:
            await self._pool.aclose()


class HttpcoreProviderNetworkSender:
    """Production sender using httpcore without proxies or environment routing."""

    async def send(self, request: PinnedProviderRequest) -> ProviderNetworkResponse:
        address = request.approved_addresses[0]
        backend = _ApprovedAddressBackend(
            expected_hostname=request.original_hostname,
            expected_port=request.port,
            approved_address=address,
        )
        ssl_context: ssl.SSLContext | None = None
        if request.scheme == "https":
            ssl_context = ssl.create_default_context()
        pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.scheme,
                host=request.original_hostname,
                port=request.port,
                target=request.target,
            ),
            headers=[(name, value.reveal_for_use()) for name, value in request.headers],
            content=request.body.reveal_for_use(),
            extensions={
                "timeout": {
                    "connect": request.timeout_seconds,
                    "read": request.timeout_seconds,
                    "write": request.timeout_seconds,
                    "pool": request.timeout_seconds,
                },
                "sni_hostname": request.original_hostname,
            },
        )
        try:
            response = await pool.handle_async_request(core_request)
        except asyncio.CancelledError:
            await pool.aclose()
            raise
        except httpcore.ConnectError, httpcore.ConnectTimeout:
            await pool.aclose()
            raise ProviderNetworkError(
                "PROVIDER_CONNECT_FAILED", retryable_before_send=True
            ) from None
        except httpcore.TimeoutException:
            await pool.aclose()
            raise ProviderNetworkError("PROVIDER_HTTP_TIMEOUT") from None
        except httpcore.NetworkError, httpcore.ProtocolError:
            await pool.aclose()
            raise ProviderNetworkError("PROVIDER_NETWORK_FAILED") from None
        except Exception:
            await pool.aclose()
            raise ProviderNetworkError("PROVIDER_NETWORK_FAILED") from None
        return _HttpcoreResponse(response, pool)


def _validated_target(base_path: str, request_path: str) -> str:
    try:
        encoded = request_path.encode("ascii")
    except UnicodeEncodeError:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID") from None
    if (
        not request_path.startswith("/")
        or len(encoded) > MAX_PATH_BYTES
        or request_path == "/"
        or any(character in request_path for character in ("?", "#", "\\", "%"))
        or "//" in request_path
        or any(segment in {"", ".", ".."} for segment in request_path[1:].split("/"))
        or any(byte < 0x21 or byte == 0x7F for byte in encoded)
    ):
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    target = f"{base_path}{request_path}" or "/"
    if len(target.encode("ascii")) > MAX_PATH_BYTES:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    return target


def _request_headers(
    raw: Mapping[str, SensitiveValue[str]], *, stream: bool, hostname: str, port: int, scheme: str
) -> tuple[tuple[bytes, SensitiveValue[bytes]], ...]:
    if not 1 <= len(raw) <= MAX_REQUEST_HEADERS:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    output: list[tuple[bytes, SensitiveValue[bytes]]] = []
    total = 0
    for raw_name, wrapped in raw.items():
        try:
            name = raw_name.encode("ascii").lower()
            value = wrapped.reveal_for_use().encode("ascii")
        except AttributeError, UnicodeEncodeError:
            raise ProviderProtocolError("PROVIDER_REQUEST_INVALID") from None
        if (
            not _HEADER_NAME.fullmatch(name)
            or name in _FORBIDDEN_REQUEST_HEADERS
            or any(byte < 0x20 or byte == 0x7F for byte in value)
        ):
            raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
        total += len(name) + len(value) + 4
        output.append((name, SensitiveValue(value)))
    if total > MAX_REQUEST_HEADER_BYTES:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    content_types = [
        value.reveal_for_use().lower() for name, value in output if name == b"content-type"
    ]
    if content_types != [b"application/json"]:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    default_port = 443 if scheme == "https" else 80
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = display_host if port == default_port else f"{display_host}:{port}"
    total += len(authority) + 64
    if total > MAX_REQUEST_HEADER_BYTES:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
    output.extend(
        (
            (b"host", SensitiveValue(authority.encode("ascii"))),
            (
                b"accept",
                SensitiveValue(b"text/event-stream" if stream else b"application/json"),
            ),
            (b"user-agent", SensitiveValue(b"telegram-userbot/0.1")),
        )
    )
    return tuple(output)


def _request_body(raw: Mapping[str, Any]) -> SensitiveValue[bytes]:
    try:
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            check_circular=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except TypeError, ValueError, OverflowError, RecursionError:
        raise ProviderProtocolError("PROVIDER_REQUEST_INVALID") from None
    if not encoded or len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ProviderProtocolError("PROVIDER_REQUEST_TOO_LARGE")
    return SensitiveValue(encoded)


def _response_header_map(
    headers: Sequence[tuple[bytes, bytes]],
) -> dict[bytes, list[bytes]]:
    if len(headers) > MAX_RESPONSE_HEADERS:
        raise ProviderProtocolError("PROVIDER_RESPONSE_HEADERS_TOO_LARGE")
    total = 0
    mapped: dict[bytes, list[bytes]] = {}
    for name, value in headers:
        lower = name.lower()
        total += len(lower) + len(value) + 4
        if not _HEADER_NAME.fullmatch(lower) or any(
            (byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in value
        ):
            raise ProviderProtocolError("PROVIDER_RESPONSE_HEADERS_INVALID")
        mapped.setdefault(lower, []).append(value.strip())
    if total > MAX_RESPONSE_HEADER_BYTES:
        raise ProviderProtocolError("PROVIDER_RESPONSE_HEADERS_TOO_LARGE")
    content_lengths = mapped.get(b"content-length", [])
    if content_lengths:
        try:
            parsed = {int(value) for value in content_lengths}
        except ValueError:
            raise ProviderProtocolError("PROVIDER_RESPONSE_HEADERS_INVALID") from None
        if len(parsed) != 1 or min(parsed) < 0:
            raise ProviderProtocolError("PROVIDER_RESPONSE_HEADERS_INVALID")
        if max(parsed) > MAX_RESPONSE_BODY_BYTES:
            raise ProviderProtocolError("PROVIDER_RESPONSE_TOO_LARGE")
    return mapped


def _safe_provider_request_id(headers: Mapping[bytes, Sequence[bytes]]) -> str | None:
    """Return one bounded content-free correlation value, never arbitrary headers."""

    for name in _REQUEST_ID_HEADERS:
        values = headers.get(name, ())
        if len(values) != 1:
            continue
        value = values[0]
        if len(value) <= MAX_PROVIDER_REQUEST_ID_BYTES and _SAFE_REQUEST_ID.fullmatch(value):
            return value.decode("ascii")
    return None


def _retry_after_seconds(
    headers: Mapping[bytes, Sequence[bytes]], *, now: datetime | None = None
) -> int | None:
    values = headers.get(b"retry-after", ())
    if len(values) != 1:
        return None
    value = values[0]
    if value.isdigit():
        parsed = int(value)
    else:
        try:
            deadline = parsedate_to_datetime(value.decode("ascii"))
        except UnicodeDecodeError, TypeError, ValueError, OverflowError:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        parsed = max(0, int((deadline.astimezone(UTC) - current).total_seconds()))
    return parsed if 0 <= parsed <= MAX_RETRY_AFTER_SECONDS else None


async def _bounded_body(response: ProviderNetworkResponse) -> bytes:
    body = bytearray()
    async for chunk in response.iter_bytes():
        if not isinstance(chunk, bytes):
            raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
        if len(body) + len(chunk) > MAX_RESPONSE_BODY_BYTES:
            raise ProviderProtocolError("PROVIDER_RESPONSE_TOO_LARGE")
        body.extend(chunk)
    return bytes(body)


def _json_object(raw: bytes) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED") from None
    if not isinstance(decoded, dict):
        raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
    return cast(Mapping[str, Any], decoded)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate provider JSON field")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite provider JSON number")


def _media_type(headers: Mapping[bytes, Sequence[bytes]]) -> bytes:
    values = headers.get(b"content-type", ())
    if len(values) != 1:
        return b""
    return values[0].split(b";", maxsplit=1)[0].strip().lower()


def _is_json_media_type(value: bytes) -> bool:
    return value == b"application/json" or (
        value.startswith(b"application/") and value.endswith(b"+json")
    )


async def _sse_events(  # noqa: PLR0915 - one bounded incremental parser state machine
    response: ProviderNetworkResponse,
) -> tuple[SensitiveValue[Mapping[str, Any]], ...]:
    events: list[SensitiveValue[Mapping[str, Any]]] = []
    line_buffer = bytearray()
    data_lines: list[bytes] = []
    event_size = 0
    total = 0
    done = False

    def dispatch() -> None:
        nonlocal data_lines, event_size, done
        if not data_lines:
            event_size = 0
            return
        raw = b"\n".join(data_lines)
        data_lines = []
        event_size = 0
        if raw == b"[DONE]":
            if done:
                raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED")
            done = True
            events.append(SensitiveValue({"_transport_sse_done": True}))
            return
        if done:
            raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED")
        if len(events) >= MAX_SSE_EVENTS:
            raise ProviderProtocolError("PROVIDER_STREAM_TOO_LARGE")
        events.append(SensitiveValue(_json_object(raw)))

    def consume_line(raw_line: bytes) -> None:
        nonlocal event_size
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        if len(line) > MAX_SSE_EVENT_BYTES:
            raise ProviderProtocolError("PROVIDER_STREAM_TOO_LARGE")
        if not line:
            dispatch()
            return
        if line.startswith(b":"):
            return
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"data":
            event_size += len(value)
            if event_size > MAX_SSE_EVENT_BYTES:
                raise ProviderProtocolError("PROVIDER_STREAM_TOO_LARGE")
            data_lines.append(value)

    async for chunk in response.iter_bytes():
        if not isinstance(chunk, bytes):
            raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED")
        total += len(chunk)
        if total > MAX_RESPONSE_BODY_BYTES:
            raise ProviderProtocolError("PROVIDER_RESPONSE_TOO_LARGE")
        line_buffer.extend(chunk)
        while True:
            newline = line_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(line_buffer[:newline])
            del line_buffer[: newline + 1]
            consume_line(line)
        if len(line_buffer) > MAX_SSE_EVENT_BYTES:
            raise ProviderProtocolError("PROVIDER_STREAM_TOO_LARGE")
    if line_buffer:
        consume_line(bytes(line_buffer))
    dispatch()
    if not events:
        raise ProviderProtocolError("PROVIDER_STREAM_MALFORMED")
    return tuple(events)


class ProviderHTTPTransport:
    """Provider transport with strict endpoint, size, parsing and retry boundaries."""

    def __init__(
        self,
        *,
        endpoint: ValidatedEndpoint,
        policy: EndpointPolicy,
        resolver: HostResolver,
        sender: ProviderNetworkSender | None = None,
        connect_attempts: int = CONNECT_ATTEMPTS,
    ) -> None:
        if isinstance(connect_attempts, bool) or not 1 <= connect_attempts <= CONNECT_ATTEMPTS:
            raise ProviderProtocolError("PROVIDER_TRANSPORT_INVALID")
        self._endpoint = endpoint
        self._policy = policy
        self._resolver = resolver
        self._sender = sender or HttpcoreProviderNetworkSender()
        self._connect_attempts = connect_attempts

    async def send(  # noqa: PLR0912 - fail-closed transport boundary branches
        self, request: ProviderWireRequest
    ) -> ProviderWireResponse:
        if (
            request.method != "POST"
            or isinstance(request.timeout_seconds, bool)
            or not 1 <= request.timeout_seconds <= 600
            or not isinstance(request.stream, bool)
            or not isinstance(request.headers, Mapping)
            or not isinstance(request.body.reveal_for_use(), Mapping)
        ):
            raise ProviderProtocolError("PROVIDER_REQUEST_INVALID")
        target = _validated_target(self._endpoint.base_path, request.path)
        body = _request_body(request.body.reveal_for_use())
        last_network_error: ProviderNetworkError | None = None
        try:
            async with asyncio.timeout(request.timeout_seconds):
                for attempt in range(self._connect_attempts):
                    try:
                        contract = build_transport_security_contract(
                            self._endpoint,
                            policy=self._policy,
                            resolver=self._resolver,
                        )
                    except EndpointPolicyError:
                        raise ProviderProtocolError("PROVIDER_ENDPOINT_REJECTED") from None
                    if contract.verify_tls != (self._endpoint.scheme == "https"):
                        raise ProviderProtocolError("PROVIDER_ENDPOINT_REJECTED")
                    addresses = contract.approved_addresses
                    if not addresses:
                        raise ProviderProtocolError("PROVIDER_ENDPOINT_REJECTED")
                    selected = addresses[attempt % len(addresses)]
                    headers = _request_headers(
                        request.headers,
                        stream=request.stream,
                        hostname=contract.server_hostname,
                        port=self._endpoint.port,
                        scheme=self._endpoint.scheme,
                    )
                    pinned = PinnedProviderRequest(
                        method=b"POST",
                        scheme=self._endpoint.scheme,
                        original_hostname=contract.server_hostname,
                        port=self._endpoint.port,
                        target=target,
                        approved_addresses=(selected,),
                        headers=headers,
                        body=body,
                        timeout_seconds=float(request.timeout_seconds),
                        stream=request.stream,
                    )
                    try:
                        response = await self._sender.send(pinned)
                    except ProviderNetworkError as error:
                        last_network_error = error
                        if error.retryable_before_send and attempt + 1 < self._connect_attempts:
                            continue
                        raise _network_protocol_error(error) from None
                    except Exception:
                        raise ProviderProtocolError(
                            "PROVIDER_NETWORK_FAILED",
                            retryable=True,
                            request_may_have_been_sent=True,
                        ) from None
                    try:
                        return await self._consume_response(response, stream=request.stream)
                    except ProviderNetworkError as error:
                        raise _network_protocol_error(error) from None
                    except ProviderProtocolError:
                        raise
                    except Exception:
                        raise ProviderProtocolError(
                            "PROVIDER_NETWORK_FAILED",
                            retryable=True,
                            request_may_have_been_sent=True,
                        ) from None
        except TimeoutError:
            raise ProviderProtocolError(
                "PROVIDER_HTTP_TIMEOUT",
                retryable=True,
                request_may_have_been_sent=True,
            ) from None
        if last_network_error is not None:
            raise _network_protocol_error(last_network_error)
        raise ProviderProtocolError("PROVIDER_NETWORK_FAILED", retryable=True)

    async def _consume_response(
        self, response: ProviderNetworkResponse, *, stream: bool
    ) -> ProviderWireResponse:
        try:
            if not 200 <= response.status_code <= 599:
                raise ProviderProtocolError("PROVIDER_RESPONSE_MALFORMED")
            headers = _response_header_map(response.headers)
            provider_request_id = _safe_provider_request_id(headers)
            retry_after_seconds = _retry_after_seconds(headers)
            if 300 <= response.status_code < 400:
                raise ProviderProtocolError(
                    "PROVIDER_REDIRECT_FORBIDDEN",
                    http_status=response.status_code,
                    provider_request_id=provider_request_id,
                )
            media_type = _media_type(headers)
            if stream and 200 <= response.status_code < 300:
                if media_type != b"text/event-stream":
                    raise ProviderProtocolError("PROVIDER_STREAM_CONTENT_TYPE_INVALID")
                events = await _sse_events(response)
                return ProviderWireResponse(
                    response.status_code,
                    SensitiveValue({}),
                    events,
                    provider_request_id,
                    retry_after_seconds,
                )
            raw = await _bounded_body(response)
            if 200 <= response.status_code < 300 and not _is_json_media_type(media_type):
                raise ProviderProtocolError("PROVIDER_JSON_CONTENT_TYPE_INVALID")
            if not _is_json_media_type(media_type):
                body: Mapping[str, Any] = {}
            else:
                body = _json_object(raw)
            return ProviderWireResponse(
                response.status_code,
                SensitiveValue(body),
                provider_request_id=provider_request_id,
                retry_after_seconds=retry_after_seconds,
            )
        finally:
            await response.aclose()


def _network_protocol_error(error: ProviderNetworkError) -> ProviderProtocolError:
    if error.code == "PROVIDER_HTTP_TIMEOUT":
        return ProviderProtocolError(
            "PROVIDER_HTTP_TIMEOUT",
            retryable=True,
            request_may_have_been_sent=True,
        )
    return ProviderProtocolError(
        "PROVIDER_NETWORK_FAILED",
        retryable=True,
        request_may_have_been_sent=not error.retryable_before_send,
    )
