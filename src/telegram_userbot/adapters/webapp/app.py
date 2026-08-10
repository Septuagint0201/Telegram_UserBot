"""Minimal key-only Starlette Web App with no model-configuration API."""

import json
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
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
<title>Model API key</title><link rel="stylesheet" href="/model-key.css"></head>
<body><main><h1>Model API key</h1><p id="scope"></p>
<form id="key-form" autocomplete="off"><label>API key
<input id="api-key" type="password" maxlength="8192" autocomplete="new-password">
</label><button type="submit">Confirm</button></form><p id="result" role="status"></p></main>
<script src="/model-key.js" defer></script></body></html>"""

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


def create_key_web_app(
    *,
    verifier: TelegramInitDataVerifier,
    mutation_port: ModelKeyMutationPort,
    public_origin: str,
    allow_insecure_loopback: bool = False,
) -> Starlette:
    parsed_origin = urlsplit(public_origin)
    secure_origin = parsed_origin.scheme == "https"
    test_loopback = (
        allow_insecure_loopback
        and parsed_origin.scheme == "http"
        and parsed_origin.hostname in {"127.0.0.1", "::1"}
    )
    if (
        (not secure_origin and not test_loopback)
        or public_origin.endswith("/")
        or parsed_origin.path
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise ValueError("public Web App origin must be canonical HTTPS")

    async def page(_: Request) -> Response:
        return _headers(HTMLResponse(PAGE))

    async def script(_: Request) -> Response:
        return _headers(Response(SCRIPT, media_type="application/javascript"))

    async def style(_: Request) -> Response:
        return _headers(Response(STYLE, media_type="text/css"))

    async def mutate(request: Request) -> Response:
        rejected = _headers(JSONResponse({"ok": False, "code": "REQUEST_REJECTED"}, 400))
        try:
            if request.headers.get("origin") != public_origin:
                raise _RequestRejectedError
            content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
            declared_length = request.headers.get("content-length")
            if (
                content_type != "application/json"
                or declared_length is None
                or int(declared_length) > MAX_KEY_REQUEST_BYTES
            ):
                raise _RequestRejectedError
            body = await request.body()
            if len(body) > MAX_KEY_REQUEST_BYTES:
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
                not isinstance(raw_key, str) or not raw_key or len(raw_key) > 8192
            ):
                raise _RequestRejectedError
            identity = verifier.verify(
                request.headers.get("x-telegram-init-data", ""), now=datetime.now(UTC)
            )
            launch = request.headers.get("x-model-key-launch", "")
            if not launch:
                raise _RequestRejectedError
            accepted = await mutation_port.mutate(
                identity=identity,
                launch_token=SensitiveValue(launch),
                role=role,
                action=action,
                api_key=None if raw_key is None else SensitiveValue(raw_key),
                now=datetime.now(UTC),
            )
            if not accepted:
                raise _RequestRejectedError
        except KeyError, TypeError, ValueError, json.JSONDecodeError, WebAppAuthenticationError:
            return rejected
        return _headers(Response(status_code=204))

    async def method_not_allowed(_: Request, __: Exception) -> Response:
        return _headers(JSONResponse({"ok": False, "code": "REQUEST_REJECTED"}, 405))

    routes = [
        Route("/webapp/model-key", page, methods=["GET"]),
        Route("/model-key.js", script, methods=["GET"]),
        Route("/model-key.css", style, methods=["GET"]),
        Route("/api/v1/model-keys/{role:str}", mutate, methods=["POST"]),
    ]
    return Starlette(debug=False, routes=routes, exception_handlers={405: method_not_allowed})
