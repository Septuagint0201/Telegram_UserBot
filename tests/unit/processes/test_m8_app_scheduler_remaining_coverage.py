"""Fake-first coverage for the app scheduler lifecycle and query seams."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import telegram_userbot.processes.app_scheduler as scheduler_module
from telegram_userbot.platform.config.production import ProductionProcess
from telegram_userbot.platform.runtime import TerminationDeadline
from telegram_userbot.processes.app import AppSchedulerContext
from telegram_userbot.processes.app_scheduler import (
    APP_CONTROL_BATCH,
    APP_MEDIA_CLEANUP_INTERVAL_SECONDS,
    APP_MEDIA_RECOVERY_INTERVAL_SECONDS,
    APP_RECOVERY_INTERVAL_SECONDS,
    ProductionAppScheduler,
    build_production_app_scheduler,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
ACCOUNT_ID = UUID(int=1)


class _Result:
    def __init__(self, *, rows: tuple[object, ...] = (), scalar: object = None) -> None:
        self.rows = rows
        self.scalar_value = scalar

    def mappings(self) -> Self:
        return self

    def all(self) -> list[object]:
        return list(self.rows)

    def one_or_none(self) -> object | None:
        if len(self.rows) > 1:
            raise AssertionError("synthetic result contains multiple rows")
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class _Sessions:
    def __init__(
        self, *, scalars: tuple[object, ...] = (), results: tuple[_Result, ...] = ()
    ) -> None:
        self.scalars = deque(scalars)
        self.results = deque(results)
        self.calls = 0

    async def __aenter__(self) -> _Sessions:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Sessions:
        return self

    def __call__(self) -> _Sessions:
        return self

    async def execute(self, _statement: object) -> _Result:
        self.calls += 1
        return self.results.popleft() if self.results else _Result()

    async def scalar(self, _statement: object) -> object:
        self.calls += 1
        return self.scalars.popleft() if self.scalars else None


def _scheduler(*, poll_seconds: float = 0.05) -> ProductionAppScheduler:
    return ProductionAppScheduler(
        account_id=ACCOUNT_ID,
        allowed_admin_ids=frozenset({1000}),
        bot_identity="SyntheticControlBot",
        now=lambda: NOW,
        poll_seconds=poll_seconds,
    )


def _context(*, sessions: object | None = None, admission: object | None = None) -> Any:
    return cast(
        AppSchedulerContext,
        SimpleNamespace(
            sessions=sessions or _Sessions(),
            model=object(),
            telegram=object(),
            redis=object(),
            owner_instance_id=UUID(int=2),
            operation_admission=admission or AsyncMock(return_value=True),
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": UUID(int=0)},
        {"allowed_admin_ids": frozenset()},
        {"bot_identity": ""},
        {"poll_seconds": 0.01},
        {"poll_seconds": 60.1},
    ],
)
def test_scheduler_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "allowed_admin_ids": frozenset({1000}),
        "bot_identity": "SyntheticControlBot",
        "poll_seconds": 0.5,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match="configuration"):
        ProductionAppScheduler(**cast(Any, values))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_run_requires_both_bound_callbacks_and_clears_running_state() -> None:
    scheduler = _scheduler()
    with pytest.raises(RuntimeError, match="pending-image"):
        await scheduler.run(_context())

    scheduler.bind_pending_image_recovery(AsyncMock(return_value=0))
    with pytest.raises(RuntimeError, match="media cleanup"):
        await scheduler.run(_context())

    scheduler.bind_media_cleanup(AsyncMock(return_value=0))
    seen: list[object] = []

    class _Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

    async def one_cycle(_context_value: object, _runtime: object) -> None:
        seen.append(_context_value)
        scheduler._stop.set()

    object.__setattr__(scheduler, "_run_cycle", one_cycle)
    module = cast(Any, scheduler_module)
    original = module.ConversationRuntimeService
    module.ConversationRuntimeService = _Runtime
    try:
        context = _context()
        await scheduler.run(context)
    finally:
        module.ConversationRuntimeService = original
    assert seen == [context]
    assert not scheduler._running
    assert scheduler._stopped.is_set()
    assert not scheduler.ready()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_drain_sets_stop_when_idle_and_wait_for_work_handles_wake_and_timeout() -> (
    None
):
    scheduler = _scheduler(poll_seconds=0.05)
    deadline = cast(TerminationDeadline, object())
    await scheduler.drain(deadline)
    assert not scheduler._running
    assert scheduler._stop.is_set()

    scheduler._stop.clear()
    scheduler.wake()
    await scheduler._wait_for_work()
    assert not scheduler._wake.is_set()

    await scheduler._wait_for_work()
    assert not scheduler._wake.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_run_cycle_covers_cleanup_missing_and_dispatch_paths() -> None:
    scheduler = _scheduler()
    scheduler._pending_image_recovery = AsyncMock(return_value=0)
    scheduler._media_cleanup = AsyncMock(return_value=1)
    scheduler._process_control_commands = AsyncMock()  # type: ignore[method-assign]
    scheduler._next_due_turn = AsyncMock(return_value=UUID(int=3))  # type: ignore[method-assign]
    scheduler._next_delivery_group = AsyncMock(return_value=UUID(int=4))  # type: ignore[method-assign]
    runtime = SimpleNamespace(
        recover_once=AsyncMock(),
        run_due_turn=AsyncMock(),
        dispatch_group=AsyncMock(),
    )
    context = _context()
    await scheduler._run_cycle(context, cast(Any, runtime))
    runtime.run_due_turn.assert_awaited_once()
    runtime.dispatch_group.assert_not_awaited()

    scheduler._last_recovery_at = NOW + timedelta(seconds=APP_RECOVERY_INTERVAL_SECONDS)
    scheduler._last_media_recovery_at = NOW + timedelta(seconds=APP_MEDIA_RECOVERY_INTERVAL_SECONDS)
    scheduler._last_media_cleanup_at = NOW + timedelta(seconds=APP_MEDIA_CLEANUP_INTERVAL_SECONDS)
    scheduler._last_media_cleanup_attempt_at = NOW + timedelta(
        seconds=APP_MEDIA_CLEANUP_INTERVAL_SECONDS
    )
    scheduler._next_due_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await scheduler._run_cycle(context, cast(Any, runtime))
    runtime.dispatch_group.assert_awaited_once()

    scheduler._pending_image_recovery = None
    scheduler._last_media_recovery_at = None
    with pytest.raises(RuntimeError, match="recovery disappeared"):
        await scheduler._run_cycle(context, cast(Any, runtime))

    scheduler._pending_image_recovery = AsyncMock(return_value=0)
    scheduler._media_cleanup = None
    scheduler._last_media_recovery_at = NOW + timedelta(seconds=APP_MEDIA_RECOVERY_INTERVAL_SECONDS)
    scheduler._last_media_cleanup_at = None
    scheduler._last_media_cleanup_attempt_at = None
    with pytest.raises(RuntimeError, match="cleanup disappeared"):
        await scheduler._run_cycle(context, cast(Any, runtime))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_control_command_batch_stops_at_empty_and_at_batch_limit() -> None:
    scheduler = _scheduler()
    context = _context(sessions=_Sessions())
    processor = SimpleNamespace(process_next=AsyncMock(side_effect=[object(), None]))
    module = cast(Any, scheduler_module)
    original = module.ConversationControlCommandProcessor
    module.ConversationControlCommandProcessor = lambda **_: processor
    try:
        await scheduler._process_control_commands(context, NOW)
    finally:
        module.ConversationControlCommandProcessor = original
    assert processor.process_next.await_count == 2

    processor.process_next.reset_mock()
    processor.process_next.side_effect = [object()] * (APP_CONTROL_BATCH + 1)
    module.ConversationControlCommandProcessor = lambda **_: processor
    try:
        await scheduler._process_control_commands(context, NOW)
    finally:
        module.ConversationControlCommandProcessor = original
    assert processor.process_next.await_count == APP_CONTROL_BATCH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_turn_and_delivery_queries_return_scalar_ids() -> None:
    turn_id = UUID(int=9)
    group_id = UUID(int=10)
    sessions = _Sessions(scalars=(turn_id, group_id))
    context = _context(sessions=sessions)
    scheduler = _scheduler()
    assert await scheduler._next_due_turn(context, NOW) == turn_id
    assert await scheduler._next_delivery_group(context) == group_id
    assert sessions.calls == 2


@pytest.mark.unit
def test_scheduler_due_and_builder_process_guard() -> None:
    scheduler = _scheduler()
    assert scheduler._due(None, NOW, 1)
    assert scheduler._due(NOW - timedelta(seconds=1), NOW, 1)
    assert not scheduler._due(NOW, NOW, 1)

    settings = SimpleNamespace(
        process=ProductionProcess.APP,
        deployment=SimpleNamespace(
            runtime_identity=SimpleNamespace(
                account_id=ACCOUNT_ID,
                control_admin_user_ids=(1000,),
                control_bot_username="SyntheticControlBot",
            )
        ),
    )
    built = build_production_app_scheduler(cast(Any, settings))
    assert built._account_id == ACCOUNT_ID
    settings.process = ProductionProcess.WORKER
    with pytest.raises(ValueError, match="app scheduler"):
        build_production_app_scheduler(cast(Any, settings))
