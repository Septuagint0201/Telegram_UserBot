import asyncio
import math
import signal
from collections.abc import Callable
from pathlib import Path
from threading import Event as ThreadEvent

import pytest

from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.health import HealthState, ReadinessPolicy, ServiceName
from telegram_userbot.platform.runtime import (
    EVENT_LOOP_STALL_SECONDS,
    EVENT_LOOP_WATCHDOG_EXIT_CODE,
    HEALTH_HEARTBEAT_SECONDS,
    SERVICE_DRAIN_GRACE_SECONDS,
    DrainController,
    EventLoopWatchdog,
    ManagedProcess,
    TerminationDeadline,
)
from telegram_userbot.platform.runtime.managed import AsyncioSignalRegistrar
from tests.unit.platform.test_m8_health import NOW, health_state


class FakeMonotonicClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> MonotonicInstant:
        return MonotonicInstant(self.value)


class FakeSignalRegistrar:
    def __init__(self) -> None:
        self.callback: Callable[[signal.Signals], None] | None = None
        self.installed = False
        self.remove_count = 0

    def install(self, callback: Callable[[signal.Signals], None]) -> Callable[[], None]:
        if self.installed:
            raise RuntimeError("fake signal registrar already installed")
        self.callback = callback
        self.installed = True

        def remove() -> None:
            self.installed = False
            self.remove_count += 1

        return remove

    def trigger(self, process_signal: signal.Signals) -> None:
        if self.callback is None or not self.installed:
            raise RuntimeError("fake signal registrar is not installed")
        self.callback(process_signal)


class RecordingSnapshotWriter:
    def __init__(self) -> None:
        self.states: list[HealthState] = []

    def __call__(self, _path: Path, state: HealthState) -> None:
        self.states.append(state)


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.unit
def test_drain_is_one_way_idempotent_and_preserves_first_deadline() -> None:
    clock = FakeMonotonicClock(10.0)
    controller = DrainController(grace_seconds=30.0, clock=clock)
    initial = (controller.draining, controller.accepting_new_work, controller.remaining_seconds())
    assert initial == (False, True, None)

    first = controller.begin()
    clock.value = 20.0
    second = controller.begin()
    assert first is second
    assert controller.draining
    assert not controller.accepting_new_work
    assert controller.deadline is first
    assert controller.remaining_seconds() == 20.0
    assert not controller.deadline_reached()

    clock.value = 40.0
    assert controller.remaining_seconds() == 0.0
    assert controller.deadline_reached()


@pytest.mark.unit
def test_termination_deadline_uses_monotonic_time() -> None:
    deadline = TerminationDeadline(started_at=MonotonicInstant(5.0), grace_seconds=2.5)
    assert deadline.started_at == MonotonicInstant(5.0)
    assert deadline.expires_at == MonotonicInstant(7.5)
    assert deadline.remaining_seconds(MonotonicInstant(6.0)) == 1.5
    assert deadline.expired(MonotonicInstant(7.5))


@pytest.mark.unit
@pytest.mark.parametrize("grace", [0.0, -1.0, math.inf, math.nan])
def test_drain_rejects_invalid_grace(grace: float) -> None:
    with pytest.raises(ValueError, match="grace"):
        DrainController(grace_seconds=grace)
    with pytest.raises(ValueError, match="grace"):
        TerminationDeadline(started_at=MonotonicInstant(0.0), grace_seconds=grace)


@pytest.mark.unit
def test_import_and_controller_do_not_register_a_global_sigterm_handler() -> None:
    # Portable side-effect evidence only; this does not claim Linux signal-delivery behavior.
    before = signal.getsignal(signal.SIGTERM)
    DrainController(grace_seconds=1.0)
    after = signal.getsignal(signal.SIGTERM)
    assert after is before


@pytest.mark.unit
def test_managed_runtime_defaults_are_fixed_by_service() -> None:
    assert HEALTH_HEARTBEAT_SECONDS == 10.0
    assert SERVICE_DRAIN_GRACE_SECONDS == {
        ServiceName.APP: 85.0,
        ServiceName.CONTROL: 25.0,
        ServiceName.WORKER: 85.0,
    }


@pytest.mark.unit
async def test_managed_process_starts_fail_closed_and_drains_on_first_signal() -> None:
    registrar = FakeSignalRegistrar()
    writer = RecordingSnapshotWriter()
    monotonic = FakeMonotonicClock(10.0)
    exit_codes: list[int] = []
    watchdog = EventLoopWatchdog(
        clock=monotonic,
        exit_callback=exit_codes.append,
        poll_seconds=0.01,
    )
    hook_deadlines: list[TerminationDeadline] = []

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(
            ServiceName.CONTROL,
            observed_at=observed_at,
            heartbeat_at=observed_at,
        )

    async def hook(deadline: TerminationDeadline) -> None:
        hook_deadlines.append(deadline)

    async def serve(process: ManagedProcess) -> None:
        await process.wait_for_drain()

    process = ManagedProcess(
        service=ServiceName.CONTROL,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        drain_hooks=(hook,),
        signal_registrar=registrar,
        snapshot_writer=writer,
        utc_clock=lambda: NOW,
        monotonic_clock=monotonic,
        heartbeat_seconds=0.01,
        watchdog=watchdog,
    )
    run_task = asyncio.create_task(process.run(serve))
    await wait_until(lambda: registrar.installed and len(writer.states) >= 2)

    initial = writer.states[0]
    assert initial.service is ServiceName.CONTROL
    assert initial.process_loop_ok
    assert not initial.draining
    assert not initial.required_config_ok
    assert not initial.disk_safety_ok
    assert initial.control_bot_ready is False
    assert initial.web_api_ready is False
    accepting_before = process.accepting_new_work
    assert accepting_before

    registrar.trigger(signal.SIGTERM)
    assert process.draining
    accepting_after = process.accepting_new_work
    assert not accepting_after
    registrar.trigger(signal.SIGINT)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert len(hook_deadlines) == 1
    assert hook_deadlines[0].expires_at.value - hook_deadlines[0].started_at.value == 25.0
    assert writer.states[-1].draining
    assert not writer.states[-1].process_loop_ok
    assert registrar.remove_count == 1
    assert not watchdog.running
    assert exit_codes == []


@pytest.mark.unit
async def test_managed_process_rejects_provider_service_mismatch_and_cleans_up() -> None:
    registrar = FakeSignalRegistrar()
    writer = RecordingSnapshotWriter()
    monotonic = FakeMonotonicClock(1.0)
    watchdog = EventLoopWatchdog(
        clock=monotonic,
        exit_callback=lambda _code: None,
        poll_seconds=0.01,
    )

    async def wrong_provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(
            ServiceName.APP,
            observed_at=observed_at,
            heartbeat_at=observed_at,
        )

    async def serve(process: ManagedProcess) -> None:
        await process.wait_for_drain()

    process = ManagedProcess(
        service=ServiceName.WORKER,
        snapshot_path=Path("ignored-health.json"),
        health_provider=wrong_provider,
        signal_registrar=registrar,
        snapshot_writer=writer,
        utc_clock=lambda: NOW,
        monotonic_clock=monotonic,
        watchdog=watchdog,
    )
    with pytest.raises(ValueError, match="service mismatch"):
        await asyncio.wait_for(process.run(serve), timeout=1.0)
    assert process.draining
    assert not process.accepting_new_work
    assert registrar.remove_count == 1
    assert not watchdog.running


@pytest.mark.unit
def test_event_loop_watchdog_exits_only_after_strict_stall_boundary() -> None:
    clock = FakeMonotonicClock(0.0)
    exited = ThreadEvent()
    exit_codes: list[int] = []

    def record_exit(code: int) -> None:
        exit_codes.append(code)
        exited.set()

    watchdog = EventLoopWatchdog(
        clock=clock,
        exit_callback=record_exit,
        stall_seconds=EVENT_LOOP_STALL_SECONDS,
        poll_seconds=0.005,
    )
    assert not watchdog.running
    watchdog.start()
    clock.value = EVENT_LOOP_STALL_SECONDS
    assert not exited.wait(0.02)
    clock.value += 0.001
    assert exited.wait(0.2)
    watchdog.stop()
    assert exit_codes == [EVENT_LOOP_WATCHDOG_EXIT_CODE]


@pytest.mark.unit
def test_watchdog_ticks_ignore_readiness_and_disable_before_drain() -> None:
    clock = FakeMonotonicClock(0.0)
    exited = ThreadEvent()
    watchdog = EventLoopWatchdog(
        clock=clock,
        exit_callback=lambda _code: exited.set(),
        stall_seconds=0.02,
        poll_seconds=0.005,
    )
    not_ready = health_state(database_ok=False, redis_ok=False)
    assert not ReadinessPolicy().readiness(not_ready, now=NOW).healthy
    watchdog.start()
    for value in (0.01, 0.02, 0.03, 0.04):
        clock.value = value
        watchdog.tick()
    watchdog.disable()
    clock.value = 100.0
    assert not exited.wait(0.04)
    watchdog.stop()


@pytest.mark.unit
@pytest.mark.parametrize("heartbeat", [0.0, -1.0, math.inf, math.nan])
def test_managed_process_rejects_invalid_heartbeat_interval(heartbeat: float) -> None:
    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(observed_at=observed_at, heartbeat_at=observed_at)

    with pytest.raises(ValueError, match="heartbeat"):
        ManagedProcess(
            service=ServiceName.APP,
            snapshot_path=Path("ignored-health.json"),
            health_provider=provider,
            heartbeat_seconds=heartbeat,
        )


@pytest.mark.unit
async def test_managed_process_propagates_drain_hook_failure_after_closing_admission() -> None:
    registrar = FakeSignalRegistrar()
    writer = RecordingSnapshotWriter()
    monotonic = FakeMonotonicClock(10.0)
    watchdog = EventLoopWatchdog(
        clock=monotonic,
        exit_callback=lambda _code: None,
        poll_seconds=0.01,
    )

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(observed_at=observed_at, heartbeat_at=observed_at)

    async def failing_hook(deadline: TerminationDeadline) -> None:
        assert deadline.expires_at.value == 95.0
        raise RuntimeError("synthetic drain failure")

    async def serve(process: ManagedProcess) -> None:
        await process.wait_for_drain()

    process = ManagedProcess(
        service=ServiceName.APP,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        drain_hooks=(failing_hook,),
        signal_registrar=registrar,
        snapshot_writer=writer,
        utc_clock=lambda: NOW,
        monotonic_clock=monotonic,
        heartbeat_seconds=0.01,
        watchdog=watchdog,
    )
    run_task = asyncio.create_task(process.run(serve))
    await wait_until(lambda: registrar.installed and len(writer.states) >= 2)
    registrar.trigger(signal.SIGTERM)

    with pytest.raises(RuntimeError, match="synthetic drain failure"):
        await asyncio.wait_for(run_task, timeout=1.0)
    assert process.draining
    assert not process.accepting_new_work
    assert writer.states[-1].process_loop_ok is False
    assert registrar.remove_count == 1
    assert not watchdog.running


@pytest.mark.unit
async def test_primary_serve_failure_is_not_replaced_by_secondary_drain_failure() -> None:
    registrar = FakeSignalRegistrar()
    writer = RecordingSnapshotWriter()

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(observed_at=observed_at, heartbeat_at=observed_at)

    async def failing_hook(_deadline: TerminationDeadline) -> None:
        raise RuntimeError("secondary drain failure")

    async def failing_serve(_process: ManagedProcess) -> None:
        raise LookupError("primary serve failure")

    process = ManagedProcess(
        service=ServiceName.APP,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        drain_hooks=(failing_hook,),
        signal_registrar=registrar,
        snapshot_writer=writer,
        utc_clock=lambda: NOW,
        monotonic_clock=FakeMonotonicClock(10.0),
    )

    with pytest.raises(LookupError, match="primary serve failure"):
        await asyncio.wait_for(process.run(failing_serve), timeout=1.0)
    assert registrar.remove_count == 1
    assert writer.states[-1].process_loop_ok is False


@pytest.mark.unit
async def test_managed_process_is_one_shot_and_rejects_drain_while_stopped() -> None:
    registrar = FakeSignalRegistrar()
    writer = RecordingSnapshotWriter()

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(observed_at=observed_at, heartbeat_at=observed_at)

    async def serve(_process: ManagedProcess) -> None:
        return

    process = ManagedProcess(
        service=ServiceName.APP,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        signal_registrar=registrar,
        snapshot_writer=writer,
        utc_clock=lambda: NOW,
    )
    assert process.request_drain() is False
    await asyncio.wait_for(process.run(serve), timeout=1.0)
    assert process.request_drain() is False
    with pytest.raises(RuntimeError, match="one-shot"):
        await process.run(serve)


@pytest.mark.unit
async def test_initial_snapshot_failure_aborts_before_signal_or_watchdog_install() -> None:
    registrar = FakeSignalRegistrar()
    watchdog = EventLoopWatchdog(exit_callback=lambda _code: None)

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(observed_at=observed_at, heartbeat_at=observed_at)

    async def serve(_process: ManagedProcess) -> None:
        raise AssertionError("serve must not start")

    def failing_writer(_path: Path, _state: HealthState) -> None:
        raise OSError("synthetic snapshot failure")

    process = ManagedProcess(
        service=ServiceName.CONTROL,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        signal_registrar=registrar,
        snapshot_writer=failing_writer,
        utc_clock=lambda: NOW,
        watchdog=watchdog,
    )

    with pytest.raises(OSError, match="synthetic snapshot failure"):
        await process.run(serve)
    assert registrar.installed is False
    assert not watchdog.running
    assert process.request_drain() is False


@pytest.mark.unit
def test_signal_registrar_removes_partial_installation_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingLoop:
        def __init__(self) -> None:
            self.added: list[signal.Signals] = []
            self.removed: list[signal.Signals] = []

        def add_signal_handler(
            self,
            process_signal: signal.Signals,
            callback: Callable[[signal.Signals], None],
            argument: signal.Signals,
        ) -> None:
            del callback, argument
            if process_signal is signal.SIGINT:
                raise OSError("synthetic unsupported signal")
            self.added.append(process_signal)

        def remove_signal_handler(self, process_signal: signal.Signals) -> None:
            self.removed.append(process_signal)

    loop = FailingLoop()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)

    with pytest.raises(OSError, match="unsupported signal"):
        AsyncioSignalRegistrar().install(lambda _process_signal: None)
    assert loop.added == [signal.SIGTERM]
    assert loop.removed == [signal.SIGTERM]
