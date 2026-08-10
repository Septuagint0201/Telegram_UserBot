import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from telegram_userbot.adapters.telegram_bot import (
    ControlBotModelController,
    ControlSessionPrompt,
    IssuedKeyLaunch,
    ModelProfileSummary,
)
from telegram_userbot.adapters.webapp import (
    LaunchTokenCodec,
    TelegramInitDataVerifier,
    WebAppAuthenticationError,
    create_key_web_app,
)
from telegram_userbot.adapters.webapp.auth import TelegramWebIdentity
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

BOT_TOKEN = "123456:SYNTHETIC_BOT_TOKEN"  # noqa: S105 - synthetic fixture
ADMIN_ID = 42
ORIGIN = "https://keys.example.invalid"


def signed_init_data(
    now: datetime,
    *,
    user_id: int = ADMIN_ID,
    query_id: str = "SYNTHETIC_QUERY",
    bot_token: str = BOT_TOKEN,
) -> str:
    values = {
        "auth_date": str(int(now.timestamp())),
        "query_id": query_id,
        "user": json.dumps({"id": user_id, "first_name": "Synthetic"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.unit
def test_telegram_init_data_auth_tamper_expiry_future_admin_and_duplicates() -> None:
    now = datetime.now(UTC)
    verifier = TelegramInitDataVerifier(
        bot_token=SensitiveValue(BOT_TOKEN),
        allowed_admin_ids=frozenset({ADMIN_ID}),
    )
    identity = verifier.verify(signed_init_data(now), now=now)
    assert identity.user_id == ADMIN_ID
    assert len(identity.query_id_hash) == 32
    rejected = (
        signed_init_data(now).replace("Synthetic", "Tampered"),
        signed_init_data(now - timedelta(minutes=10)),
        signed_init_data(now + timedelta(minutes=2)),
        signed_init_data(now, user_id=999),
        f"{signed_init_data(now)}&query_id=duplicate",
    )
    for raw in rejected:
        with pytest.raises(WebAppAuthenticationError, match="REJECTED"):
            verifier.verify(raw, now=now)


@pytest.mark.unit
def test_launch_token_is_random_secret_safe_and_pepper_bound() -> None:
    codec = LaunchTokenCodec(SensitiveValue(b"p" * 32))
    first, second = codec.issue(), codec.issue()
    assert first.digest != second.digest
    assert first.digest == codec.digest(first.token)
    assert first.token.reveal_for_use() not in repr(first)
    with pytest.raises(WebAppAuthenticationError, match="REJECTED"):
        codec.digest(SensitiveValue("short"))


class MutationFake:
    def __init__(self) -> None:
        self.calls: list[tuple[int, LogicalRole, str, str | None]] = []
        self.accept = True

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
        assert launch_token.reveal_for_use() == "L" * 43
        assert now.tzinfo is not None
        self.calls.append(
            (
                identity.user_id,
                role,
                action,
                None if api_key is None else api_key.reveal_for_use(),
            )
        )
        return self.accept


@pytest.mark.unit
async def test_key_only_web_app_accepts_write_without_echo_or_config_api() -> None:
    now = datetime.now(UTC)
    mutation = MutationFake()
    app = create_key_web_app(
        verifier=TelegramInitDataVerifier(
            bot_token=SensitiveValue(BOT_TOKEN), allowed_admin_ids=frozenset({ADMIN_ID})
        ),
        mutation_port=mutation,
        public_origin=ORIGIN,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        page = await client.get("/webapp/model-key")
        assert page.status_code == 200
        assert page.headers["cache-control"].startswith("no-store")
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert "endpoint" not in page.text.lower()
        assert (await client.get("/api/v1/models")).status_code == 404
        response = await client.post(
            "/api/v1/model-keys/main_ai",
            headers={
                "origin": ORIGIN,
                "x-telegram-init-data": signed_init_data(now),
                "x-model-key-launch": "L" * 43,
            },
            json={"action": "set", "api_key": "SYNTHETIC_WEB_KEY"},
        )
        assert response.status_code == 204
        assert response.content == b""
        assert mutation.calls == [(ADMIN_ID, LogicalRole.MAIN_AI, "set", "SYNTHETIC_WEB_KEY")]


@pytest.mark.unit
async def test_key_web_app_rejects_origin_shape_auth_and_backend_without_echo() -> None:
    now = datetime.now(UTC)
    mutation = MutationFake()
    app = create_key_web_app(
        verifier=TelegramInitDataVerifier(
            bot_token=SensitiveValue(BOT_TOKEN), allowed_admin_ids=frozenset({ADMIN_ID})
        ),
        mutation_port=mutation,
        public_origin=ORIGIN,
    )
    base_headers = {
        "origin": ORIGIN,
        "x-telegram-init-data": signed_init_data(now),
        "x-model-key-launch": "L" * 43,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        requests = (
            ({**base_headers, "origin": "https://evil.invalid"}, {"action": "delete"}),
            (base_headers, {"action": "set", "api_key": ""}),
            (base_headers, {"action": "set", "api_key": "SYNTHETIC", "endpoint": "bad"}),
            ({**base_headers, "x-telegram-init-data": "bad"}, {"action": "delete"}),
        )
        for headers, payload in requests:
            response = await client.post(
                "/api/v1/model-keys/main_ai", headers=headers, json=payload
            )
            assert response.status_code == 400
            assert "SYNTHETIC" not in response.text
        mutation.accept = False
        response = await client.post(
            "/api/v1/model-keys/main_ai", headers=base_headers, json={"action": "delete"}
        )
        assert response.status_code == 400
        assert response.json() == {"ok": False, "code": "REQUEST_REJECTED"}


class BotBackendFake:
    def __init__(self) -> None:
        self.input_values: list[str] = []

    async def list_profiles(self) -> tuple[ModelProfileSummary, ...]:
        return (
            ModelProfileSummary(
                LogicalRole.MAIN_AI,
                "active",
                "openai_responses",
                "synthetic-model",
                "public",
                "active",
                2,
                3,
            ),
        )

    async def start_config(
        self, *, admin_id: int, role: LogicalRole, now: datetime
    ) -> ControlSessionPrompt:
        assert admin_id == ADMIN_ID
        assert role is LogicalRole.MAIN_AI
        assert now.tzinfo
        return ControlSessionPrompt("endpoint", "Enter endpoint URL.", 1)

    async def apply_session_input(
        self, *, admin_id: int, value: str, now: datetime
    ) -> ControlSessionPrompt | None:
        self.input_values.append(value)
        return None

    async def cancel(self, *, admin_id: int, now: datetime) -> bool:
        return admin_id == ADMIN_ID and now.tzinfo is not None

    async def validate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool:
        return role is LogicalRole.MAIN_AI

    async def activate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool:
        return False

    async def issue_key_launch(
        self, *, admin_id: int, role: LogicalRole, action: str, now: datetime
    ) -> IssuedKeyLaunch:
        return IssuedKeyLaunch(role, action, SensitiveValue("L" * 43))


@pytest.mark.unit
async def test_control_bot_routes_nonsecret_config_and_key_only_web_link() -> None:
    backend = BotBackendFake()
    controller = ControlBotModelController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend, web_app_origin=ORIGIN
    )
    now = datetime.now(UTC)
    assert (
        "openai_responses"
        in (await controller.handle(admin_id=ADMIN_ID, message_text="/models", now=now)).text
    )
    assert (
        "endpoint"
        in (
            await controller.handle(
                admin_id=ADMIN_ID, message_text="/model_config main_ai", now=now
            )
        ).text.lower()
    )
    shown = await controller.handle(admin_id=ADMIN_ID, message_text="/model_show main_ai", now=now)
    assert "synthetic-model" in shown.text
    cancelled = await controller.handle(admin_id=ADMIN_ID, message_text="/model_cancel", now=now)
    assert cancelled.text == "Draft cancelled."
    await controller.handle(admin_id=ADMIN_ID, message_text="synthetic-model-v2", now=now)
    assert backend.input_values == ["synthetic-model-v2"]
    launch = await controller.handle(
        admin_id=ADMIN_ID, message_text="/model_key main_ai replace", now=now
    )
    assert launch.web_app_url is not None
    assert "#role=main_ai&action=replace&launch=" in launch.web_app_url.reveal_for_use()
    assert "LLLL" not in repr(launch)
    assert (
        "unchanged"
        in (
            await controller.handle(
                admin_id=ADMIN_ID, message_text="/model_activate main_ai", now=now
            )
        ).text
    )


@pytest.mark.unit
async def test_control_bot_rejects_api_key_messages_without_backend_persistence() -> None:
    backend = BotBackendFake()
    controller = ControlBotModelController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend, web_app_origin=ORIGIN
    )
    reply = await controller.handle(
        admin_id=ADMIN_ID,
        message_text="sk-SYNTHETICKEY1234567890",
        now=datetime.now(UTC),
    )
    assert "only" in reply.text
    assert backend.input_values == []
    assert (
        await controller.handle(admin_id=999, message_text="/models", now=datetime.now(UTC))
    ).text == "Request rejected."


@pytest.mark.unit
async def test_control_bot_rejects_invalid_command_shapes_without_mutation() -> None:
    backend = BotBackendFake()
    controller = ControlBotModelController(
        allowed_admin_ids=frozenset({ADMIN_ID}), backend=backend, web_app_origin=ORIGIN
    )
    now = datetime.now(UTC)
    cases = (
        ("", "No input"),
        ('/model_config "unterminated', "Invalid command syntax"),
        ("/model_show unknown", "Unknown model role"),
        ("/model_key main_ai read", "Usage"),
        ("/unknown", "Unknown model command"),
    )
    for message, expected in cases:
        assert (
            expected
            in (await controller.handle(admin_id=ADMIN_ID, message_text=message, now=now)).text
        )
    assert backend.input_values == []
    with pytest.raises(ValueError, match="invalid"):
        ControlBotModelController(
            allowed_admin_ids=frozenset(), backend=backend, web_app_origin=ORIGIN
        )
