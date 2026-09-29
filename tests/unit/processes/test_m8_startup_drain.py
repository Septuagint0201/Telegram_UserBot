"""Startup admission fences for production process roots."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.queue.redis import RedisRuntime
from telegram_userbot.adapters.telegram_bot.http import TelegramBotAPI
from telegram_userbot.adapters.telegram_bot.polling import ControlBotPoller
from telegram_userbot.platform.config.production import ProductionSettings
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.app import ProductionAppError, ProductionAppRuntime
from telegram_userbot.processes.control import (
    ControlComponents,
    ControlWebServer,
    PreviewDeletionMaintenance,
    ProductionControlApplication,
)
from telegram_userbot.processes.worker import (
    ProductionWorkerApplication,
    WorkerProcessError,
)


class _ProcessDrainFake:
    def __init__(self) -> None:
        self.accepting_new_work = True
        self.draining = False
        self._drain = asyncio.Event()

    def request_drain(self) -> None:
        self.accepting_new_work = False
        self.draining = True
        self._drain.set()

    async def wait_for_drain(self) -> None:
        await self._drain.wait()


class _AppOwnershipFake:
    def __init__(self, process: _ProcessDrainFake) -> None:
        self.acquired = False
        self.released = False
        self._process = process

    async def acquire(self) -> None:
        self.acquired = True
        self._process.request_drain()

    async def release(self) -> None:
        self.released = True
        self.acquired = False


class _AppRedisFake:
    started = False

    def __init__(self) -> None:
        self.connect_calls = 0

    async def connect(self, *, with_arq: bool) -> None:
        assert with_arq
        self.connect_calls += 1
        self.started = True

    async def clear_heartbeat(self, _service: object) -> None:
        return None

    async def close(self) -> None:
        self.started = False


class _ObserverFake:
    def stop(self) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_start_releases_session_ownership_when_drain_begins_after_acquire() -> None:
    process = _ProcessDrainFake()
    ownership = _AppOwnershipFake(process)
    redis = _AppRedisFake()
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._started = False
    runtime._closed = False
    runtime._settings = SimpleNamespace(bootstrap_maintenance=False)
    runtime._managed_process = cast(ManagedProcess, process)
    runtime._require_operation_admission = AsyncMock()
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_and_restore_ready = AsyncMock(return_value=(True, True, True))
    runtime._ownership = ownership
    runtime._redis = redis
    runtime._telethon = None
    runtime._telegram_gateway = None
    runtime._media_ingestion = None
    runtime._runtime_marker_observer = _ObserverFake()

    with pytest.raises(ProductionAppError, match=r"^APP_DRAINING$"):
        await runtime.start()

    assert ownership.acquired is False
    assert ownership.released
    assert redis.connect_calls == 0
    assert runtime._telethon is None


class _WorkerRedisFake:
    def __init__(self, process: _ProcessDrainFake) -> None:
        self.started = False
        self.closed = False
        self._process = process

    async def connect(self, *, with_arq: bool) -> None:
        assert with_arq
        self.started = True
        self._process.request_drain()

    async def close(self) -> None:
        self.closed = True
        self.started = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_rolls_back_redis_when_drain_begins_during_connect() -> None:
    process = _ProcessDrainFake()
    redis = _WorkerRedisFake(process)
    runtime = cast(Any, object.__new__(ProductionWorkerApplication))
    runtime._components = SimpleNamespace(
        settings=SimpleNamespace(bootstrap_maintenance=False, image_stage_concurrency=1),
        redis=redis,
    )
    runtime._managed_process = cast(ManagedProcess, process)
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))
    runtime._arq_worker = None
    runtime._outbox = None
    runtime._scheduler = None
    runtime._runtime_outbox = None
    runtime._worker_factory = AsyncMock()

    with pytest.raises(WorkerProcessError, match=r"^WORKER_DRAINING$"):
        await runtime._start()

    assert redis.closed
    assert not redis.started
    runtime._worker_factory.assert_not_called()


class _ControlRedisFake:
    def __init__(self, process: _ProcessDrainFake, events: list[str]) -> None:
        self.started = False
        self._process = process
        self._events = events

    async def connect(self, *, with_arq: bool) -> None:
        assert not with_arq
        self.started = True
        self._events.append("redis-connected")
        self._process.request_drain()

    async def clear_heartbeat(self, _service: object) -> None:
        self._events.append("redis-heartbeat-cleared")

    async def close(self) -> None:
        self.started = False
        self._events.append("redis-closed")


class _WebServerFake:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False
        self.serve_calls = 0

    async def serve(self) -> None:
        self.serve_calls += 1
        self.started = True


class _ApiFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def aclose(self) -> None:
        self._events.append("api-closed")


class _EngineFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("engine-disposed")


class _PollerFake:
    identity_verified = False

    async def run(self, _stop: asyncio.Event) -> None:
        raise AssertionError("poller must not start after drain")


class _PreviewMaintenanceFake:
    async def run_once(self, *, now: datetime) -> int:
        del now
        raise AssertionError("maintenance must not start after drain")


def _unavailable_sessions() -> AsyncSession:
    raise RuntimeError("status persistence is unavailable in this unit test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_start_closes_dependencies_without_creating_tasks_after_redis_drain() -> None:
    process = _ProcessDrainFake()
    events: list[str] = []
    web_server = _WebServerFake()
    components = ControlComponents(
        settings=cast(
            ProductionSettings,
            SimpleNamespace(
                bootstrap_maintenance=False,
                deployment=SimpleNamespace(
                    deployment_id="synthetic-deployment",
                    source_commit="1" * 40,
                ),
            ),
        ),
        engine=cast(AsyncEngine, _EngineFake(events)),
        sessions=cast(async_sessionmaker[AsyncSession], _unavailable_sessions),
        redis=cast(RedisRuntime, _ControlRedisFake(process, events)),
        api=cast(TelegramBotAPI, _ApiFake(events)),
        poller=cast(ControlBotPoller, _PollerFake()),
        web_server=cast(ControlWebServer, web_server),
        preview_maintenance=cast(PreviewDeletionMaintenance, _PreviewMaintenanceFake()),
        account_id=UUID(int=1),
        admission=cast(Any, SimpleNamespace(process=None)),
        instance_id=uuid7(),
        started_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    application = ProductionControlApplication(components)

    await application.serve(cast(ManagedProcess, process))

    assert web_server.serve_calls == 0
    assert application._poll_task is None
    assert application._preview_maintenance_task is None
    assert application._runtime_marker_task is None
    assert events == [
        "redis-connected",
        "redis-heartbeat-cleared",
        "redis-closed",
        "api-closed",
        "engine-disposed",
    ]
    assert components.admission.process is None
