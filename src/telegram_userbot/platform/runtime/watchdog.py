"""Threaded event-loop stall watchdog with no import-time side effects."""

import math
import os
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread

from telegram_userbot.domain.shared.time import MonotonicInstant
from telegram_userbot.platform.time.system import SystemClock

EVENT_LOOP_STALL_SECONDS = 30.0
EVENT_LOOP_WATCHDOG_EXIT_CODE = 70


class EventLoopWatchdog:
    """Exit only when loop ticks stop; dependency readiness is intentionally absent."""

    def __init__(
        self,
        *,
        clock: Callable[[], MonotonicInstant] | None = None,
        exit_callback: Callable[[int], None] = os._exit,
        stall_seconds: float = EVENT_LOOP_STALL_SECONDS,
        poll_seconds: float = 1.0,
    ) -> None:
        if (
            not math.isfinite(stall_seconds)
            or stall_seconds <= 0
            or not math.isfinite(poll_seconds)
            or poll_seconds <= 0
        ):
            raise ValueError("watchdog intervals must be finite and positive")
        self._clock = clock or SystemClock().monotonic_now
        self._exit_callback = exit_callback
        self._stall_seconds = stall_seconds
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._last_tick: MonotonicInstant | None = None
        self._enabled = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("event-loop watchdog is one-shot")
            self._last_tick = self._clock()
            self._enabled = True
            self._thread = Thread(
                target=self._monitor,
                name="telegram-userbot-event-loop-watchdog",
                daemon=True,
            )
            self._thread.start()

    def tick(self) -> None:
        with self._lock:
            if self._enabled:
                self._last_tick = self._clock()

    def disable(self) -> None:
        """Disable before drain work so an intentional quiet loop never exits hard."""

        with self._lock:
            self._enabled = False
        self._stop.set()

    def stop(self) -> None:
        self.disable()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=self._poll_seconds * 2)

    def _monitor(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            with self._lock:
                last_tick = self._last_tick
                stalled = (
                    self._enabled
                    and last_tick is not None
                    and self._clock().value - last_tick.value > self._stall_seconds
                )
                if stalled:
                    self._enabled = False
            if stalled:
                self._exit_callback(EVENT_LOOP_WATCHDOG_EXIT_CODE)
                return
