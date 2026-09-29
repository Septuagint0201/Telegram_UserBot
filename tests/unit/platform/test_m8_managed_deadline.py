"""Deadline behavior for cancellation-resistant managed-process children."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import pytest

from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.health import HealthState, ServiceName
from telegram_userbot.platform.runtime import ManagedProcess, TerminationDeadline
from telegram_userbot.platform.runtime.managed import DRAIN_DEADLINE_EXIT_CODE
from tests.unit.platform.test_m8_health import NOW, health_state


class _ForcedTerminationError(RuntimeError):
    """A test-only replacement for the non-returning ``os._exit`` boundary."""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_managed_process_forces_pid_exit_after_cancellation_resistant_task_deadline() -> None:
    release_child = asyncio.Event()
    cancellation_observed = asyncio.Event()
    exit_codes: list[int] = []

    async def provider(observed_at: UtcTimestamp) -> HealthState:
        return health_state(
            ServiceName.CONTROL,
            observed_at=observed_at,
            heartbeat_at=observed_at,
        )

    async def cancellation_resistant_child() -> None:
        while not release_child.is_set():
            try:
                await release_child.wait()
            except asyncio.CancelledError:
                cancellation_observed.set()

    def force_terminate(exit_code: int) -> NoReturn:
        exit_codes.append(exit_code)
        raise _ForcedTerminationError("synthetic pid exit")

    process = ManagedProcess(
        service=ServiceName.CONTROL,
        snapshot_path=Path("ignored-health.json"),
        health_provider=provider,
        utc_clock=lambda: NOW,
        monotonic_clock=lambda: MonotonicInstant(asyncio.get_running_loop().time()),
        force_terminate=force_terminate,
    )
    child = asyncio.create_task(cancellation_resistant_child())
    await asyncio.sleep(0)
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(asyncio.get_running_loop().time()),
        grace_seconds=0.01,
    )

    with pytest.raises(_ForcedTerminationError, match="synthetic pid exit"):
        await process._require_settled_before_deadline(child, deadline, cancel=True)

    assert cancellation_observed.is_set()
    assert not child.done()
    assert exit_codes == [DRAIN_DEADLINE_EXIT_CODE]
    assert process._force_termination_requested

    release_child.set()
    await child
