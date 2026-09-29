"""Dedicated PostgreSQL session ownership for singleton runtime resources."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from telegram_userbot.adapters.persistence.engine import PostgresConnectionSettings

_DEPLOYMENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_MAX_TELEGRAM_ACCOUNT_ID = (1 << 63) - 1
_LOCK_NAMESPACE = "telegram-userbot:ownership:v1"
_LOCK_HELD_SQL = text(
    """
    SELECT EXISTS (
      SELECT 1
      FROM pg_locks
      WHERE locktype = 'advisory'
        AND pid = pg_backend_pid()
        AND granted
        AND classid = (
          (CAST(:lock_key AS bigint) >> 32) & 4294967295
        )::oid
        AND objid = (
          CAST(:lock_key AS bigint) & 4294967295
        )::oid
        AND objsubid = 1
    )
    """
)


class OwnershipScope(StrEnum):
    """Closed scopes prevent two unrelated singleton resources sharing one lock."""

    TELEGRAM_SESSION = "telegram_session"
    WORKER_SCHEDULER = "worker_scheduler"


class SessionOwnershipError(RuntimeError):
    """A stable, content-free ownership failure safe for process logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class SessionOwnershipTarget:
    """Hashed lock identity that does not retain a Telegram account identifier."""

    scope: OwnershipScope
    lock_key: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, OwnershipScope) or type(self.lock_key) is not int:
            raise ValueError("ownership target is invalid")
        if not -(1 << 63) <= self.lock_key < (1 << 63):
            raise ValueError("ownership lock key is invalid")

    @classmethod
    def telegram_session(
        cls,
        *,
        deployment_id: str,
        telegram_account_id: int,
    ) -> Self:
        if type(telegram_account_id) is not int or not (
            0 < telegram_account_id <= _MAX_TELEGRAM_ACCOUNT_ID
        ):
            raise ValueError("Telegram account identity is invalid")
        return cls._from_subject(
            deployment_id=deployment_id,
            scope=OwnershipScope.TELEGRAM_SESSION,
            subject=str(telegram_account_id),
        )

    @classmethod
    def worker_scheduler(cls, *, deployment_id: str) -> Self:
        return cls._from_subject(
            deployment_id=deployment_id,
            scope=OwnershipScope.WORKER_SCHEDULER,
            subject="singleton",
        )

    @classmethod
    def _from_subject(
        cls,
        *,
        deployment_id: str,
        scope: OwnershipScope,
        subject: str,
    ) -> Self:
        if not isinstance(deployment_id, str) or _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
            raise ValueError("deployment identity is invalid")
        material = f"{_LOCK_NAMESPACE}:{scope.value}:{deployment_id}:{subject}".encode()
        lock_key = int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)
        return cls(scope, lock_key)

    def __repr__(self) -> str:
        return f"SessionOwnershipTarget(scope={self.scope.value!r}, lock_key=<derived>)"


type OwnershipEngineFactory = Callable[[PostgresConnectionSettings], AsyncEngine]


def create_dedicated_ownership_engine(settings: PostgresConnectionSettings) -> AsyncEngine:
    """Create a no-pool engine whose sole connection owns one session lock.

    ``NullPool`` is part of the safety contract: closing the adapter closes the database
    session instead of returning a lock-bearing connection to an application pool.
    """

    return create_async_engine(
        settings.sqlalchemy_url(),
        echo=False,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=False,
        poolclass=NullPool,
    )


class PostgresSessionOwnership:
    """Hold one PostgreSQL session advisory lock on a dedicated connection."""

    def __init__(
        self,
        settings: PostgresConnectionSettings,
        target: SessionOwnershipTarget,
        *,
        engine_factory: OwnershipEngineFactory = create_dedicated_ownership_engine,
    ) -> None:
        self._settings = settings
        self._target = target
        self._engine_factory = engine_factory
        self._engine: AsyncEngine | None = None
        self._connection: AsyncConnection | None = None
        self._backend_pid: int | None = None

    @property
    def scope(self) -> OwnershipScope:
        return self._target.scope

    @property
    def acquired(self) -> bool:
        """Return only the last-known state; callers use :meth:`probe` for readiness."""

        return self._connection is not None and self._backend_pid is not None

    async def acquire(self) -> None:
        """Acquire once or fail closed without exposing database error details."""

        if self._connection is not None:
            if await self.probe():
                return
            raise SessionOwnershipError("SESSION_OWNERSHIP_LOST")

        engine: AsyncEngine | None = None
        connection: AsyncConnection | None = None
        try:
            engine = self._engine_factory(self._settings)
            connection = await engine.connect()
            if self._settings.runtime_role is not None:
                # The settings object has already constrained the role to a SQL identifier.
                await connection.execute(text(f'SET ROLE "{self._settings.runtime_role}"'))
            backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": self._target.lock_key},
            )
        except BaseException as error:
            await self._close_resources(connection, engine)
            if not isinstance(error, Exception):
                raise
            raise SessionOwnershipError("SESSION_OWNERSHIP_UNAVAILABLE") from None

        if type(backend_pid) is not int:
            await self._close_resources(connection, engine)
            raise SessionOwnershipError("SESSION_OWNERSHIP_IDENTITY_INVALID")
        if acquired is not True:
            await self._close_resources(connection, engine)
            raise SessionOwnershipError("SESSION_OWNERSHIP_CONTENDED")

        self._engine = engine
        self._connection = connection
        self._backend_pid = backend_pid

    async def probe(self) -> bool:
        """Confirm the same database session still holds the exact advisory lock."""

        connection = self._connection
        if connection is None or self._backend_pid is None:
            return False
        try:
            backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            lock_held = await connection.scalar(
                _LOCK_HELD_SQL,
                {"lock_key": self._target.lock_key},
            )
        except Exception:
            await self._drop_resources()
            return False
        if (
            type(backend_pid) is not int
            or backend_pid != self._backend_pid
            or lock_held is not True
        ):
            await self._drop_resources()
            return False
        return True

    async def release(self) -> None:
        """Release and close once; a repeated call is a no-op."""

        connection = self._connection
        if connection is None:
            await self._drop_resources()
            return
        failure_code: str | None = None
        try:
            released = await connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": self._target.lock_key},
            )
            if released is not True:
                failure_code = "SESSION_OWNERSHIP_LOST"
        except Exception:
            failure_code = "SESSION_OWNERSHIP_RELEASE_FAILED"
        close_failed = await self._drop_resources()
        if failure_code is not None:
            raise SessionOwnershipError(failure_code)
        if close_failed:
            raise SessionOwnershipError("SESSION_OWNERSHIP_CLOSE_FAILED")

    async def close(self) -> None:
        await self.release()

    async def _drop_resources(self) -> bool:
        connection = self._connection
        engine = self._engine
        self._connection = None
        self._engine = None
        self._backend_pid = None
        return await self._close_resources(connection, engine)

    @staticmethod
    async def _close_resources(
        connection: AsyncConnection | None,
        engine: AsyncEngine | None,
    ) -> bool:
        failed = False
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                failed = True
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                failed = True
        return failed

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            await self.close()
            return
        # A cleanup failure must not replace the exception raised by the managed body.
        with suppress(SessionOwnershipError):
            await self.close()


__all__ = [
    "OwnershipScope",
    "PostgresSessionOwnership",
    "SessionOwnershipError",
    "SessionOwnershipTarget",
    "create_dedicated_ownership_engine",
]
