"""Bounded Telegram Bot API HTTP client for the Control Bot."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self, cast
from urllib.parse import urlsplit

import httpx

from telegram_userbot.domain.shared.redaction import SensitiveValue

TELEGRAM_BOT_API_ORIGIN = "https://api.telegram.org"
ALLOWED_UPDATES = ("message", "callback_query")
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_HEADERS = 64
MAX_RESPONSE_HEADER_BYTES = 32 * 1024
MAX_JSON_ITEMS = 20_000
MAX_JSON_DEPTH = 24
MAX_UPDATES_PER_POLL = 100
MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_DATA_BYTES = 64

_TOKEN = re.compile(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}\Z")
_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class BotAPIError(RuntimeError):
    """Stable error that never carries request URLs, response bodies, or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"BotAPIError({self.code!r})"


class BotMutationState(StrEnum):
    KNOWN = "known"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class KnownBotMessage:
    """A Bot message that Telegram positively acknowledged with an exact identifier."""

    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        if type(self.chat_id) is not int or self.chat_id == 0:
            raise ValueError("Bot message chat id is invalid")
        if type(self.message_id) is not int or self.message_id <= 0:
            raise ValueError("Bot message id is invalid")


@dataclass(frozen=True, slots=True)
class BotMutationResult:
    state: BotMutationState
    message: KnownBotMessage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, BotMutationState):
            raise TypeError("Bot mutation state is invalid")
        if self.message is not None and self.state is not BotMutationState.KNOWN:
            raise ValueError("only a known send may carry a Bot message reference")


class BotHTTPResponse(Protocol):
    status_code: int
    headers: Sequence[tuple[bytes, bytes]]

    def iter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class BotHTTPSender(Protocol):
    async def post(
        self,
        *,
        token: SensitiveValue[str],
        method: str,
        body: SensitiveValue[bytes],
        timeout_seconds: float,
    ) -> BotHTTPResponse: ...

    async def aclose(self) -> None: ...


class _HttpxResponse:
    def __init__(self, context: Any, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.headers: Sequence[tuple[bytes, bytes]] = tuple(response.headers.raw)
        self._context = context
        self._response = response
        self._closed = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes():
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._context.__aexit__(None, None, None)


class HttpxTelegramBotSender:
    """Production sender with proxies, redirects, and environment routing disabled."""

    def __init__(
        self,
        *,
        base_origin: str = TELEGRAM_BOT_API_ORIGIN,
        allow_insecure_loopback: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _validate_base_origin(base_origin, allow_insecure_loopback=allow_insecure_loopback)
        self._origin = base_origin
        self._client = httpx.AsyncClient(
            base_url=base_origin,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        self._closed = False

    def __repr__(self) -> str:
        return f"HttpxTelegramBotSender(origin=<fixed>, closed={self._closed!r})"

    async def post(
        self,
        *,
        token: SensitiveValue[str],
        method: str,
        body: SensitiveValue[bytes],
        timeout_seconds: float,
    ) -> BotHTTPResponse:
        if self._closed:
            raise BotAPIError("BOT_HTTP_CLOSED")
        if _METHOD.fullmatch(method) is None:
            raise BotAPIError("BOT_REQUEST_INVALID")
        raw_token = token.reveal_for_use()
        if _TOKEN.fullmatch(raw_token) is None:
            raise BotAPIError("BOT_TOKEN_INVALID")
        try:
            context = self._client.stream(
                "POST",
                f"/bot{raw_token}/{method}",
                content=body.reveal_for_use(),
                headers={"content-type": "application/json", "accept": "application/json"},
                timeout=httpx.Timeout(timeout_seconds),
            )
            response = await context.__aenter__()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BotAPIError("BOT_NETWORK_FAILED") from None
        return _HttpxResponse(context, response)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()


@dataclass(frozen=True, slots=True)
class TelegramBotIdentity:
    user_id: int
    username: str

    def __post_init__(self) -> None:
        if type(self.user_id) is not int or self.user_id <= 0:
            raise ValueError("Control Bot user id is invalid")
        if (
            not 5 <= len(self.username) <= 32
            or not self.username.casefold().endswith("bot")
            or re.fullmatch(r"[A-Za-z0-9_]+", self.username) is None
        ):
            raise ValueError("Control Bot username is invalid")


@dataclass(slots=True)
class TelegramBotAPI:
    """Strict Bot API surface; mutation uncertainty is returned, never retried."""

    token: SensitiveValue[str] = field(repr=False)
    identity: TelegramBotIdentity
    sender: BotHTTPSender
    request_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        raw_token = self.token.reveal_for_use()
        if _TOKEN.fullmatch(raw_token) is None:
            raise ValueError("Control Bot token is invalid")
        if (
            isinstance(self.request_timeout_seconds, bool)
            or not 1 <= self.request_timeout_seconds <= 120
        ):
            raise ValueError("Control Bot request timeout is invalid")

    async def verify_identity(self) -> None:
        payload = await self._query("getMe", {})
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise BotAPIError("BOT_IDENTITY_MISMATCH")
        user_id = result.get("id")
        username = result.get("username")
        if (
            type(user_id) is not int
            or user_id != self.identity.user_id
            or result.get("is_bot") is not True
            or not isinstance(username, str)
            or username.casefold() != self.identity.username.casefold()
        ):
            raise BotAPIError("BOT_IDENTITY_MISMATCH")

    async def get_updates(
        self,
        *,
        offset: int,
        long_poll_seconds: int = 45,
    ) -> tuple[Mapping[str, Any], ...]:
        if (
            type(offset) is not int
            or offset < 0
            or type(long_poll_seconds) is not int
            or not 1 <= long_poll_seconds <= 50
        ):
            raise BotAPIError("BOT_REQUEST_INVALID")
        payload = await self._query(
            "getUpdates",
            {
                "offset": offset,
                "limit": MAX_UPDATES_PER_POLL,
                "timeout": long_poll_seconds,
                "allowed_updates": list(ALLOWED_UPDATES),
            },
            timeout_seconds=float(long_poll_seconds) + self.request_timeout_seconds,
        )
        result = payload.get("result")
        if not isinstance(result, list) or len(result) > MAX_UPDATES_PER_POLL:
            raise BotAPIError("BOT_RESPONSE_MALFORMED")
        if any(not isinstance(update, Mapping) for update in result):
            raise BotAPIError("BOT_RESPONSE_MALFORMED")
        return tuple(cast(Mapping[str, Any], update) for update in result)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        web_app_url: SensitiveValue[str] | None = None,
        callback_data: SensitiveValue[str] | None = None,
    ) -> BotMutationResult:
        if type(chat_id) is not int or chat_id == 0 or not isinstance(text, str):
            raise BotAPIError("BOT_REQUEST_INVALID")
        if not text or len(text) > MAX_MESSAGE_CHARS or len(text.encode("utf-8")) > 16_384:
            raise BotAPIError("BOT_REQUEST_INVALID")
        if web_app_url is not None and callback_data is not None:
            raise BotAPIError("BOT_REQUEST_INVALID")
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if web_app_url is not None:
            url = web_app_url.reveal_for_use()
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
                raise BotAPIError("BOT_REQUEST_INVALID")
            body["reply_markup"] = {
                "inline_keyboard": [[{"text": "Open secure key page", "web_app": {"url": url}}]]
            }
        if callback_data is not None:
            data = callback_data.reveal_for_use()
            if not data or len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
                raise BotAPIError("BOT_REQUEST_INVALID")
            body["reply_markup"] = {
                "inline_keyboard": [[{"text": "Confirm", "callback_data": data}]]
            }
        return await self._send_message_mutation(chat_id=chat_id, body=body)

    async def delete_message(self, message: KnownBotMessage) -> BotMutationResult:
        if not isinstance(message, KnownBotMessage):
            raise BotAPIError("BOT_DELETE_REQUIRES_KNOWN_MESSAGE")
        return await self._boolean_mutation(
            "deleteMessage",
            {"chat_id": message.chat_id, "message_id": message.message_id},
        )

    async def answer_callback_query(self, *, callback_query_id: str) -> BotMutationResult:
        if (
            not isinstance(callback_query_id, str)
            or not 1 <= len(callback_query_id) <= 256
            or any(ord(character) < 0x20 for character in callback_query_id)
        ):
            raise BotAPIError("BOT_REQUEST_INVALID")
        return await self._boolean_mutation(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "cache_time": 0},
        )

    async def aclose(self) -> None:
        await self.sender.aclose()

    async def _query(
        self,
        method: str,
        body: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        response = await self._request(
            method,
            body,
            timeout_seconds=timeout_seconds or self.request_timeout_seconds,
        )
        if response.get("ok") is not True:
            raise BotAPIError("BOT_API_REJECTED")
        return response

    async def _send_message_mutation(
        self, *, chat_id: int, body: Mapping[str, Any]
    ) -> BotMutationResult:
        try:
            payload = await self._request("sendMessage", body)
        except BotAPIError as error:
            return BotMutationResult(
                BotMutationState.REJECTED
                if error.code in {"BOT_API_REJECTED", "BOT_REDIRECT_FORBIDDEN"}
                else BotMutationState.UNKNOWN
            )
        if payload.get("ok") is not True:
            return BotMutationResult(BotMutationState.REJECTED)
        result = payload.get("result")
        if not isinstance(result, Mapping):
            return BotMutationResult(BotMutationState.UNKNOWN)
        message_id = result.get("message_id")
        chat = result.get("chat")
        returned_chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        if type(message_id) is not int or message_id <= 0 or returned_chat_id != chat_id:
            return BotMutationResult(BotMutationState.UNKNOWN)
        return BotMutationResult(
            BotMutationState.KNOWN,
            KnownBotMessage(chat_id=chat_id, message_id=message_id),
        )

    async def _boolean_mutation(self, method: str, body: Mapping[str, Any]) -> BotMutationResult:
        try:
            payload = await self._request(method, body)
        except BotAPIError as error:
            return BotMutationResult(
                BotMutationState.REJECTED
                if error.code in {"BOT_API_REJECTED", "BOT_REDIRECT_FORBIDDEN"}
                else BotMutationState.UNKNOWN
            )
        return BotMutationResult(
            BotMutationState.KNOWN
            if payload.get("ok") is True and payload.get("result") is True
            else BotMutationState.REJECTED
        )

    async def _request(
        self,
        method: str,
        body: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        encoded = _encode_request(body)
        response: BotHTTPResponse | None = None
        try:
            response = await self.sender.post(
                token=self.token,
                method=method,
                body=SensitiveValue(encoded),
                timeout_seconds=timeout_seconds or self.request_timeout_seconds,
            )
            payload = await _consume_response(response)
        except asyncio.CancelledError:
            raise
        except BotAPIError:
            raise
        except Exception:
            raise BotAPIError("BOT_NETWORK_FAILED") from None
        else:
            return payload
        finally:
            if response is not None:
                with suppress(Exception):
                    await response.aclose()


def _validate_base_origin(value: str, *, allow_insecure_loopback: bool) -> None:
    parsed = urlsplit(value)
    loopback = (
        allow_insecure_loopback
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1"}
    )
    if (
        (value != TELEGRAM_BOT_API_ORIGIN and not loopback)
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ValueError("Bot API origin is invalid")


def _encode_request(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except TypeError, ValueError, OverflowError, RecursionError:
        raise BotAPIError("BOT_REQUEST_INVALID") from None
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise BotAPIError("BOT_REQUEST_TOO_LARGE")
    return encoded


def _validate_response_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> dict[bytes, list[bytes]]:
    if len(headers) > MAX_RESPONSE_HEADERS:
        raise BotAPIError("BOT_RESPONSE_HEADERS_TOO_LARGE")
    total = 0
    mapped: dict[bytes, list[bytes]] = {}
    for name, value in headers:
        lower = name.lower()
        total += len(lower) + len(value) + 4
        if _HEADER_NAME.fullmatch(lower) is None or any(
            (byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in value
        ):
            raise BotAPIError("BOT_RESPONSE_HEADERS_INVALID")
        mapped.setdefault(lower, []).append(value.strip())
    if total > MAX_RESPONSE_HEADER_BYTES:
        raise BotAPIError("BOT_RESPONSE_HEADERS_TOO_LARGE")
    lengths = mapped.get(b"content-length", [])
    if lengths:
        try:
            parsed = {int(value) for value in lengths}
        except ValueError:
            raise BotAPIError("BOT_RESPONSE_HEADERS_INVALID") from None
        if len(parsed) != 1 or min(parsed) < 0:
            raise BotAPIError("BOT_RESPONSE_HEADERS_INVALID")
        if max(parsed) > MAX_RESPONSE_BYTES:
            raise BotAPIError("BOT_RESPONSE_TOO_LARGE")
    return mapped


def _media_type(headers: Mapping[bytes, Sequence[bytes]]) -> bytes:
    values = headers.get(b"content-type", ())
    if len(values) != 1:
        return b""
    return values[0].split(b";", maxsplit=1)[0].strip().lower()


async def _read_response(response: BotHTTPResponse) -> bytes:
    content = bytearray()
    async for chunk in response.iter_bytes():
        if not isinstance(chunk, bytes):
            raise BotAPIError("BOT_RESPONSE_MALFORMED")
        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
            raise BotAPIError("BOT_RESPONSE_TOO_LARGE")
        content.extend(chunk)
    return bytes(content)


async def _consume_response(response: BotHTTPResponse) -> Mapping[str, Any]:
    if not 200 <= response.status_code <= 599:
        raise BotAPIError("BOT_RESPONSE_MALFORMED")
    if 300 <= response.status_code < 400:
        raise BotAPIError("BOT_REDIRECT_FORBIDDEN")
    if 400 <= response.status_code < 500:
        raise BotAPIError("BOT_API_REJECTED")
    if response.status_code >= 500:
        raise BotAPIError("BOT_API_SERVER_FAILED")
    headers = _validate_response_headers(response.headers)
    raw = await _read_response(response)
    if _media_type(headers) != b"application/json":
        raise BotAPIError("BOT_RESPONSE_CONTENT_TYPE_INVALID")
    payload = _decode_json(raw)
    if not 200 <= response.status_code < 300:
        raise BotAPIError("BOT_API_REJECTED")
    return payload


def _decode_json(raw: bytes) -> Mapping[str, Any]:
    if not raw:
        raise BotAPIError("BOT_RESPONSE_MALFORMED")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise BotAPIError("BOT_RESPONSE_MALFORMED") from None
    if not isinstance(payload, dict):
        raise BotAPIError("BOT_RESPONSE_MALFORMED")
    _validate_json_shape(payload)
    return cast(Mapping[str, Any], payload)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate Bot API JSON field")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite Bot API JSON number")


def _validate_json_shape(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    items = 0
    while stack:
        value, depth = stack.pop()
        items += 1
        if items > MAX_JSON_ITEMS or depth > MAX_JSON_DEPTH:
            raise BotAPIError("BOT_RESPONSE_TOO_LARGE")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


__all__ = [
    "ALLOWED_UPDATES",
    "BotAPIError",
    "BotHTTPSender",
    "BotMutationResult",
    "BotMutationState",
    "HttpxTelegramBotSender",
    "KnownBotMessage",
    "TelegramBotAPI",
    "TelegramBotIdentity",
]
