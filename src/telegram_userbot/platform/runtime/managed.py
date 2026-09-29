"""Reusable asynchronous process runner with fail-closed health and draining."""

import asyncio
import math
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, Protocol, TypeVar

from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.health import HealthState, ServiceName, write_health_snapshot
from telegram_userbot.platform.runtime.drain import DrainController, TerminationDeadline
from telegram_userbot.platform.runtime.watchdog import EventLoopWatchdog
from telegram_userbot.platform.time.system import SystemClock

HEALTH_HEARTBEAT_SECONDS = 10.0
DRAIN_DEADLINE_EXIT_CODE = 75
SERVICE_DRAIN_GRACE_SECONDS = {
    ServiceName.APP: 85.0,
    ServiceName.CONTROL: 25.0,
    ServiceName.WORKER: 85.0,
}

HealthProvider = Callable[[UtcTimestamp], Awaitable[HealthState]]
DrainHook = Callable[[TerminationDeadline], Awaitable[None]]
SnapshotWriter = Callable[[Path, HealthState], None]
ForceTermination = Callable[[int], NoReturn]
T = TypeVar("T")


class SignalRegistrar(Protocol):
    def install(self, callback: Callable[[signal.Signals], None]) -> Callable[[], None]: ...


class AsyncioSignalRegistrar:
    """Install loop-owned handlers only for the lifetime of a running process."""

    def install(self, callback: Callable[[signal.Signals], None]) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        try:
            for process_signal in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(process_signal, callback, process_signal)
                installed.append(process_signal)
        except BaseException:
            for process_signal in installed:
                loop.remove_signal_handler(process_signal)
            raise

        def remove() -> None:
            for process_signal in installed:
                loop.remove_signal_handler(process_signal)

        return remove


class ManagedProcess:
    """One-shot runner shared by app, control, and worker process roots."""

    def __init__(  # noqa: PLR0913 - dependencies are explicit runtime boundaries
        self,
        *,
        service: ServiceName,
        snapshot_path: Path,
        health_provider: HealthProvider,
        drain_hooks: Sequence[DrainHook] = (),
        signal_registrar: SignalRegistrar | None = None,
        snapshot_writer: SnapshotWriter = write_health_snapshot,
        utc_clock: Callable[[], UtcTimestamp] | None = None,
        monotonic_clock: Callable[[], MonotonicInstant] | None = None,
        heartbeat_seconds: float = HEALTH_HEARTBEAT_SECONDS,
        watchdog: EventLoopWatchdog | None = None,
        force_terminate: ForceTermination = os._exit,
    ) -> None:
        if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be finite and positive")
        system_clock = SystemClock()
        self._service = service
        self._snapshot_path = snapshot_path
        self._health_provider = health_provider
        self._drain_hooks = tuple(drain_hooks)
        self._signal_registrar = signal_registrar or AsyncioSignalRegistrar()
        self._snapshot_writer = snapshot_writer
        self._utc_clock = utc_clock or system_clock.now
        self._monotonic_clock = monotonic_clock or system_clock.monotonic_now
        self._heartbeat_seconds = heartbeat_seconds
        self._drain = DrainController(
            grace_seconds=SERVICE_DRAIN_GRACE_SECONDS[service],
            clock=self._monotonic_clock,
        )
        self._watchdog = watchdog or EventLoopWatchdog(clock=self._monotonic_clock)
        self._force_terminate = force_terminate
        self._stop_requested = asyncio.Event()
        self._running = False
        self._has_run = False
        self._force_termination_requested = False

    @property
    def service(self) -> ServiceName:
        return self._service

    @property
    def drain_grace_seconds(self) -> float:
        return SERVICE_DRAIN_GRACE_SECONDS[self._service]

    @property
    def draining(self) -> bool:
        return self._drain.draining

    @property
    def accepting_new_work(self) -> bool:
        return self._running and self._drain.accepting_new_work

    @property
    def termination_deadline(self) -> TerminationDeadline | None:
        """Return the one process-wide deadline once draining has begun."""

        return self._drain.deadline

    @property
    def force_termination_requested(self) -> bool:
        """Expose the terminal boundary to composition-root cleanup code."""

        return self._force_termination_requested

    def request_drain(self) -> bool:
        """Synchronously close the in-process admission gate on the first request."""

        if not self._running:
            return False
        first_request = not self._drain.draining
        self._drain.begin()
        self._watchdog.disable()
        self._stop_requested.set()
        return first_request

    async def wait_for_drain(self) -> None:
        """Wait until the admission gate has closed."""

        await self._stop_requested.wait()

    def _handle_signal(self, _process_signal: signal.Signals) -> None:
        self.request_drain()

    def _write_fail_closed(self, *, process_loop_ok: bool) -> None:
        observed_at = self._utc_clock()
        self._snapshot_writer(
            self._snapshot_path,
            HealthState.fail_closed(
                self._service,
                observed_at=observed_at,
                draining=self.draining,
                process_loop_ok=process_loop_ok,
            ),
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            observed_at = self._utc_clock()
            reported = await self._health_provider(observed_at)
            if reported.service is not self._service:
                raise ValueError("health provider service mismatch")
            snapshot = replace(
                reported,
                observed_at=observed_at,
                heartbeat_at=observed_at,
                draining=self.draining,
            )
            self._snapshot_writer(self._snapshot_path, snapshot)
            await asyncio.sleep(self._heartbeat_seconds)

    async def _watchdog_tick_loop(self) -> None:
        while True:
            self._watchdog.tick()
            await asyncio.sleep(1.0)

    async def _run_drain_hooks(self, deadline: TerminationDeadline) -> None:
        for hook in self._drain_hooks:
            await hook(deadline)

    def _drain_budget(self) -> tuple[TerminationDeadline, float]:
        deadline = self._drain.deadline
        remaining = self._drain.remaining_seconds()
        if deadline is None or remaining is None:
            raise RuntimeError("drain budget is unavailable")
        return deadline, remaining

    @staticmethod
    async def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task

    @staticmethod
    def _consume_detached_task(task: asyncio.Future[Any]) -> None:
        """Consume a task that outlives the process drain boundary.

        A cancellation-resistant dependency must never be awaited after the
        monotonic deadline.  It can nevertheless finish before an injected
        test terminator (or an event-loop teardown), so consume its terminal
        exception to avoid an unhandled-task warning.  The production
        terminator is normally ``os._exit`` and therefore never reaches this
        callback during a real deadline breach.
        """

        with suppress(BaseException):
            task.exception()

    @classmethod
    def _detach_task(cls, task: asyncio.Future[Any]) -> None:
        if task.done():
            cls._consume_detached_task(task)
        else:
            task.add_done_callback(cls._consume_detached_task)

    async def _settle_before_deadline(
        self,
        task: asyncio.Task[Any] | None,
        deadline: TerminationDeadline,
        *,
        cancel: bool,
    ) -> bool:
        """Settle a task without letting cancellation suppression extend drain."""

        if task is None or task.done():
            return True
        if cancel:
            task.cancel()
        remaining = deadline.remaining_seconds(self._monotonic_clock())
        if remaining <= 0:
            return task.done()
        timer = asyncio.create_task(asyncio.sleep(remaining), name="drain-deadline")
        done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            timer.cancel()
            with suppress(BaseException):
                await timer
            return True
        if not task.done():
            task.cancel()
        # Do not await a cancellation-resistant task: it may still use process
        # dependencies, so the only safe next step is the container-PID exit.
        self._detach_task(task)
        return False

    def _terminate_after_drain_deadline(self) -> NoReturn:
        """Exit the PID before application cleanup can release live dependencies."""

        # ``os._exit`` does not return.  The flag retains that same lifecycle
        # boundary for injected test terminators, which normally raise instead.
        self._force_termination_requested = True
        self._force_terminate(DRAIN_DEADLINE_EXIT_CODE)
        raise RuntimeError("force termination callback returned")

    def force_terminate_after_deadline(self) -> NoReturn:
        """Expose the single force-exit boundary to composition roots.

        Adapters may have their own deadline-aware operation (for example, a
        Telethon disconnect that detaches its underlying task).  Once such an
        adapter reports that the shared deadline was exceeded, the composition
        root must be able to select the same terminal path without reaching
        into a private implementation detail.
        """

        self._terminate_after_drain_deadline()

    async def _require_settled_before_deadline(
        self,
        task: asyncio.Task[Any] | None,
        deadline: TerminationDeadline,
        *,
        cancel: bool,
    ) -> None:
        if not await self._settle_before_deadline(task, deadline, cancel=cancel):
            self._terminate_after_drain_deadline()

    async def require_settled_before_deadline(
        self,
        task: asyncio.Task[Any] | None,
        deadline: TerminationDeadline,
        *,
        cancel: bool,
    ) -> None:
        """Apply the shared drain boundary to a composition-root child task.

        The private implementation predates the application roots; this small
        public seam lets those roots use the same cancellation-resistant-task
        policy without duplicating timeout or force-exit behavior.
        """

        await self._require_settled_before_deadline(task, deadline, cancel=cancel)

    async def await_before_deadline(
        self,
        awaitable: Awaitable[T],
        deadline: TerminationDeadline,
    ) -> T:
        """Run one cleanup awaitable inside the process-wide drain budget.

        Composition roots use this for dependency teardown that is not already
        represented by a long-lived child task.  A cancellation-resistant
        awaitable reaches the same force-termination boundary as drain hooks,
        so callers must not release another live dependency after this method
        returns through a timeout path.
        """

        task = asyncio.ensure_future(awaitable)
        await self._require_settled_before_deadline(task, deadline, cancel=False)
        return await task

    async def run(  # noqa: PLR0912, PLR0915 - lifecycle cleanup is deliberately explicit
        self, serve: Callable[[ManagedProcess], Coroutine[Any, Any, None]]
    ) -> None:
        """Run until completion or first signal, drain once, and exit."""

        if self._has_run:
            raise RuntimeError("managed process is one-shot")
        self._has_run = True
        self._running = True
        unregister_signals: Callable[[], None] | None = None
        serve_task: asyncio.Task[None] | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        watchdog_tick_task: asyncio.Task[None] | None = None
        stop_task: asyncio.Task[bool] | None = None
        primary_error: BaseException | None = None
        drain_deadline: TerminationDeadline | None = None
        try:
            self._write_fail_closed(process_loop_ok=True)
            unregister_signals = self._signal_registrar.install(self._handle_signal)
            self._watchdog.start()
            serve_task = asyncio.create_task(serve(self), name=f"{self._service.value}-serve")
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"{self._service.value}-health-heartbeat"
            )
            watchdog_tick_task = asyncio.create_task(
                self._watchdog_tick_loop(), name=f"{self._service.value}-watchdog-tick"
            )
            stop_task = asyncio.create_task(
                self._stop_requested.wait(), name=f"{self._service.value}-stop-request"
            )
            done, _pending = await asyncio.wait(
                (serve_task, heartbeat_task, watchdog_tick_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for candidate in (heartbeat_task, watchdog_tick_task, serve_task):
                if candidate in done and not candidate.cancelled():
                    candidate_error = candidate.exception()
                    if candidate_error is not None:
                        primary_error = candidate_error
                        break

            self.request_drain()
            self._write_fail_closed(process_loop_ok=primary_error is None)
            deadline, _ = self._drain_budget()
            drain_deadline = deadline
            await self._require_settled_before_deadline(
                heartbeat_task,
                deadline,
                cancel=True,
            )
            drain_task = asyncio.create_task(
                self._run_drain_hooks(deadline),
                name=f"{self._service.value}-drain-hooks",
            )
            try:
                await self._require_settled_before_deadline(drain_task, deadline, cancel=False)
                await drain_task
            except BaseException as error:
                if primary_error is None:
                    primary_error = error

            await self._require_settled_before_deadline(serve_task, deadline, cancel=False)

            if primary_error is None and serve_task.done() and not serve_task.cancelled():
                primary_error = serve_task.exception()
        except BaseException as error:
            if primary_error is None:
                primary_error = error
        finally:
            if not self._force_termination_requested:
                if drain_deadline is None:
                    await self._cancel(stop_task)
                    await self._cancel(heartbeat_task)
                    await self._cancel(watchdog_tick_task)
                    await self._cancel(serve_task)
                else:
                    await self._require_settled_before_deadline(
                        stop_task,
                        drain_deadline,
                        cancel=True,
                    )
                    await self._require_settled_before_deadline(
                        heartbeat_task,
                        drain_deadline,
                        cancel=True,
                    )
                    await self._require_settled_before_deadline(
                        watchdog_tick_task,
                        drain_deadline,
                        cancel=True,
                    )
                    await self._require_settled_before_deadline(
                        serve_task,
                        drain_deadline,
                        cancel=True,
                    )
                self._watchdog.stop()
                try:
                    if self.draining:
                        self._write_fail_closed(process_loop_ok=False)
                except BaseException as error:
                    if primary_error is None:
                        primary_error = error
                if unregister_signals is not None:
                    unregister_signals()
                self._running = False
        if primary_error is not None:
            raise primary_error
