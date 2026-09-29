"""Fake-first coverage of control process lifecycle and readiness boundaries."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.processes.control as control_module
from telegram_userbot.adapters.queue.redis import RedisRuntimeError
from telegram_userbot.adapters.telegram_bot.http import TelegramBotAPI
from telegram_userbot.adapters.telegram_bot.polling import ControlBotPoller
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.config.production import (
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.control import (
    ControlComponents,
    ControlProcessError,
    DurablePreviewDeletionMaintenance,
    ProductionControlApplication,
    build_control_application,
)

NOW = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


class _RedisFake:
    def __init__(self, *, heartbeat_failure: bool = False) -> None:
        self.started = False
        self.connect_calls: list[bool] = []
        self.close_calls = 0
        self.heartbeat_failure = heartbeat_failure

    async def connect(self, *, with_arq: bool) -> None:
        self.connect_calls.append(with_arq)
        self.started = True

    async def probe(self) -> bool:
        return True

    async def publish_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.CONTROL
        if self.heartbeat_failure:
            raise RedisRuntimeError("synthetic heartbeat failure")

    async def clear_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.CONTROL

    async def close(self) -> None:
        self.close_calls += 1
        self.started = False


class _WebServerFake:
    def __init__(self) -> None:
        self.started = False
        self._should_exit = False
        self.started_event = asyncio.Event()
        self._release = asyncio.Event()

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self._release.set()

    async def serve(self) -> None:
        self.started = True
        self.started_event.set()
        await self._release.wait()


class _PollerFake:
    identity_verified = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    async def run(self, stop: asyncio.Event) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        await stop.wait()


class _PreviewMaintenanceFake:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self, *, now: datetime) -> int:
        assert now.tzinfo is not None
        self.calls += 1
        return 0


class _ApiFake:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _EngineFake:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _ProcessFake:
    def __init__(self, *, accepting_new_work: bool = True) -> None:
        self.accepting_new_work = accepting_new_work
        self.draining = not accepting_new_work
        self._drain = asyncio.Event()

    async def wait_for_drain(self) -> None:
        await self._drain.wait()

    def request_drain(self) -> None:
        self.accepting_new_work = False
        self.draining = True
        self._drain.set()


class _RuntimeMarkerObserverFake:
    def __init__(self, **_kwargs: object) -> None:
        self.ready = True
        self._stop = asyncio.Event()
        self.run_started = asyncio.Event()
        self.stop_calls = 0

    async def run(self) -> None:
        self.run_started.set()
        await self._stop.wait()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()


def _unavailable_sessions() -> AsyncSession:
    raise RuntimeError("synthetic database access is not expected")


def _components(
    *,
    bootstrap_maintenance: bool = False,
    redis: _RedisFake | None = None,
    poller: _PollerFake | None = None,
) -> tuple[ControlComponents, _RedisFake, _WebServerFake, _PollerFake, _ApiFake, _EngineFake]:
    resolved_redis = redis or _RedisFake()
    resolved_poller = poller or _PollerFake()
    web = _WebServerFake()
    api = _ApiFake()
    engine = _EngineFake()
    settings = cast(
        ProductionSettings,
        SimpleNamespace(
            bootstrap_maintenance=bootstrap_maintenance,
            deployment=SimpleNamespace(
                deployment_id="synthetic-deployment", source_commit="a" * 40
            ),
        ),
    )
    components = ControlComponents(
        settings=settings,
        engine=cast(AsyncEngine, engine),
        sessions=cast(async_sessionmaker[AsyncSession], _unavailable_sessions),
        redis=cast(Any, resolved_redis),
        api=cast(TelegramBotAPI, api),
        poller=cast(ControlBotPoller, resolved_poller),
        web_server=cast(Any, web),
        preview_maintenance=_PreviewMaintenanceFake(),
        account_id=UUID(int=1),
        admission=control_module._AdmissionGate(bootstrap_maintenance),
        instance_id=uuid7(),
        started_at=NOW,
    )
    return components, resolved_redis, web, resolved_poller, api, engine


def _application(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bootstrap_maintenance: bool = False,
    redis: _RedisFake | None = None,
    poller: _PollerFake | None = None,
) -> tuple[
    ProductionControlApplication, _RedisFake, _WebServerFake, _PollerFake, _ApiFake, _EngineFake
]:
    monkeypatch.setattr(control_module, "RuntimeMarkerObserver", _RuntimeMarkerObserverFake)
    components, resolved_redis, web, resolved_poller, api, engine = _components(
        bootstrap_maintenance=bootstrap_maintenance,
        redis=redis,
        poller=poller,
    )
    return (
        ProductionControlApplication(components),
        resolved_redis,
        web,
        resolved_poller,
        api,
        engine,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_bootstrap_maintenance_starts_web_but_not_bot_poller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, redis, web, poller, api, engine = _application(
        monkeypatch,
        bootstrap_maintenance=True,
    )
    process = _ProcessFake()
    task = asyncio.create_task(application.serve(cast(ManagedProcess, process)))
    await asyncio.wait_for(web.started_event.wait(), timeout=1)
    process.request_drain()
    await task

    assert redis.connect_calls == [False]
    assert poller.calls == 0
    assert application._components.admission.process is None
    assert api.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_serve_propagates_poller_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ControlProcessError("CONTROL_SYNTHETIC_CHILD_FAILURE")
    application, _redis, _web, poller, api, engine = _application(
        monkeypatch,
        poller=_PollerFake(error=failure),
    )

    with pytest.raises(ControlProcessError, match=r"^CONTROL_SYNTHETIC_CHILD_FAILURE$"):
        await application.serve(cast(ManagedProcess, _ProcessFake()))

    assert poller.calls == 1
    assert api.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_health_marks_database_unready_when_status_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, redis, web, _poller, _api, _engine = _application(
        monkeypatch,
        redis=_RedisFake(heartbeat_failure=True),
    )
    redis.started = True
    web.started = True
    application._serve_loop_running = True
    monkeypatch.setattr(application, "_database_available", AsyncMock(return_value=True))
    monkeypatch.setattr(application, "_schema_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(application, "_restore_gate_open", AsyncMock(return_value=True))
    monkeypatch.setattr(application, "_redis_available", AsyncMock(return_value=True))
    persist_status = AsyncMock(return_value=False)
    monkeypatch.setattr(application, "_persist_status", persist_status)
    monkeypatch.setattr(control_module, "owner_required_surfaces_composed", lambda _owner: True)
    monkeypatch.setattr(control_module, "_disk_safe", lambda: True)

    state = await application.health(UtcTimestamp(NOW))

    assert not state.database_ok
    assert not state.schema_ok
    assert not state.restore_gate_open
    assert not state.redis_ok
    assert state.web_api_ready
    persist_status.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_completion_cursor_coalesces_duplicates_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _redis, _web, _poller, _api, _engine = _application(monkeypatch)
    first = UUID(int=1)
    second = UUID(int=2)
    application._command_completed(first)
    application._command_completed(first)
    application._command_completed(second)

    generation, command_id = await application.wait_for_command_completion(after_generation=1)
    assert generation == 2
    assert command_id == second
    assert application.command_completion_generation == 2

    with pytest.raises(ValueError, match="control completion generation is invalid"):
        await application.wait_for_command_completion(after_generation=3)

    application._stop.set()
    with pytest.raises(ControlProcessError, match=r"^CONTROL_COMPLETION_WAIT_STOPPED$"):
        await application.wait_for_command_completion(after_generation=2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_close_is_idempotent_after_a_redis_heartbeat_clear_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, redis, _web, _poller, api, engine = _application(monkeypatch)
    redis.started = True
    record_stopped = AsyncMock()
    monkeypatch.setattr(application, "_record_stopped", record_stopped)

    async def reject_clear(_service: ServiceName) -> None:
        raise RedisRuntimeError("synthetic heartbeat clear failure")

    monkeypatch.setattr(redis, "clear_heartbeat", reject_clear)

    await application._close()
    await application._close()

    record_stopped.assert_awaited_once()
    assert redis.close_calls == 1
    assert api.close_calls == 1
    assert engine.dispose_calls == 1
    assert application._components.admission.process is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_maintenance_uses_control_factory_backend() -> None:
    calls: list[datetime] = []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Backend:
        async def delete_due(self, *, now: datetime) -> int:
            calls.append(now)
            return 3

    class Factory:
        def context_backend(self, session: Session) -> Backend:
            assert isinstance(session, Session)
            return Backend()

    maintenance = DurablePreviewDeletionMaintenance(
        cast(async_sessionmaker[AsyncSession], Session),
        cast(Any, Factory()),
    )

    assert await maintenance.run_once(now=NOW) == 3
    assert calls == [NOW]


@pytest.mark.unit
def test_control_helpers_validate_admission_secrets_endpoints_and_disk_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = control_module._AdmissionGate(maintenance=False)
    process = _ProcessFake()
    gate.process = cast(ManagedProcess, process)
    assert gate()
    assert control_module._process_accepts_new_work(cast(ManagedProcess, process))
    process.request_drain()
    assert not gate()

    bundle = SecretBundle(
        (
            ("good", SensitiveValue(b"value")),
            ("bad", SensitiveValue(b"bad\x00value")),
        )
    )
    assert control_module._text_secret(bundle, "good").reveal_for_use() == "value"
    for secret_id in ("bad", "missing"):
        with pytest.raises(ControlProcessError, match=r"^CONTROL_SECRET_INVALID$"):
            control_module._text_secret(bundle, secret_id)

    password_secret_id = "good"  # noqa: S105 - synthetic secret bundle lookup key
    settings = cast(
        ProductionSettings,
        SimpleNamespace(
            database=SimpleNamespace(
                host="postgres",
                port=5432,
                database="telegram_userbot",
                login_role="control_login",
                runtime_role="control_runtime",
                password_secret_id=password_secret_id,
                sslmode="require",
            ),
            redis=None,
        ),
    )
    assert (
        control_module._database_settings(settings, bundle).application_name
        == "telegram_userbot_control"
    )
    with pytest.raises(ControlProcessError, match=r"^CONTROL_REDIS_SETTINGS_MISSING$"):
        control_module._redis_settings(settings, bundle)

    def no_disk_usage(_path: object) -> object:
        raise OSError("synthetic disk unavailable")

    monkeypatch.setattr(shutil, "disk_usage", no_disk_usage)
    assert not control_module._disk_safe()


@pytest.mark.unit
def test_control_build_rejects_wrong_process_and_invalid_build_clock() -> None:
    wrong_process_settings = cast(
        ProductionSettings, SimpleNamespace(process=ProductionProcess.WORKER)
    )
    with pytest.raises(ControlProcessError, match=r"^CONTROL_PROCESS_SETTINGS_INVALID$"):
        build_control_application(settings=wrong_process_settings, secrets=SecretBundle(()))

    invalid_clock_settings = cast(
        ProductionSettings, SimpleNamespace(process=ProductionProcess.CONTROL)
    )
    with pytest.raises(ControlProcessError, match=r"^CONTROL_PROCESS_CLOCK_INVALID$"):
        build_control_application(
            settings=invalid_clock_settings,
            secrets=SecretBundle(()),
            now=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC).replace(tzinfo=None),
        )
