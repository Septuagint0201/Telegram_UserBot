import hashlib
import hmac
import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import pytest
import uvicorn
from playwright.sync_api import Route, sync_playwright

from telegram_userbot.adapters.webapp import TelegramInitDataVerifier, create_key_web_app
from telegram_userbot.adapters.webapp.auth import TelegramWebIdentity
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

ROOT = Path(__file__).resolve().parents[2]
BOT_TOKEN = "123456:SYNTHETIC_BROWSER_BOT_TOKEN"  # noqa: S105 - synthetic fixture
ADMIN_ID = 42
pytestmark = [pytest.mark.integration, pytest.mark.browser]


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _init_data(now: datetime) -> str:
    values = {
        "auth_date": str(int(now.timestamp())),
        "query_id": "SYNTHETIC_BROWSER_QUERY",
        "user": json.dumps({"id": ADMIN_ID}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MutationFake:
    def __init__(self) -> None:
        self.received = False

    async def mutate(  # noqa: PLR0913 - protocol fixture
        self,
        *,
        identity: TelegramWebIdentity,
        launch_token: SensitiveValue[str],
        role: LogicalRole,
        action: str,
        api_key: SensitiveValue[str] | None,
        now: datetime,
    ) -> bool:
        assert identity.user_id == ADMIN_ID
        assert launch_token.reveal_for_use() == "L" * 43
        assert role is LogicalRole.MAIN_AI
        assert action == "set"
        assert api_key is not None
        assert api_key.reveal_for_use() == "SYNTHETIC_BROWSER_KEY"
        assert now.tzinfo
        self.received = True
        return True


@pytest.mark.integration
@pytest.mark.browser
def test_key_only_webapp_in_pinned_chromium_has_no_external_resources_or_storage(  # noqa: PLR0915
) -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    mutation = MutationFake()
    app = create_key_web_app(
        verifier=TelegramInitDataVerifier(
            bot_token=SensitiveValue(BOT_TOKEN), allowed_admin_ids=frozenset({ADMIN_ID})
        ),
        mutation_port=mutation,
        public_origin=origin,
        allow_insecure_loopback=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    browser_version = ""
    external_requests: list[str] = []
    storage: dict[str, int] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser_version = browser.version
            context = browser.new_context(base_url=origin)

            def constrain(route: Route) -> None:
                hostname = urlsplit(route.request.url).hostname
                if hostname not in {"127.0.0.1", "::1"}:
                    external_requests.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", constrain)
            page = context.new_page()
            page.add_init_script("window.Telegram={WebApp:{close:()=>{window.__closed=true;}}};")
            launch_fragment = urlencode(
                {
                    "tgWebAppData": _init_data(datetime.now(UTC)),
                    "role": "main_ai",
                    "action": "set",
                    "launch": "L" * 43,
                }
            )
            page.goto(
                "/webapp/model-key#" + launch_fragment,
                wait_until="networkidle",
            )
            page.locator("#api-key").fill("SYNTHETIC_BROWSER_KEY")
            page.locator("button").click()
            page.locator("#result").get_by_text("Saved.").wait_for()
            assert page.locator("#api-key").input_value() == ""
            assert page.evaluate("window.location.hash") == ""
            storage = page.evaluate(
                "async () => ({local: localStorage.length, session: sessionStorage.length, "
                "indexed: (await indexedDB.databases()).length, "
                "workers: (await navigator.serviceWorker.getRegistrations()).length})"
            )
            resource_hosts = page.evaluate(
                "() => performance.getEntriesByType('resource')"
                ".map(item => new URL(item.name).hostname)"
            )
            assert set(resource_hosts) <= {"127.0.0.1", "::1"}
            assert page.evaluate("window.__closed === true")
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert mutation.received
    assert external_requests == []
    assert storage == {"local": 0, "session": 0, "indexed": 0, "workers": 0}
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "browser": "chromium",
        "browser_version": browser_version,
        "external_request_count": 0,
        "storage_entries": 0,
        "credential_echo": False,
        "network_scope": "loopback-only",
    }
    output = ROOT / ".artifacts" / "m2" / "browser-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
