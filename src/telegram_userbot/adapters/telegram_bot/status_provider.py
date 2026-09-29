"""Content-free aggregation for the Control Bot ``/server_status`` command."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.service_status import ServiceStatusRepository
from telegram_userbot.adapters.queue.redis import (
    RedisRuntime,
    ServiceHeartbeatStatus,
)
from telegram_userbot.adapters.telegram_bot.dispatcher import (
    PublicServiceState,
    ServerStatusSnapshot,
)
from telegram_userbot.domain.shared.time import require_aware
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.health.status import ServiceHeartbeat, ServiceReadiness


class ServiceProjectionSource(Protocol):
    async def latest(self, service: ServiceName) -> ServiceHeartbeat | None: ...


class DatabaseAvailabilityProbe(Protocol):
    async def probe(self) -> bool: ...


class PostgresServiceProjectionSource:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def latest(self, service: ServiceName) -> ServiceHeartbeat | None:
        async with self._sessions() as session:
            return await ServiceStatusRepository(session, service).latest()


class PostgresAvailabilityProbe:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def probe(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                observed: object = await connection.scalar(text("SELECT 1"))
                return observed == 1
        except Exception:
            return False


class ContentFreeServerStatusProvider:
    """Combine independent Redis, PostgreSQL, and direct dependency observations."""

    def __init__(  # noqa: PLR0913 - independent probes and freshness bounds are explicit
        self,
        *,
        projections: ServiceProjectionSource,
        redis: RedisRuntime,
        database: DatabaseAvailabilityProbe,
        now: Callable[[], datetime],
        fresh_for: timedelta = timedelta(seconds=35),
        stale_after: timedelta = timedelta(seconds=90),
    ) -> None:
        if fresh_for <= timedelta(0) or stale_after <= fresh_for:
            raise ValueError("server status freshness settings are invalid")
        self._projections = projections
        self._redis = redis
        self._database = database
        self._now = now
        self._fresh_for = fresh_for
        self._stale_after = stale_after

    async def snapshot(self) -> ServerStatusSnapshot:
        now = require_aware(self._now(), "now")
        database_ok = await self._safe_database_probe()
        redis_ok = await self._safe_redis_probe()
        states = {
            service: await self._service_state(
                service,
                now=now,
                database_ok=database_ok,
                redis_ok=redis_ok,
            )
            for service in ServiceName
        }
        return ServerStatusSnapshot(
            app=states[ServiceName.APP],
            control=states[ServiceName.CONTROL],
            worker=states[ServiceName.WORKER],
        )

    async def _service_state(
        self,
        service: ServiceName,
        *,
        now: datetime,
        database_ok: bool,
        redis_ok: bool,
    ) -> PublicServiceState:
        projection_state = PublicServiceState.UNKNOWN
        if database_ok:
            try:
                projection = await self._projections.latest(service)
                projection_state = self._projection_state(projection, now=now)
            except Exception:
                projection_state = PublicServiceState.UNKNOWN
        redis_state = PublicServiceState.UNKNOWN
        if redis_ok:
            try:
                heartbeat = await self._redis.read_heartbeat(service)
                if heartbeat.status is ServiceHeartbeatStatus.ALIVE:
                    redis_state = PublicServiceState.HEALTHY
            except Exception:
                redis_state = PublicServiceState.UNKNOWN
        return _combine_status(projection_state, redis_state)

    def _projection_state(
        self, heartbeat: ServiceHeartbeat | None, *, now: datetime
    ) -> PublicServiceState:
        if heartbeat is None:
            return PublicServiceState.UNKNOWN
        age = now - heartbeat.heartbeat_at
        if age < timedelta(0) or age > self._stale_after:
            return PublicServiceState.DOWN
        if heartbeat.readiness in {ServiceReadiness.NOT_READY, ServiceReadiness.STOPPED}:
            return PublicServiceState.DOWN
        if age > self._fresh_for or heartbeat.readiness is not ServiceReadiness.READY:
            return PublicServiceState.DEGRADED
        return PublicServiceState.HEALTHY

    async def _safe_database_probe(self) -> bool:
        try:
            return await self._database.probe() is True
        except Exception:
            return False

    async def _safe_redis_probe(self) -> bool:
        try:
            return await self._redis.probe() is True
        except Exception:
            return False


def _combine_status(
    projection: PublicServiceState, redis: PublicServiceState
) -> PublicServiceState:
    if projection is PublicServiceState.DOWN:
        return PublicServiceState.DOWN
    if projection is PublicServiceState.HEALTHY and redis is PublicServiceState.HEALTHY:
        return PublicServiceState.HEALTHY
    if projection is PublicServiceState.UNKNOWN and redis is PublicServiceState.UNKNOWN:
        return PublicServiceState.UNKNOWN
    return PublicServiceState.DEGRADED


__all__ = [
    "ContentFreeServerStatusProvider",
    "DatabaseAvailabilityProbe",
    "PostgresAvailabilityProbe",
    "PostgresServiceProjectionSource",
    "ServiceProjectionSource",
]
