from __future__ import annotations

import asyncio
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import telegram_userbot.processes.app as app_module
from telegram_userbot.application.ports.model import ModelGateway, ModelRequest, ModelResponse
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.config.production import (
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.platform.health.status import ServiceStatusCode
from telegram_userbot.platform.runtime import TerminationDeadline
from telegram_userbot.processes.app import ProductionAppError, ProductionAppRuntime, run
from tests.unit.platform.test_m8_health import NOW

_GIB = 1024**3


class _ReadyScheduler:
    def __init__(self, *, error: bool = False, order: list[str] | None = None) -> None:
        self._error = error
        self._order = order

    def ready(self) -> bool:
        if self._error:
            raise RuntimeError("synthetic scheduler probe failure")
        return True

    def wake(self) -> None:
        return None

    def bind_pending_image_recovery(self, callback: object) -> None:
        assert callable(callback)

    def bind_media_cleanup(self, callback: object) -> None:
        assert callable(callback)

    async def run(self, _context: object) -> None:
        return None

    async def drain(self, _deadline: TerminationDeadline) -> None:
        if self._order is not None:
            self._order.append("scheduler")


class _Redis:
    started = True

    async def probe(self) -> bool:
        return True

    async def publish_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.APP


class _Ownership:
    acquired = True

    async def probe(self) -> bool:
        return True


class _Telethon:
    started = True

    def __init__(self, order: list[str] | None = None) -> None:
        self._order = order

    async def close(self) -> None:
        if self._order is not None:
            self._order.append("telethon")


def _health_runtime(tmp_path: Path, *, scheduler_error: bool) -> Any:
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._settings = SimpleNamespace(bootstrap_maintenance=False)
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_and_restore_ready = AsyncMock(return_value=(True, True, True))
    runtime._redis = _Redis()
    runtime._ownership = _Ownership()
    runtime._telethon = _Telethon()
    runtime._scheduler = _ReadyScheduler(error=scheduler_error)
    runtime._media_root = tmp_path
    runtime._managed_process = None
    runtime._disk_blocked_event = asyncio.Event()
    runtime._disk_recovery_required = False
    runtime._serve_loop_running = True
    runtime._record_service_status = AsyncMock()
    return runtime


class _ManagedProcess:
    def __init__(self) -> None:
        self.draining = False
        self.drain_requests = 0
        self._drain = asyncio.Event()

    def request_drain(self) -> bool:
        self.drain_requests += 1
        self.draining = True
        self._drain.set()
        return True

    async def wait_for_drain(self) -> None:
        await self._drain.wait()

    def release(self) -> None:
        self.draining = True
        self._drain.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_health_uses_live_dependency_probes_and_scheduler_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = _health_runtime(tmp_path, scheduler_error=True)

    state = await runtime.health(UtcTimestamp(NOW.value))

    assert state.service is ServiceName.APP
    assert state.process_loop_ok
    assert state.database_ok
    assert state.redis_ok
    assert state.schema_ok
    assert state.restore_gate_open
    assert state.account_ready is True
    assert state.session_owned is True
    assert state.telegram_ready is True
    assert not state.required_config_ok
    runtime._schema_ready.assert_awaited_once()
    runtime._database_account_and_restore_ready.assert_awaited_once()
    runtime._record_service_status.assert_awaited_once_with(
        state,
        UtcTimestamp(NOW.value),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_disk_block_is_sticky_without_requesting_restart(
    tmp_path: Path,
) -> None:
    runtime = _health_runtime(tmp_path, scheduler_error=False)
    process = _ManagedProcess()
    runtime._managed_process = process
    runtime._filesystem_admission = AsyncMock(
        side_effect=(
            disk_admission(total_bytes=100 * _GIB, available_bytes=5 * _GIB),
            disk_admission(total_bytes=100 * _GIB, available_bytes=20 * _GIB),
        )
    )

    blocked = await runtime.health(UtcTimestamp(NOW.value))
    recovered_capacity = await runtime.health(UtcTimestamp(NOW.value))

    assert not blocked.disk_safety_ok
    assert not recovered_capacity.disk_safety_ok
    assert runtime._disk_recovery_required
    assert runtime._disk_blocked_event.is_set()
    assert process.drain_requests == 0
    assert not process.draining
    assert (
        runtime._failure_code(replace(blocked, required_config_ok=False))
        is ServiceStatusCode.DISK_SAFETY_NOT_READY
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_disk_block_disconnects_once_and_waits_for_operator_restart() -> None:
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._disk_blocked_event = asyncio.Event()
    runtime._disk_recovery_required = False
    runtime._scheduler = _ReadyScheduler()
    runtime._close_started_resources = AsyncMock()
    process = _ManagedProcess()

    blocked = asyncio.create_task(runtime._wait_in_disk_blocked_state(process))
    await asyncio.sleep(0)

    assert not blocked.done()
    runtime._close_started_resources.assert_awaited_once()
    process.release()
    await blocked

    runtime._close_started_resources.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_drain_stops_scheduler_before_closing_sole_telethon_owner() -> None:
    order: list[str] = []
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._scheduler = _ReadyScheduler(order=order)
    runtime._telethon = _Telethon(order)
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(1.0),
        grace_seconds=10.0,
    )

    await runtime.drain(deadline)

    assert order == ["scheduler", "telethon"]


@pytest.mark.unit
def test_app_entrypoint_rejects_missing_production_configuration_without_details() -> None:
    stderr = StringIO()

    assert run([], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "APP_CONFIGURATION_REJECTED\n"


class _Model:
    async def generate(self, _request: ModelRequest) -> ModelResponse:
        raise AssertionError("provider call is outside composition")

    def invalidate_profile(self, _profile_id: UUID, *, minimum_version: int | None = None) -> int:
        del minimum_version
        return 0


class _Engine:
    async def dispose(self) -> None:
        return None


class _Keyring:
    def derive_runtime_key(self, purpose: bytes) -> SensitiveValue[bytes]:
        assert purpose == b"model-input-fingerprint"
        return SensitiveValue(b"h" * 32)


class _SessionContext:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _production_settings() -> ProductionSettings:
    identity = SimpleNamespace(
        account_id=UUID(int=1),
        telegram_user_id=1000,
        control_bot_username="SyntheticControlBot",
        control_admin_user_ids=(1000,),
    )
    return cast(
        ProductionSettings,
        SimpleNamespace(
            process=ProductionProcess.APP,
            bootstrap_maintenance=False,
            deployment=SimpleNamespace(
                deployment_id="synthetic-deployment",
                source_commit="1" * 40,
                runtime_identity=identity,
            ),
            database=SimpleNamespace(
                host="postgres",
                port=5432,
                database="telegram_userbot",
                login_role="app_login",
                runtime_role="app_runtime",
                password_secret_id="app_database_password",  # noqa: S106
                sslmode="require",
            ),
            redis=SimpleNamespace(
                host="redis",
                port=6379,
                password_secret_id="redis_password",  # noqa: S106
            ),
        ),
    )


def _production_secrets() -> SecretBundle:
    return SecretBundle(
        (
            ("app_database_password", SensitiveValue(b"d" * 32)),
            ("redis_password", SensitiveValue(b"r" * 32)),
            ("credential_master_keyring", SensitiveValue(b"opaque-keyring")),
        )
    )


@pytest.mark.unit
def test_app_composition_builds_production_model_and_scheduler_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model = _Model()
    scheduler = _ReadyScheduler()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app_module,
        "create_postgres_engine",
        lambda _settings: cast(AsyncEngine, _Engine()),
    )
    monkeypatch.setattr(
        app_module, "parse_credential_keyring", lambda *_args, **_kwargs: _Keyring()
    )

    def build_model(sessions: object, **kwargs: object) -> ModelGateway:
        captured["sessions"] = sessions
        captured.update(kwargs)
        return model

    monkeypatch.setattr(app_module, "build_production_model_gateway", build_model)
    monkeypatch.setattr(
        app_module,
        "build_production_app_scheduler",
        lambda settings: scheduler if settings is not None else None,
    )

    runtime = ProductionAppRuntime(
        _production_settings(),
        _production_secrets(),
        session_path=tmp_path / "account.session",
        media_root=tmp_path / "media",
    )

    assert runtime._model is model
    assert runtime._scheduler is scheduler
    assert captured["sessions"] is runtime._sessions
    assert captured["media_root"] == tmp_path / "media"
    assert isinstance(captured["input_hmac_key"], SensitiveValue)


@pytest.mark.unit
def test_app_composition_failure_is_stable_and_does_not_echo_secret_or_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_marker = "must-not-appear"  # noqa: S105 - synthetic leak sentinel
    endpoint_marker = "private-endpoint.invalid"
    settings = _production_settings()
    settings.database.host = endpoint_marker  # type: ignore[misc]
    secrets = SecretBundle(
        (
            ("app_database_password", SensitiveValue(secret_marker.encode())),
            ("redis_password", SensitiveValue(b"r" * 32)),
            ("credential_master_keyring", SensitiveValue(b"opaque-keyring")),
        )
    )
    monkeypatch.setattr(
        app_module,
        "create_postgres_engine",
        lambda _settings: cast(AsyncEngine, _Engine()),
    )

    def reject_keyring(*_args: object, **_kwargs: object) -> object:
        raise app_module.CredentialCryptoError("CREDENTIAL_KEYRING_JSON_INVALID")  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "parse_credential_keyring", reject_keyring)

    with pytest.raises(ProductionAppError) as captured:
        ProductionAppRuntime(
            settings,
            secrets,
            session_path=tmp_path / "account.session",
            media_root=tmp_path / "media",
        )
    rendered = str(captured.value)
    assert rendered == "APP_MODEL_GATEWAY_CONFIGURATION_INVALID"
    assert secret_marker not in rendered
    assert endpoint_marker not in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_runs_one_bounded_cleanup_batch_through_its_owned_media_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()
    store = object()
    captured: dict[str, object] = {}

    class _Cleanup:
        def __init__(self, *, repository: object, store: object, account_id: object) -> None:
            captured["account_id"] = account_id
            captured["repository"] = repository
            captured["store"] = store

        async def run_once(self, *, now: object, limit: int) -> object:
            captured["now"] = now
            captured["limit"] = limit
            return SimpleNamespace(deleted=2, already_missing=1, failed=0)

    monkeypatch.setattr(app_module, "DurableMediaCleanup", _Cleanup)
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._sessions = lambda: _SessionContext(session)
    runtime._media_store = store
    account_id = UUID(int=88)
    runtime._settings = SimpleNamespace(
        deployment=SimpleNamespace(runtime_identity=SimpleNamespace(account_id=account_id))
    )

    assert await runtime._cleanup_expired_media_once() == 3
    assert captured["store"] is store
    assert captured["account_id"] == account_id
    assert cast(Any, captured["repository"])._session is session
    assert captured["limit"] == app_module.MEDIA_RECONCILE_LIMIT
