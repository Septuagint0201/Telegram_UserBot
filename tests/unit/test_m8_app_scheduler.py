from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import telegram_userbot.processes.app_scheduler as scheduler_module
from telegram_userbot.platform.runtime import TerminationDeadline
from telegram_userbot.processes.app import AppSchedulerContext
from telegram_userbot.processes.app_scheduler import ProductionAppScheduler

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _scheduler(*, now: object | None = None) -> ProductionAppScheduler:
    return ProductionAppScheduler(
        account_id=UUID(int=1),
        allowed_admin_ids=frozenset({1000}),
        bot_identity="SyntheticControlBot",
        now=cast(Any, now or (lambda: NOW)),
    )


@pytest.mark.unit
def test_pending_media_recovery_binding_has_one_authoritative_owner() -> None:
    scheduler = _scheduler()

    async def recover() -> int:
        return 0

    scheduler.bind_pending_image_recovery(recover)
    scheduler.bind_media_cleanup(recover)
    with pytest.raises(RuntimeError, match="binding"):
        scheduler.bind_pending_image_recovery(recover)
    with pytest.raises(RuntimeError, match="binding"):
        scheduler.bind_media_cleanup(recover)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_runs_pending_media_recovery_once_per_interval() -> None:
    scheduler = _scheduler()
    recover = AsyncMock(return_value=1)
    cleanup = AsyncMock(return_value=1)
    scheduler.bind_pending_image_recovery(recover)
    scheduler.bind_media_cleanup(cleanup)
    scheduler._process_control_commands = AsyncMock()  # type: ignore[method-assign]
    scheduler._next_due_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduler._next_delivery_group = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runtime = SimpleNamespace(recover_once=AsyncMock())
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(operation_admission=AsyncMock(return_value=True)),
    )

    await scheduler._run_cycle(context, cast(Any, runtime))
    await scheduler._run_cycle(context, cast(Any, runtime))

    runtime.recover_once.assert_awaited_once()
    recover.assert_awaited_once()
    cleanup.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_retries_failed_media_cleanup_without_stopping_other_work() -> None:
    current = [NOW]
    scheduler = _scheduler(now=lambda: current[0])
    recover = AsyncMock(return_value=0)
    cleanup = AsyncMock(side_effect=(RuntimeError("synthetic cleanup failure"), 1, 1))
    scheduler.bind_pending_image_recovery(recover)
    scheduler.bind_media_cleanup(cleanup)
    scheduler._process_control_commands = AsyncMock()  # type: ignore[method-assign]
    scheduler._next_due_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduler._next_delivery_group = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runtime = SimpleNamespace(recover_once=AsyncMock())
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(operation_admission=AsyncMock(return_value=True)),
    )

    await scheduler._run_cycle(context, cast(Any, runtime))
    current[0] += timedelta(seconds=59)
    await scheduler._run_cycle(context, cast(Any, runtime))
    assert cleanup.await_count == 1

    current[0] += timedelta(seconds=1)
    await scheduler._run_cycle(context, cast(Any, runtime))
    assert cleanup.await_count == 2

    current[0] += timedelta(seconds=3599)
    await scheduler._run_cycle(context, cast(Any, runtime))
    assert cleanup.await_count == 2

    current[0] += timedelta(seconds=1)
    await scheduler._run_cycle(context, cast(Any, runtime))
    assert cleanup.await_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_hard_disk_gate_keeps_recovery_but_skips_new_model_and_send() -> None:
    scheduler = _scheduler()
    scheduler.bind_pending_image_recovery(AsyncMock(return_value=0))
    scheduler.bind_media_cleanup(AsyncMock(return_value=0))
    scheduler._process_control_commands = AsyncMock()  # type: ignore[method-assign]
    scheduler._next_due_turn = AsyncMock(return_value=UUID(int=2))  # type: ignore[method-assign]
    scheduler._next_delivery_group = AsyncMock(return_value=UUID(int=3))  # type: ignore[method-assign]
    runtime = SimpleNamespace(
        recover_once=AsyncMock(),
        run_due_turn=AsyncMock(),
        dispatch_group=AsyncMock(),
    )
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(operation_admission=AsyncMock(return_value=False)),
    )

    await scheduler._run_cycle(context, cast(Any, runtime))

    runtime.recover_once.assert_awaited_once()
    scheduler._next_due_turn.assert_not_awaited()
    scheduler._next_delivery_group.assert_not_awaited()
    runtime.run_due_turn.assert_not_awaited()
    runtime.dispatch_group.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("work_kind", ["generation", "delivery", "media"])
async def test_control_commands_run_while_slow_work_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, work_kind: str
) -> None:
    scheduler = _scheduler()
    started = asyncio.Event()
    release = asyncio.Event()
    command_pending = asyncio.Event()
    applied = asyncio.Event()
    mode = ["AUTO"]
    sends: list[str] = []

    async def slow_work(**_kwargs: object) -> int:
        started.set()
        await release.wait()
        if mode[0] == "AUTO":
            sends.append("stale-send")
        return 0

    async def controls(_context: object, _now: datetime) -> None:
        if command_pending.is_set():
            mode[0] = "HUMAN"
            applied.set()

    runtime = SimpleNamespace(
        recover_once=AsyncMock(),
        run_due_turn=slow_work,
        dispatch_group=slow_work,
    )
    monkeypatch.setattr(scheduler_module, "ConversationRuntimeService", lambda *_, **__: runtime)
    scheduler.bind_pending_image_recovery(
        slow_work if work_kind == "media" else AsyncMock(return_value=0)
    )
    scheduler.bind_media_cleanup(AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler, "_process_control_commands", controls)
    monkeypatch.setattr(
        scheduler,
        "_next_due_turn",
        AsyncMock(return_value=UUID(int=2) if work_kind == "generation" else None),
    )
    monkeypatch.setattr(
        scheduler,
        "_next_delivery_group",
        AsyncMock(return_value=UUID(int=3) if work_kind == "delivery" else None),
    )
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(
            sessions=object(),
            model=object(),
            telegram=object(),
            owner_instance_id=UUID(int=4),
            operation_admission=AsyncMock(return_value=True),
        ),
    )
    task = asyncio.create_task(scheduler.run(context))
    try:
        async with asyncio.timeout(2):
            await started.wait()
            command_pending.set()
            scheduler.wake()
            await applied.wait()
        assert not release.is_set()
        drain = asyncio.create_task(scheduler.drain(cast(TerminationDeadline, object())))
        await asyncio.sleep(0)
        release.set()
        async with asyncio.timeout(2):
            await drain
            await task
        assert not sends
        assert not scheduler.ready()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_failure_cancels_inflight_work_before_scheduler_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    started = asyncio.Event()
    work_settled = asyncio.Event()
    control_calls = 0

    async def controls(_context: object, _now: datetime) -> None:
        nonlocal control_calls
        control_calls += 1
        if control_calls > 1:
            await started.wait()
            raise RuntimeError("synthetic control failure")

    async def work(_context: object, _runtime: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            work_settled.set()

    scheduler.bind_pending_image_recovery(AsyncMock(return_value=0))
    scheduler.bind_media_cleanup(AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler, "_process_control_commands", controls)
    monkeypatch.setattr(scheduler, "_run_cycle", work)
    monkeypatch.setattr(scheduler_module, "ConversationRuntimeService", lambda *_, **__: object())
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(
            sessions=object(),
            model=object(),
            telegram=object(),
            operation_admission=AsyncMock(return_value=True),
        ),
    )
    scheduler.wake()
    async with asyncio.timeout(2):
        with pytest.raises(ExceptionGroup) as caught:
            await scheduler.run(context)
    assert isinstance(caught.value.exceptions[0], RuntimeError)
    assert str(caught.value.exceptions[0]) == "synthetic control failure"
    assert work_settled.is_set()
    assert scheduler._stopped.is_set()
    assert not scheduler.ready()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_cancellation_settles_inflight_control_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    control_started = asyncio.Event()
    control_settled = asyncio.Event()
    work_started = asyncio.Event()
    work_settled = asyncio.Event()
    control_calls = 0

    async def controls(_context: object, _now: datetime) -> None:
        nonlocal control_calls
        control_calls += 1
        if control_calls == 1:
            return
        control_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            control_settled.set()

    async def work(_context: object, _runtime: object) -> None:
        work_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            work_settled.set()

    scheduler.bind_pending_image_recovery(AsyncMock(return_value=0))
    scheduler.bind_media_cleanup(AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler, "_process_control_commands", controls)
    monkeypatch.setattr(scheduler, "_run_cycle", work)
    monkeypatch.setattr(scheduler_module, "ConversationRuntimeService", lambda *_, **__: object())
    context = cast(
        AppSchedulerContext,
        SimpleNamespace(
            sessions=object(),
            model=object(),
            telegram=object(),
            operation_admission=AsyncMock(return_value=True),
        ),
    )
    scheduler.wake()
    task = asyncio.create_task(scheduler.run(context))
    try:
        async with asyncio.timeout(2):
            await control_started.wait()
            await work_started.wait()
        task.cancel()
        async with asyncio.timeout(2):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert control_settled.is_set()
        assert work_settled.is_set()
        assert scheduler._stopped.is_set()
        assert not scheduler.ready()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
