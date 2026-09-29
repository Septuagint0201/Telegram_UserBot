"""Fake-first coverage of production worker lifecycle boundaries."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.processes.worker as worker_module
from telegram_userbot.adapters.queue.redis import RedisRuntimeError
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.config.production import (
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.worker import (
    DurableOutboxPublisher,
    ProductionWorkerApplication,
    WorkerProcessError,
    WorkerSchedulerLeader,
)
from telegram_userbot.processes.worker_executors import JobExecutionError

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def _unused_session_factory() -> object:
    return object()


class _RedisFake:
    def __init__(self, *, publish_error: bool = False) -> None:
        self.started = False
        self.connect_calls: list[bool] = []
        self.close_calls = 0
        self.publish_error = publish_error
        self.notifier = object()

    async def connect(self, *, with_arq: bool) -> None:
        self.connect_calls.append(with_arq)
        self.started = True

    def durable_job_notifier(self, *, queue_name: str) -> object:
        assert queue_name == worker_module.ARQ_QUEUE_NAME
        return self.notifier

    async def close(self) -> None:
        self.close_calls += 1
        self.started = False

    async def clear_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.WORKER

    async def probe(self) -> bool:
        return True

    async def publish_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.WORKER
        if self.publish_error:
            raise RedisRuntimeError("synthetic heartbeat failure")


class _ArqFake:
    def __init__(self, *, exits_immediately: bool = False) -> None:
        self.allow_pick_jobs = True
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._exits_immediately = exits_immediately
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0

    async def async_run(self) -> None:
        self.started.set()
        if not self._exits_immediately:
            await self.release.wait()

    async def close(self) -> None:
        self.close_calls += 1
        self.release.set()


class _BlockingPublisher:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.stop_calls = 0

    async def run(self) -> None:
        await self.release.wait()

    def stop(self) -> None:
        self.stop_calls += 1
        self.release.set()


class _BlockingScheduler:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.stop_calls = 0

    async def run(self) -> None:
        await self.release.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.release.set()


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


def _worker_runtime(
    *,
    redis: _RedisFake | None = None,
    process: _ProcessFake | None = None,
) -> tuple[Any, _RedisFake]:
    resolved_redis = redis or _RedisFake()
    runtime = cast(Any, object.__new__(ProductionWorkerApplication))
    runtime._components = SimpleNamespace(
        settings=SimpleNamespace(
            bootstrap_maintenance=False,
            image_stage_concurrency=1,
            deployment=SimpleNamespace(
                timezone="UTC", deployment_id="synthetic-deployment", source_commit="a" * 40
            ),
        ),
        redis=resolved_redis,
        sessions=cast(async_sessionmaker[AsyncSession], _unused_session_factory),
        registry=SimpleNamespace(job_types=("memory.refresh",)),
        instance_id=uuid7(),
        engine=cast(AsyncEngine, object()),
        started_at=NOW,
    )
    runtime._managed_process = cast(ManagedProcess, process) if process is not None else None
    runtime._arq_worker = None
    runtime._outbox = None
    runtime._scheduler = None
    runtime._runtime_outbox = None
    runtime._serve_running = False
    runtime._consumer_running = False
    runtime._closed = False
    runtime._stop_lock = asyncio.Lock()
    runtime._background_tasks = ()
    runtime._monotonic_clock = lambda: MonotonicInstant(0.0)
    return runtime, resolved_redis


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_rejects_each_preflight_gate_before_resource_acquisition() -> None:
    cases = (
        (True, True, (True, True, True), "WORKER_MAINTENANCE_ACTIVE"),
        (False, False, (True, True, True), "WORKER_SCHEMA_NOT_READY"),
        (False, True, (False, True, True), "WORKER_DATABASE_UNAVAILABLE"),
        (False, True, (True, False, True), "WORKER_ACCOUNT_NOT_READY"),
        (False, True, (True, True, False), "WORKER_RESTORE_GATE_CLOSED"),
    )
    for maintenance, schema_ready, database_state, code in cases:
        runtime, redis = _worker_runtime(process=_ProcessFake())
        runtime._components.settings.bootstrap_maintenance = maintenance
        runtime._schema_ready = AsyncMock(return_value=schema_ready)
        runtime._database_account_restore = AsyncMock(return_value=database_state)

        with pytest.raises(WorkerProcessError, match=rf"^{code}$"):
            await runtime._start()

        assert redis.connect_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_constructs_all_runtime_children_after_validated_dependencies() -> None:
    process = _ProcessFake()
    runtime, redis = _worker_runtime(process=process)
    arq = _ArqFake()
    captured: list[object] = []
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))

    def build_worker(consumer: object) -> _ArqFake:
        captured.append(consumer)
        return arq

    runtime._worker_factory = build_worker

    await runtime._start()

    assert redis.connect_calls == [True]
    assert runtime._arq_worker is arq
    assert isinstance(runtime._outbox, DurableOutboxPublisher)
    assert isinstance(runtime._scheduler, WorkerSchedulerLeader)
    assert runtime._runtime_outbox is not None
    assert len(captured) == 1

    await runtime._rollback_start()
    assert redis.close_calls == 1
    assert runtime._arq_worker is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_rolls_back_redis_when_child_composition_fails() -> None:
    runtime, redis = _worker_runtime(process=_ProcessFake())
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))

    def reject_worker(_consumer: object) -> _ArqFake:
        raise RuntimeError("synthetic worker composition failure")

    runtime._worker_factory = reject_worker

    with pytest.raises(RuntimeError, match="synthetic worker composition failure"):
        await runtime._start()

    assert redis.connect_calls == [True]
    assert redis.close_calls == 1
    assert runtime._arq_worker is None
    assert runtime._outbox is None
    assert runtime._scheduler is None
    assert runtime._runtime_outbox is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_preserves_primary_error_when_rollback_close_fails() -> None:
    runtime, _redis = _worker_runtime(process=_ProcessFake())
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))

    class Redis:
        started = False
        close_calls = 0

        async def connect(self, *, with_arq: bool) -> None:
            assert with_arq
            self.started = True

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("synthetic rollback failure")

        def durable_job_notifier(self, *, queue_name: str) -> object:
            assert queue_name == worker_module.ARQ_QUEUE_NAME
            return object()

    redis = Redis()
    runtime._components.redis = redis

    def reject_worker(_consumer: object) -> _ArqFake:
        raise RuntimeError("synthetic startup failure")

    runtime._worker_factory = reject_worker

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        await runtime._start()

    assert redis.close_calls == 1
    assert runtime._arq_worker is None
    assert runtime._outbox is None
    assert runtime._scheduler is None
    assert runtime._runtime_outbox is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_serve_returns_on_drain_and_settles_started_children() -> None:
    process = _ProcessFake()
    runtime, _redis = _worker_runtime(process=process)
    arq = _ArqFake()
    outbox = _BlockingPublisher()
    scheduler = _BlockingScheduler()
    runtime_outbox = _BlockingPublisher()

    async def start() -> None:
        runtime._arq_worker = arq
        runtime._outbox = outbox
        runtime._scheduler = scheduler
        runtime._runtime_outbox = runtime_outbox

    async def stop(_deadline: object) -> None:
        outbox.stop()
        runtime_outbox.stop()
        await scheduler.stop()
        await arq.close()
        for task in runtime._background_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*runtime._background_tasks, return_exceptions=True)

    runtime._start = start
    runtime._request_stop = stop
    runtime._close_dependencies = AsyncMock()

    task = asyncio.create_task(runtime.serve(cast(ManagedProcess, process)))
    await asyncio.wait_for(arq.started.wait(), timeout=1)
    process.request_drain()
    await task

    assert not runtime._serve_running
    assert not runtime._consumer_running
    assert arq.close_calls == 1
    assert scheduler.stop_calls == 1
    runtime._close_dependencies.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_serve_reports_unexpected_clean_child_exit() -> None:
    process = _ProcessFake()
    runtime, _redis = _worker_runtime(process=process)
    arq = _ArqFake(exits_immediately=True)
    outbox = _BlockingPublisher()
    scheduler = _BlockingScheduler()
    runtime_outbox = _BlockingPublisher()

    async def start() -> None:
        runtime._arq_worker = arq
        runtime._outbox = outbox
        runtime._scheduler = scheduler
        runtime._runtime_outbox = runtime_outbox

    async def stop(_deadline: object) -> None:
        outbox.stop()
        runtime_outbox.stop()
        await scheduler.stop()
        await arq.close()
        for task in runtime._background_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*runtime._background_tasks, return_exceptions=True)

    runtime._start = start
    runtime._request_stop = stop
    runtime._close_dependencies = AsyncMock()

    with pytest.raises(WorkerProcessError, match=r"^WORKER_CHILD_EXITED$"):
        await runtime.serve(cast(ManagedProcess, process))

    runtime._close_dependencies.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_health_downgrades_redis_after_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, redis = _worker_runtime(redis=_RedisFake(publish_error=True), process=_ProcessFake())
    redis.started = True
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._persist_status = AsyncMock()
    runtime._serve_running = True
    runtime._consumer_running = True
    monkeypatch.setattr(worker_module, "worker_required_queues_composed", lambda: True)
    monkeypatch.setattr(
        worker_module,
        "_disk_admission",
        lambda: disk_admission(total_bytes=100 * 1024**3, available_bytes=20 * 1024**3),
    )

    state = await runtime.health(UtcTimestamp(NOW))

    assert state.database_ok
    assert not state.redis_ok
    assert state.consumer_ready
    runtime._persist_status.assert_awaited_once_with(state, "warning")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_retries_lock_acquisition_and_releases_on_stop() -> None:
    published = asyncio.Event()

    class Lock:
        acquired = False
        release_calls = 0

        async def try_acquire(self) -> bool:
            self.acquired = True
            return True

        async def probe(self) -> bool:
            return True

        async def release(self) -> None:
            self.acquired = False
            self.release_calls += 1

    class Publisher:
        async def publish(self, *, now: datetime) -> int:
            assert now == NOW
            published.set()
            return 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(
        lock=cast(Any, lock),
        publishers=(Publisher(),),
        now=lambda: NOW,
        tick_seconds=1.0,
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(published.wait(), timeout=1)
    await scheduler.stop()
    await task

    assert lock.release_calls == 1
    assert not scheduler.leader


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_outbox_retries_transient_publish_loop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = DurableOutboxPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _unused_session_factory),
        notifier=cast(Any, object()),
    )
    calls = 0

    async def publish_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic database transient")
        publisher.stop()
        return 0

    monkeypatch.setattr(cast(Any, publisher), "publish_once", publish_once)
    monkeypatch.setattr(worker_module, "OUTBOX_POLL_SECONDS", 0.01)

    await publisher.run()

    assert calls == 2


@pytest.mark.unit
def test_worker_helpers_fail_closed_for_invalid_secrets_uuid_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = SecretBundle(
        (
            ("text", SensitiveValue(b"value")),
            ("binary", SensitiveValue(b"x" * 32)),
            ("newline", SensitiveValue(b"bad\nvalue")),
        )
    )
    assert worker_module._text_secret(bundle, "text").reveal_for_use() == "value"
    assert worker_module._bytes_secret(bundle, "binary", minimum=32).reveal_for_use() == b"x" * 32
    assert worker_module._canonical_uuid(str(UUID(int=1))) == UUID(int=1)

    for secret_id in ("missing", "newline"):
        with pytest.raises(WorkerProcessError, match=r"^WORKER_SECRET_INVALID$"):
            worker_module._text_secret(bundle, secret_id)
    with pytest.raises(WorkerProcessError, match=r"^WORKER_SECRET_INVALID$"):
        worker_module._bytes_secret(bundle, "text", minimum=32)
    with pytest.raises(JobExecutionError, match=r"^WORKER_NOTIFICATION_INVALID$"):
        worker_module._canonical_uuid("not-a-uuid")

    def no_disk_usage(_path: object) -> object:
        raise OSError("synthetic disk unavailable")

    monkeypatch.setattr(shutil, "disk_usage", no_disk_usage)
    assert not worker_module._disk_admission().operational


@pytest.mark.unit
def test_worker_entrypoint_maps_configuration_runtime_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = StringIO()
    assert worker_module.run(["unexpected"], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "WORKER_ARGUMENT_INVALID\n"

    settings = cast(ProductionSettings, SimpleNamespace(load_secrets=lambda: SecretBundle(())))

    def load_settings(*_args: object) -> ProductionSettings:
        return settings

    monkeypatch.setattr(ProductionSettings, "load", load_settings)

    def reject_build(**_kwargs: object) -> ProductionWorkerApplication:
        raise WorkerProcessError("WORKER_SYNTHETIC_CONFIGURATION_INVALID")

    monkeypatch.setattr(worker_module, "build_worker_application", reject_build)
    stderr = StringIO()
    assert worker_module.run([], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "WORKER_CONFIGURATION_REJECTED\n"

    class Application:
        async def run_managed(self) -> None:
            raise RuntimeError("synthetic runtime failure")

    monkeypatch.setattr(worker_module, "build_worker_application", lambda **_: Application())
    stderr = StringIO()
    assert worker_module.run([], {}, stderr=stderr) == 1
    assert stderr.getvalue() == "WORKER_RUNTIME_FAILED\n"

    class SuccessfulApplication:
        async def run_managed(self) -> None:
            return None

    monkeypatch.setattr(
        worker_module,
        "build_worker_application",
        lambda **_: SuccessfulApplication(),
    )
    stderr = StringIO()
    assert worker_module.run([], {}, stderr=stderr) == 0
    assert stderr.getvalue() == ""


@pytest.mark.unit
def test_worker_application_rejects_non_worker_configuration() -> None:
    components = SimpleNamespace(settings=SimpleNamespace(process=ProductionProcess.CONTROL))

    with pytest.raises(WorkerProcessError, match=r"^WORKER_PROCESS_SETTINGS_INVALID$"):
        ProductionWorkerApplication(cast(Any, components))
