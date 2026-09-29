from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from telegram_userbot.adapters.persistence.scheduler_leader import SchedulerLeaderLock


class _Connection:
    def __init__(self, *scalars: object, error: BaseException | None = None) -> None:
        self.scalars = list(scalars)
        self.error = error
        self.calls: list[tuple[object, dict[str, object] | None]] = []
        self.closed = False

    async def scalar(
        self, statement: object, parameters: dict[str, object] | None = None
    ) -> object:
        self.calls.append((statement, parameters))
        if self.error is not None:
            raise self.error
        return self.scalars.pop(0) if self.scalars else None

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.connect_count = 0

    async def connect(self) -> _Connection:
        self.connect_count += 1
        return self.connection


@pytest.mark.unit
def test_leader_rejects_empty_or_oversized_deployment_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        SchedulerLeaderLock(cast(AsyncEngine, _Engine(_Connection())), deployment_id="")
    with pytest.raises(ValueError, match="identity"):
        SchedulerLeaderLock(cast(AsyncEngine, _Engine(_Connection())), deployment_id="x" * 64)


@pytest.mark.unit
async def test_try_acquire_keeps_connection_only_when_lock_is_owned() -> None:
    rejected_connection = _Connection(False)
    rejected_engine = _Engine(rejected_connection)
    rejected = SchedulerLeaderLock(cast(AsyncEngine, rejected_engine), deployment_id="primary")
    assert await rejected.try_acquire() is False
    assert not rejected.acquired
    assert rejected_connection.closed
    assert rejected_engine.connect_count == 1

    accepted_connection = _Connection(True)
    accepted = SchedulerLeaderLock(
        cast(AsyncEngine, _Engine(accepted_connection)), deployment_id="primary"
    )
    assert await accepted.try_acquire() is True
    assert accepted.acquired
    assert await accepted.try_acquire() is True
    assert accepted_connection.closed is False
    assert len(accepted_connection.calls) == 1
    await accepted.release()
    assert accepted_connection.closed


@pytest.mark.unit
async def test_try_acquire_closes_connection_on_driver_error() -> None:
    connection = _Connection(error=OSError("synthetic"))
    lock = SchedulerLeaderLock(cast(AsyncEngine, _Engine(connection)), deployment_id="primary")
    with pytest.raises(OSError, match="synthetic"):
        await lock.try_acquire()
    assert connection.closed
    assert not lock.acquired


@pytest.mark.unit
async def test_probe_reports_health_and_releases_broken_connection() -> None:
    idle = SchedulerLeaderLock(cast(AsyncEngine, _Engine(_Connection())), deployment_id="primary")
    assert await idle.probe() is False
    await idle.release()

    connection = _Connection(True, 1)
    lock = SchedulerLeaderLock(cast(AsyncEngine, _Engine(connection)), deployment_id="primary")
    assert await lock.try_acquire()
    assert await lock.probe() is True
    assert await lock.probe() is False
    assert lock.acquired
    await lock.release()

    broken = _Connection(True)
    broken_lock = SchedulerLeaderLock(cast(AsyncEngine, _Engine(broken)), deployment_id="primary")
    assert await broken_lock.try_acquire()
    original_scalar = broken.scalar

    async def fail_probe(statement: object, parameters: dict[str, object] | None = None) -> Any:
        if "SELECT 1" in str(statement):
            raise OSError("lost")
        return await original_scalar(statement, parameters)

    broken.scalar = fail_probe  # type: ignore[method-assign]
    assert await broken_lock.probe() is False
    assert not broken_lock.acquired
    assert broken.closed


@pytest.mark.unit
async def test_release_swallows_unlock_failure_and_always_closes() -> None:
    connection = _Connection(True)
    lock = SchedulerLeaderLock(cast(AsyncEngine, _Engine(connection)), deployment_id="primary")
    assert await lock.try_acquire()

    original_scalar = connection.scalar

    async def fail_unlock(statement: object, parameters: dict[str, object] | None = None) -> Any:
        if "pg_advisory_unlock" in str(statement):
            raise OSError("unlock failed")
        return await original_scalar(statement, parameters)

    connection.scalar = fail_unlock  # type: ignore[method-assign]
    await lock.release()
    assert not lock.acquired
    assert connection.closed
