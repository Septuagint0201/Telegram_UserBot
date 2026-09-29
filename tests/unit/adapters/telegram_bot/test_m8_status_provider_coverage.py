from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.adapters.telegram_bot.status_provider as status_module
from telegram_userbot.adapters.queue.redis import ServiceHeartbeat as RedisHeartbeat
from telegram_userbot.adapters.queue.redis import ServiceHeartbeatStatus
from telegram_userbot.adapters.telegram_bot.dispatcher import PublicServiceState
from telegram_userbot.adapters.telegram_bot.status_provider import (
    ContentFreeServerStatusProvider,
    PostgresAvailabilityProbe,
    PostgresServiceProjectionSource,
)
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.health.status import (
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
INSTANCE_ID = UUID("01900000-0000-7000-8000-000000000231")


def _heartbeat(
    service: ServiceName,
    *,
    at: datetime = NOW,
    readiness: ServiceReadiness = ServiceReadiness.READY,
    code: ServiceStatusCode = ServiceStatusCode.READY,
) -> ServiceHeartbeat:
    return ServiceHeartbeat(
        instance_id=INSTANCE_ID,
        service_name=service,
        started_at=at - timedelta(minutes=1),
        heartbeat_at=at,
        readiness=readiness,
        status_code=code,
        metadata=ServiceStatusMetadata(deployment_id="prod-primary"),
    )


class _Projection:
    def __init__(self, values: dict[ServiceName, ServiceHeartbeat | None]) -> None:
        self.values = values
        self.raise_error = False

    async def latest(self, service: ServiceName) -> ServiceHeartbeat | None:
        if self.raise_error:
            raise RuntimeError("projection unavailable")
        return self.values.get(service)


class _Redis:
    def __init__(self, status: ServiceHeartbeatStatus = ServiceHeartbeatStatus.ALIVE) -> None:
        self.status = status
        self.probe_result = True
        self.raise_probe = False
        self.raise_read = False

    async def probe(self) -> bool:
        if self.raise_probe:
            raise RuntimeError("redis unavailable")
        return self.probe_result

    async def read_heartbeat(self, service: ServiceName) -> RedisHeartbeat:
        if self.raise_read:
            raise RuntimeError("heartbeat unavailable")
        return RedisHeartbeat(service, self.status, 10)


class _Database:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.raise_error = False

    async def probe(self) -> bool:
        if self.raise_error:
            raise RuntimeError("database unavailable")
        return self.result


def _provider(
    projection: _Projection | None = None,
    redis: _Redis | None = None,
    database: _Database | None = None,
) -> ContentFreeServerStatusProvider:
    return ContentFreeServerStatusProvider(
        projections=projection
        or _Projection({service: _heartbeat(service) for service in ServiceName}),
        redis=cast(Any, redis or _Redis()),
        database=cast(Any, database or _Database()),
        now=lambda: NOW,
        fresh_for=timedelta(seconds=35),
        stale_after=timedelta(seconds=90),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_combines_fresh_projection_and_alive_redis_states() -> None:
    provider = _provider()
    snapshot = await provider.snapshot()
    assert snapshot.app is PublicServiceState.HEALTHY
    assert snapshot.control is PublicServiceState.HEALTHY
    assert snapshot.worker is PublicServiceState.HEALTHY

    projection = _Projection(
        {
            ServiceName.APP: _heartbeat(
                ServiceName.APP,
                at=NOW - timedelta(seconds=60),
            ),
            ServiceName.CONTROL: None,
            ServiceName.WORKER: _heartbeat(
                ServiceName.WORKER,
                readiness=ServiceReadiness.DEGRADED,
                code=ServiceStatusCode.DATABASE_UNAVAILABLE,
            ),
        }
    )
    redis = _Redis(ServiceHeartbeatStatus.UNKNOWN)
    database = _Database()
    snapshot = await _provider(projection, redis, database).snapshot()
    assert snapshot.app is PublicServiceState.DEGRADED
    assert snapshot.control is PublicServiceState.UNKNOWN
    assert snapshot.worker is PublicServiceState.DEGRADED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_fails_closed_when_dependency_probes_or_reads_fail() -> None:
    projection = _Projection({service: _heartbeat(service) for service in ServiceName})
    projection.raise_error = True
    redis = _Redis()
    redis.raise_probe = True
    database = _Database()
    database.raise_error = True
    snapshot = await _provider(projection, redis, database).snapshot()
    assert snapshot.app is PublicServiceState.UNKNOWN
    assert snapshot.control is PublicServiceState.UNKNOWN
    assert snapshot.worker is PublicServiceState.UNKNOWN

    redis = _Redis()
    redis.raise_read = True
    snapshot = await _provider(
        _Projection({service: _heartbeat(service) for service in ServiceName}), redis
    ).snapshot()
    assert snapshot.app is PublicServiceState.DEGRADED


@pytest.mark.unit
def test_projection_and_combination_rules_cover_all_public_state_bands() -> None:
    provider = _provider()
    assert provider._projection_state(None, now=NOW) is PublicServiceState.UNKNOWN
    assert (
        provider._projection_state(
            _heartbeat(ServiceName.APP, at=NOW + timedelta(seconds=1)), now=NOW
        )
        is PublicServiceState.DOWN
    )
    assert (
        provider._projection_state(
            _heartbeat(ServiceName.APP, at=NOW - timedelta(seconds=100)), now=NOW
        )
        is PublicServiceState.DOWN
    )
    assert (
        provider._projection_state(
            _heartbeat(
                ServiceName.APP,
                readiness=ServiceReadiness.NOT_READY,
                code=ServiceStatusCode.DATABASE_UNAVAILABLE,
            ),
            now=NOW,
        )
        is PublicServiceState.DOWN
    )
    assert (
        provider._projection_state(
            _heartbeat(
                ServiceName.APP,
                readiness=ServiceReadiness.DRAINING,
                code=ServiceStatusCode.DRAINING,
            ),
            now=NOW,
        )
        is PublicServiceState.DEGRADED
    )
    assert (
        provider._projection_state(
            _heartbeat(ServiceName.APP, at=NOW - timedelta(seconds=40)), now=NOW
        )
        is PublicServiceState.DEGRADED
    )

    combine = status_module._combine_status
    assert combine(PublicServiceState.DOWN, PublicServiceState.HEALTHY) is PublicServiceState.DOWN
    assert (
        combine(PublicServiceState.HEALTHY, PublicServiceState.HEALTHY)
        is PublicServiceState.HEALTHY
    )
    assert (
        combine(PublicServiceState.UNKNOWN, PublicServiceState.UNKNOWN)
        is PublicServiceState.UNKNOWN
    )
    assert (
        combine(PublicServiceState.HEALTHY, PublicServiceState.UNKNOWN)
        is PublicServiceState.DEGRADED
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_wrappers_use_context_managers_and_return_false_on_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Sessions:
        def __call__(self) -> _SessionContext:
            return _SessionContext()

    class _ProjectionRepository:
        def __init__(self, _session: object, _service: ServiceName) -> None:
            pass

        async def latest(self) -> ServiceHeartbeat:
            return _heartbeat(ServiceName.APP)

    monkeypatch.setattr(status_module, "ServiceStatusRepository", _ProjectionRepository)
    source = PostgresServiceProjectionSource(cast(async_sessionmaker[AsyncSession], _Sessions()))
    assert await source.latest(ServiceName.APP) == _heartbeat(ServiceName.APP)

    class _Connection:
        def __init__(self, value: object = 1, error: bool = False) -> None:
            self.value = value
            self.error = error

        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _statement: object) -> object:
            if self.error:
                raise RuntimeError("database probe failed")
            return self.value

    class _Engine:
        def __init__(self, connection: _Connection) -> None:
            self.connection = connection

        def connect(self) -> _Connection:
            return self.connection

    assert await PostgresAvailabilityProbe(cast(AsyncEngine, _Engine(_Connection()))).probe()
    assert not await PostgresAvailabilityProbe(
        cast(AsyncEngine, _Engine(_Connection(value=0)))
    ).probe()
    assert not await PostgresAvailabilityProbe(
        cast(AsyncEngine, _Engine(_Connection(error=True)))
    ).probe()


@pytest.mark.unit
def test_provider_rejects_invalid_freshness_configuration() -> None:
    with pytest.raises(ValueError, match="freshness"):
        ContentFreeServerStatusProvider(
            projections=cast(Any, _Projection({})),
            redis=cast(Any, _Redis()),
            database=cast(Any, _Database()),
            now=lambda: NOW,
            fresh_for=timedelta(0),
        )
