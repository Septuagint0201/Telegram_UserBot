"""Minimal key-only Starlette Web App with no model-configuration API."""

import json
import re
from collections import defaultdict, deque
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from typing import Protocol, cast
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from telegram_userbot.adapters.webapp.auth import (
    TelegramInitDataVerifier,
    TelegramWebIdentity,
    WebAppAuthenticationError,
)
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

MAX_KEY_REQUEST_BYTES = 16 * 1024
KEY_ACTIONS = frozenset({"set", "replace", "delete"})
SECURITY_HEADERS = {
    "cache-control": "no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'none'; frame-ancestors https://web.telegram.org "
        "https://*.telegram.org; base-uri 'none'; form-action 'self'"
    ),
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model API key</title><link rel="stylesheet" href="/webapp/model-key.css"></head>
<body><main><h1>Model API key</h1><p id="scope"></p>
<form id="key-form" autocomplete="off"><label>API key
<input id="api-key" type="password" maxlength="8192" autocomplete="new-password">
</label><button type="submit">Confirm</button></form><p id="result" role="status"></p></main>
<script src="/webapp/model-key.js" defer></script></body></html>"""

SCRIPT = """'use strict';
const params = new URLSearchParams(window.location.hash.slice(1));
const role = params.get('role') || '';
const action = params.get('action') || '';
let launch = params.get('launch') || '';
let signedInitData = params.get('tgWebAppData') || window.Telegram?.WebApp?.initData || '';
history.replaceState(null, '', window.location.pathname);
document.getElementById('scope').textContent = `${role}: ${action}`;
if (action === 'delete') document.getElementById('api-key').disabled = true;
document.getElementById('key-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('api-key');
  const payload = {action};
  if (action !== 'delete') payload.api_key = input.value;
  input.value = '';
  let accepted = false;
  try {
    const response = await fetch(`/api/v1/model-keys/${encodeURIComponent(role)}`, {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: {'content-type': 'application/json', 'x-telegram-init-data': signedInitData,
        'x-model-key-launch': launch}, body: JSON.stringify(payload)
    });
    accepted = response.ok;
  } catch {
    accepted = false;
  } finally {
    delete payload.api_key;
    launch = '';
    signedInitData = '';
  }
  document.getElementById('result').textContent = accepted ? 'Saved.' : 'Request rejected.';
  if (accepted && window.Telegram?.WebApp) window.Telegram.WebApp.close();
});"""

STYLE = """html{font-family:system-ui;color-scheme:light dark}body{margin:2rem}main{max-width:32rem}
label,input,button{display:block;width:100%;box-sizing:border-box}input,button{margin-top:.5rem;padding:.75rem}
button{margin-top:1rem}#result{min-height:1.5rem}"""


class ModelKeyMutationPort(Protocol):
    async def mutate(  # noqa: PLR0913 - explicit authentication and mutation boundary
        self,
        *,
        identity: TelegramWebIdentity,
        launch_token: SensitiveValue[str],
        role: LogicalRole,
        action: str,
        api_key: SensitiveValue[str] | None,
        now: datetime,
    ) -> bool: ...


class ClientNetworkIdentityResolver(Protocol):
    """Resolve only a reviewed transport peer; implementations may add trusted proxies."""

    def resolve(self, request: Request) -> SensitiveValue[str] | None: ...


class ClientNetworkRateLimitPort(Protocol):
    async def allow(self, *, client: SensitiveValue[str], now: datetime) -> bool: ...


class DirectPeerClientResolver:
    """Use only the ASGI socket peer and deliberately ignore forwarding headers.

    Behind the production gateway this is a conservative proxy-source identity,
    not a claim about the originating Internet client.
    """

    def resolve(self, request: Request) -> SensitiveValue[str] | None:
        client = request.client
        if client is None:
            return None
        try:
            canonical = str(ip_address(client.host))
        except ValueError:
            return None
        return SensitiveValue(canonical)


class FixedWindowClientRateLimiter:
    """Bounded secondary limiter for a direct or conservative proxy-source identity."""

    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: int = 60,
        max_clients: int = 1_024,
    ) -> None:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1_000
            or type(window_seconds) is not int
            or not 1 <= window_seconds <= 3_600
            or type(max_clients) is not int
            or not 1 <= max_clients <= 100_000
        ):
            raise ValueError("client rate-limit settings are invalid")
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_clients = max_clients
        self._events: dict[bytes, deque[float]] = defaultdict(deque)

    async def allow(self, *, client: SensitiveValue[str], now: datetime) -> bool:
        observed_at = now
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return False
        key = sha256(b"webapp-client-v1\0" + client.reveal_for_use().encode()).digest()
        threshold = observed_at.timestamp() - self._window_seconds
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self._max_clients:
                self._purge(threshold)
            if len(self._events) >= self._max_clients:
                return False
            events = self._events[key]
        while events and events[0] <= threshold:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(observed_at.timestamp())
        return True

    def _purge(self, threshold: float) -> None:
        for key in tuple(self._events):
            events = self._events[key]
            while events and events[0] <= threshold:
                events.popleft()
            if not events:
                del self._events[key]


class _RequestRejectedError(ValueError):
    pass


def _reject_duplicate_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _RequestRejectedError
        result[key] = value
    return result


def _headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _single_header(request: Request, name: bytes) -> str:
    raw_headers = cast(list[tuple[bytes, bytes]], request.scope.get("headers", []))
    values = [value for key, value in raw_headers if key.lower() == name]
    if len(values) != 1:
        raise _RequestRejectedError
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        raise _RequestRejectedError from None


def _canonical_origin(value: str, *, allow_insecure_loopback: bool) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except TypeError, ValueError:
        return False
    host = parsed.hostname
    if (
        host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        return False
    if parsed.scheme == "https" and port is None:
        return parsed.netloc == host
    if (
        not allow_insecure_loopback
        or parsed.scheme != "http"
        or host
        not in {
            "127.0.0.1",
            "::1",
        }
    ):
        return False
    canonical_host = f"[{host}]" if ":" in host else host
    canonical_netloc = canonical_host if port is None else f"{canonical_host}:{port}"
    return parsed.netloc == canonical_netloc


def create_key_web_app(  # noqa: PLR0913 - explicit security dependencies/checks
    *,
    verifier: TelegramInitDataVerifier,
    mutation_port: ModelKeyMutationPort,
    public_origin: str,
    allow_insecure_loopback: bool = False,
    client_identity: ClientNetworkIdentityResolver | None = None,
    network_rate_limit: ClientNetworkRateLimitPort | None = None,
) -> Starlette:
    if not _canonical_origin(public_origin, allow_insecure_loopback=allow_insecure_loopback):
        raise ValueError("public Web App origin must be canonical HTTPS")
    if (client_identity is None) != (network_rate_limit is None):
        raise ValueError("client rate-limit dependencies must be configured together")

    async def page(_: Request) -> Response:
        return _headers(HTMLResponse(PAGE))

    async def script(_: Request) -> Response:
        return _headers(Response(SCRIPT, media_type="application/javascript"))

    async def style(_: Request) -> Response:
        return _headers(Response(STYLE, media_type="text/css"))

    async def mutate(request: Request) -> Response:
        rejected = _headers(JSONResponse({"ok": False, "code": "REQUEST_REJECTED"}, 400))
        try:
            now = datetime.now(UTC)
            if client_identity is not None and network_rate_limit is not None:
                client = client_identity.resolve(request)
                if client is None or not await network_rate_limit.allow(client=client, now=now):
                    raise _RequestRejectedError
            if _single_header(request, b"origin") != public_origin:
                raise _RequestRejectedError
            content_type = _single_header(request, b"content-type").split(";", maxsplit=1)[0]
            declared_length = _single_header(request, b"content-length")
            if (
                content_type != "application/json"
                or re.fullmatch(r"[0-9]+", declared_length) is None
            ):
                raise _RequestRejectedError
            parsed_length = int(declared_length)
            if parsed_length < 0 or parsed_length > MAX_KEY_REQUEST_BYTES:
                raise _RequestRejectedError
            body = await request.body()
            if len(body) != parsed_length or len(body) > MAX_KEY_REQUEST_BYTES:
                raise _RequestRejectedError
            payload = json.loads(body, object_pairs_hook=_reject_duplicate_json)
            if not isinstance(payload, dict):
                raise _RequestRejectedError
            role = LogicalRole(request.path_params["role"])
            action = payload["action"]
            expected_keys = {"action"} if action == "delete" else {"action", "api_key"}
            if set(payload) != expected_keys or action not in KEY_ACTIONS:
                raise _RequestRejectedError
            raw_key = payload.get("api_key")
            if action != "delete" and (
                not isinstance(raw_key, str)
                or not raw_key
                or len(raw_key.encode("utf-8")) > 8192
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_key)
            ):
                raise _RequestRejectedError
            identity = verifier.verify(_single_header(request, b"x-telegram-init-data"), now=now)
            launch = _single_header(request, b"x-model-key-launch")
            if not launch:
                raise _RequestRejectedError
            accepted = await mutation_port.mutate(
                identity=identity,
                launch_token=SensitiveValue(launch),
                role=role,
                action=action,
                api_key=None if raw_key is None else SensitiveValue(raw_key),
                now=now,
            )
            if not accepted:
                raise _RequestRejectedError
        except (
            ClientDisconnect,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            WebAppAuthenticationError,
        ):
            return rejected
        return _headers(Response(status_code=204))

    async def method_not_allowed(_: Request, __: Exception) -> Response:
        return _headers(JSONResponse({"ok": False, "code": "REQUEST_REJECTED"}, 405))

    routes = [
        Route("/webapp/model-key", page, methods=["GET"]),
        Route("/webapp/model-key.js", script, methods=["GET"]),
        Route("/webapp/model-key.css", style, methods=["GET"]),
        Route("/api/v1/model-keys/{role:str}", mutate, methods=["POST"]),
    ]
    return Starlette(debug=False, routes=routes, exception_handlers={405: method_not_allowed})


__all__ = [
    "ClientNetworkIdentityResolver",
    "ClientNetworkRateLimitPort",
    "DirectPeerClientResolver",
    "FixedWindowClientRateLimiter",
    "ModelKeyMutationPort",
    "create_key_web_app",
]
