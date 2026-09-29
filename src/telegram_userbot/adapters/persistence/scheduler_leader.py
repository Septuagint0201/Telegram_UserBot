"""Dedicated PostgreSQL session advisory lock for scheduler leadership."""

from __future__ import annotations

from contextlib import suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class SchedulerLeaderLock:
    """Own one physical connection for the lifetime of scheduler leadership."""

    def __init__(self, engine: AsyncEngine, *, deployment_id: str) -> None:
        if not deployment_id or len(deployment_id) > 63:
            raise ValueError("scheduler deployment identity is invalid")
        self._engine = engine
        self._lock_key = f"telegram-userbot:scheduler:v1:{deployment_id}"
        self._connection: AsyncConnection | None = None

    @property
    def acquired(self) -> bool:
        return self._connection is not None

    async def try_acquire(self) -> bool:
        if self._connection is not None:
            return True
        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": self._lock_key},
            )
        except BaseException:
            await connection.close()
            raise
        if acquired is not True:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def probe(self) -> bool:
        connection = self._connection
        if connection is None:
            return False
        try:
            result = await connection.scalar(text("SELECT 1"))
            return bool(result == 1)
        except Exception:
            # A broken connection releases the PostgreSQL session lock.  Do not
            # continue publishing ticks based on stale in-process state.
            await self.release()
            return False

    async def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            with suppress(Exception):
                await connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": self._lock_key},
                )
        finally:
            await connection.close()


__all__ = ["SchedulerLeaderLock"]
