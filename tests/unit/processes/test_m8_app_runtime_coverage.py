"""Fake-first lifecycle coverage for the app-owned production runtime."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from tests.unit.platform.test_m8_health import NOW

import telegram_userbot.processes.app as app_module
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.config.production import ProductionSettings
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.processes.app import ProductionAppError, ProductionAppRuntime, run


class _Observer:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.stop_calls = 0
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def stop(self) -> None:
        self.stop_calls += 1


class _Ownership:
    def __init__(self) -> None:
        self.acquired = False
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> None:
        self.acquire_calls += 1
        self.acquired = True

    async def release(self) -> None:
        self.release_calls += 1
        self.acquired = False

    async def probe(self) -> bool:
        return True


class _Redis:
    def __init__(self, *, heartbeat_error: bool = False) -> None:
        self.started = False
        self.connect_calls = 0
        self.clear_calls = 0
        self.close_calls = 0
        self._heartbeat_error = heartbeat_error

    async def connect(self, *, with_arq: bool) -> None:
        assert with_arq
        self.connect_calls += 1
        self.started = True

    async def clear_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.APP
        self.clear_calls += 1

    async def close(self) -> None:
        self.close_calls += 1
        self.started = False

    async def probe(self) -> bool:
        return True

    async def publish_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.APP
        if self._heartbeat_error:
            raise RuntimeError("synthetic heartbeat failure")


class _Engine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _TelethonRuntime:
    instances: ClassVar[list[_TelethonRuntime]] = []

    def __init__(self, settings: object, **callbacks: object) -> None:
        self.settings: Any = settings
        self.callbacks = callbacks
        self.client = object()
        self.started = False
        self.close_calls = 0
        type(self).instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.close_calls += 1
        self.started = False


class _FailingTelethonRuntime(_TelethonRuntime):
    async def start(self) -> None:
        raise RuntimeError("synthetic telethon startup failure")


class _ServeProcess:
    def __init__(self) -> None:
        self.draining = False


class _ServeTelethon:
    def __init__(self, process: _ServeProcess) -> None:
        self._process = process
        self.run_calls = 0

    async def run_until_disconnected(self) -> None:
        self.run_calls += 1
        self._process.draining = True


class _BlockingScheduler:
    def __init__(self) -> None:
        self.context: object | None = None
        self.cancelled = False

    async def run(self, context: object) -> None:
        self.context = context
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _runtime_for_start(tmp_path: Path) -> tuple[Any, _Ownership, _Redis, _Engine, _Observer]:
    identity = SimpleNamespace(account_id=uuid7(), telegram_user_id=1000)
    ownership = _Ownership()
    redis = _Redis()
    engine = _Engine()
    observer = _Observer()
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._settings = SimpleNamespace(
        bootstrap_maintenance=False,
        deployment=SimpleNamespace(
            deployment_id="synthetic-deployment",
            source_commit="1" * 40,
            runtime_identity=identity,
        ),
    )
    runtime._instance_id = uuid7()
    runtime._started_at = NOW.value
    runtime._started = False
    runtime._closed = False
    runtime._managed_process = None
    runtime._require_start_admission = AsyncMock()
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_and_restore_ready = AsyncMock(return_value=(True, True, True))
    runtime._ownership = ownership
    runtime._redis = redis
    runtime._telethon = None
    runtime._telegram_gateway = None
    runtime._media_ingestion = None
    runtime._runtime_marker_observer = observer
    runtime._session_path = tmp_path / "account.session"
    runtime._snapshot_path = tmp_path / "health.json"
    runtime._secret_ascii = lambda secret_id, _code: {
        "telegram_api_id": "12345",
        "telegram_api_hash": "a" * 32,
    }[secret_id]
    peer_resolver = object()
    runtime._peer_resolver = lambda: peer_resolver
    runtime._load_account_watermark = AsyncMock()
    runtime._reconcile_outbound_message_id = AsyncMock()
    runtime._ingest_batch = AsyncMock()
    media_ingestion = object()
    runtime._build_media_ingestion = lambda: media_ingestion
    runtime._outbound_peer = AsyncMock()
    runtime._new_uuid = uuid7
    runtime._engine = engine
    return runtime, ownership, redis, engine, observer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_start_connects_owned_dependencies_and_close_releases_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _TelethonRuntime.instances.clear()
    monkeypatch.setattr(app_module, "TelethonSessionRuntime", _TelethonRuntime)
    monkeypatch.setattr(app_module, "TelethonBoundPeerResolver", lambda _callback: object())
    monkeypatch.setattr(
        app_module,
        "TelethonTelegramGateway",
        lambda client, _resolver: SimpleNamespace(client=client),
    )
    runtime, ownership, redis, engine, observer = _runtime_for_start(tmp_path)

    await runtime.start()

    telethon = _TelethonRuntime.instances[-1]
    assert runtime._started
    assert ownership.acquire_calls == 1
    assert redis.connect_calls == 1
    assert telethon.started
    assert telethon.settings.api_id == 12345
    assert runtime._telegram_gateway.client is telethon.client
    assert runtime._media_ingestion is not None

    await runtime.close()
    await runtime.close()

    assert telethon.close_calls == 1
    assert redis.clear_calls == 1
    assert redis.close_calls == 1
    assert ownership.release_calls == 1
    assert engine.dispose_calls == 1
    assert observer.stop_calls == 1
    assert runtime._closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_start_failure_after_connect_rolls_back_every_acquired_resource(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FailingTelethonRuntime.instances.clear()
    monkeypatch.setattr(app_module, "TelethonSessionRuntime", _FailingTelethonRuntime)
    monkeypatch.setattr(app_module, "TelethonBoundPeerResolver", lambda _callback: object())
    runtime, ownership, redis, _engine, observer = _runtime_for_start(tmp_path)

    with pytest.raises(RuntimeError, match=r"^synthetic telethon startup failure$"):
        await runtime.start()

    telethon = _FailingTelethonRuntime.instances[-1]
    assert telethon.close_calls == 1
    assert not runtime._started
    assert runtime._telethon is None
    assert runtime._telegram_gateway is None
    assert redis.clear_calls == 1
    assert redis.close_calls == 1
    assert ownership.release_calls == 1
    assert observer.stop_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_run_managed_preserves_primary_error_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime, _ownership, _redis, _engine, _observer = _runtime_for_start(tmp_path)

    class Process:
        force_termination_requested = False
        termination_deadline = None

        def __init__(self, **_kwargs: object) -> None:
            return None

        async def run(self, _serve: object) -> None:
            raise RuntimeError("synthetic process failure")

    monkeypatch.setattr(app_module, "ManagedProcess", Process)
    runtime.close = AsyncMock(side_effect=RuntimeError("synthetic close failure"))

    with pytest.raises(RuntimeError, match="synthetic process failure"):
        await runtime.run_managed()

    runtime.close.assert_awaited_once_with()
    assert runtime._managed_process is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_ready", "database_state", "expected_code"),
    [
        (False, (True, True, True), "APP_SCHEMA_NOT_READY"),
        (True, (False, True, True), "APP_DATABASE_UNAVAILABLE"),
        (True, (True, False, True), "APP_ACCOUNT_NOT_READY"),
        (True, (True, True, False), "APP_RESTORE_GATE_CLOSED"),
    ],
)
async def test_app_start_rejects_unready_durable_prerequisites(
    tmp_path: Path,
    schema_ready: bool,
    database_state: tuple[bool, bool, bool],
    expected_code: str,
) -> None:
    runtime, ownership, redis, _engine, _observer = _runtime_for_start(tmp_path)
    runtime._schema_ready = AsyncMock(return_value=schema_ready)
    runtime._database_account_and_restore_ready = AsyncMock(return_value=database_state)

    with pytest.raises(ProductionAppError, match=rf"^{expected_code}$"):
        await runtime.start()

    assert ownership.acquire_calls == 0
    assert redis.connect_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_health_marks_failed_redis_heartbeat_and_unready_marker_not_ready(
    tmp_path: Path,
) -> None:
    runtime, _ownership, _redis, _engine, _observer = _runtime_for_start(tmp_path)
    runtime._settings.bootstrap_maintenance = False
    runtime._settings.deployment.deployment_id = "synthetic-deployment"
    runtime._settings.deployment.source_commit = "1" * 40
    runtime._redis = _Redis(heartbeat_error=True)
    runtime._redis.started = True
    runtime._telethon = SimpleNamespace(started=True)
    runtime._ownership.acquired = True
    runtime._scheduler = SimpleNamespace(ready=lambda: True)
    runtime._runtime_marker_observer = _Observer(ready=False)
    runtime._media_root = tmp_path
    runtime._filesystem_admission = AsyncMock(
        return_value=disk_admission(total_bytes=100 * 1024**3, available_bytes=50 * 1024**3)
    )
    runtime._disk_recovery_required = False
    runtime._disk_blocked_event = asyncio.Event()
    runtime._serve_loop_running = True
    runtime._record_service_status = AsyncMock()

    state = await runtime.health(UtcTimestamp(NOW.value))

    assert state.database_ok
    assert not state.redis_ok
    assert not state.required_config_ok
    runtime._record_service_status.assert_awaited_once_with(state, UtcTimestamp(NOW.value))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_serve_stops_sibling_tasks_after_telethon_drains_normally() -> None:
    process = _ServeProcess()
    scheduler = _BlockingScheduler()
    observer = _Observer()
    telethon = _ServeTelethon(process)
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._managed_process = None
    runtime._serve_loop_running = False
    runtime._start_for_serve = AsyncMock(return_value=True)
    runtime._operation_admission = AsyncMock(return_value=True)
    runtime._telegram_gateway = object()
    runtime._sessions = object()
    runtime._model = object()
    runtime._redis = object()
    runtime._instance_id = UUID(int=3)
    runtime._require_telethon = lambda: telethon
    runtime._scheduler = scheduler
    runtime._disk_blocked_event = asyncio.Event()
    runtime._runtime_marker_observer = observer

    await runtime.serve(process)

    assert telethon.run_calls == 1
    assert process.draining
    assert scheduler.context is not None
    assert scheduler.cancelled
    assert observer.cancelled
    assert observer.stop_calls == 1
    assert not runtime._serve_loop_running
    assert runtime._managed_process is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_serve_fails_closed_when_start_did_not_supply_a_gateway() -> None:
    process = _ServeProcess()
    observer = _Observer()
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._managed_process = None
    runtime._serve_loop_running = False
    runtime._start_for_serve = AsyncMock(return_value=True)
    runtime._operation_admission = AsyncMock(return_value=True)
    runtime._telegram_gateway = None
    runtime._runtime_marker_observer = observer

    with pytest.raises(ProductionAppError, match=r"^APP_TELEGRAM_GATEWAY_UNAVAILABLE$"):
        await runtime.serve(process)

    assert observer.stop_calls == 1
    assert runtime._managed_process is None


@pytest.mark.unit
def test_app_entrypoint_rejects_arguments_and_maps_unexpected_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = StringIO()
    assert run(["unexpected"], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "APP_ARGUMENT_INVALID\n"

    class _Settings:
        def load_secrets(self) -> object:
            return object()

    class _Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def run_managed(self) -> None:
            raise RuntimeError("synthetic runtime failure")

    monkeypatch.setattr(ProductionSettings, "load", lambda *_args: _Settings())
    monkeypatch.setattr(app_module, "ProductionAppRuntime", _Runtime)
    stderr = StringIO()

    assert run([], {}, stderr=stderr) == 1
    assert stderr.getvalue() == "APP_RUNTIME_FAILED\n"
