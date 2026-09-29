from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import telegram_userbot.processes.worker as worker_module
from telegram_userbot.adapters.queue.redis import RedisRuntime
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.processes.worker import (
    DurableCompensationPublisher,
    ProductionWorkerApplication,
)

_GIB = 1024**3


class _Outbox:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(self) -> None:
        self._events.append("outbox-stop")


class _Scheduler:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def stop(self) -> None:
        self._events.append("scheduler-stop")


class _Worker:
    def __init__(self, events: list[str]) -> None:
        self.allow_pick_jobs = True
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._events = events

    async def close(self) -> None:
        self._events.append("arq-close")


class _Redis:
    def __init__(self, events: list[str]) -> None:
        self.started = True
        self._events = events

    async def clear_heartbeat(self, _service: object) -> None:
        self._events.append("heartbeat-clear")

    async def close(self) -> None:
        self.started = False
        self._events.append("redis-close")


class _Engine:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("engine-dispose")


class _ManagedProcess:
    def __init__(self) -> None:
        self.draining = False

    def request_drain(self) -> bool:
        self.draining = True
        return True


class _SessionContext:
    async def __aenter__(self) -> _SessionContext:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _SessionContext:
        return self


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_settles_all_background_tasks_before_dependency_disposal() -> None:
    events: list[str] = []
    blocked = asyncio.Event()

    async def child(name: str) -> None:
        try:
            await blocked.wait()
        finally:
            events.append(f"{name}-settled")

    tasks = tuple(
        asyncio.create_task(child(name), name=f"synthetic-{name}")
        for name in ("arq", "outbox", "scheduler")
    )
    await asyncio.sleep(0)
    worker = _Worker(events)
    runtime = cast(Any, object.__new__(ProductionWorkerApplication))
    runtime._outbox = _Outbox(events)
    runtime._scheduler = _Scheduler(events)
    runtime._arq_worker = worker
    runtime._background_tasks = tasks
    runtime._stop_lock = asyncio.Lock()
    runtime._closed = False
    runtime._components = SimpleNamespace(
        redis=cast(RedisRuntime, _Redis(events)),
        engine=_Engine(events),
    )
    runtime._record_stopped = AsyncMock(side_effect=lambda: events.append("status-stopped"))

    await runtime._request_stop(None)

    assert not worker.allow_pick_jobs
    assert all(task.done() for task in tasks)
    assert "engine-dispose" not in events

    await runtime._close_dependencies()

    settled = [events.index(f"{name}-settled") for name in ("arq", "outbox", "scheduler")]
    assert max(settled) < events.index("redis-close") < events.index("engine-dispose")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_pauses_work_without_requesting_restart_when_disk_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _ManagedProcess()
    runtime = cast(Any, object.__new__(ProductionWorkerApplication))
    runtime._components = SimpleNamespace(
        settings=SimpleNamespace(bootstrap_maintenance=False),
        redis=SimpleNamespace(started=False),
        registry=SimpleNamespace(job_types=("memory",)),
    )
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._managed_process = process
    runtime._serve_running = True
    runtime._consumer_running = True
    runtime._persist_status = AsyncMock()
    monkeypatch.setattr(
        "telegram_userbot.processes.worker._disk_admission",
        lambda: SimpleNamespace(
            operational=False,
            status_metadata_band="critical",
        ),
    )

    state = await runtime.health(UtcTimestamp(datetime(2030, 1, 1, tzinfo=UTC)))

    assert not process.draining
    assert not state.draining
    assert not state.disk_safety_ok
    assert state.required_config_ok


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admission", "allow_work", "allow_proactive"),
    [
        (
            disk_admission(total_bytes=100 * _GIB, available_bytes=10 * _GIB),
            True,
            False,
        ),
        (
            disk_admission(total_bytes=100 * _GIB, available_bytes=5 * _GIB),
            False,
            False,
        ),
    ],
)
async def test_worker_compensation_recovers_leases_but_respects_new_work_admission(
    monkeypatch: pytest.MonkeyPatch,
    admission: object,
    allow_work: bool,
    allow_proactive: bool,
) -> None:
    observed: dict[str, object] = {}

    class _Repository:
        def __init__(self, _session: object) -> None:
            return None

        async def recover_expired(self, *, now: datetime, limit: int) -> int:
            observed["recover"] = (now, limit)
            return 2

        async def rebuild_due_notifications(self, **kwargs: object) -> int:
            observed["rebuild"] = kwargs
            return 3 if kwargs["allow_work"] else 0

    monkeypatch.setattr(worker_module, "WorkerJobRepository", _Repository)
    session = _SessionContext()
    publisher = DurableCompensationPublisher(
        cast(Any, lambda: session),
        admission=lambda: cast(Any, admission),
    )

    result = await publisher.publish(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert observed["recover"] == (datetime(2030, 1, 1, tzinfo=UTC), 100)
    assert cast(dict[str, object], observed["rebuild"])["allow_work"] is allow_work
    assert cast(dict[str, object], observed["rebuild"])["allow_proactive"] is allow_proactive
    assert result == 2 + (3 if allow_work else 0)
