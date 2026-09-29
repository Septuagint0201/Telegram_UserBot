from __future__ import annotations

import asyncio
from typing import cast

import pytest
from arq.connections import RedisSettings

from telegram_userbot.adapters.queue.redis import (
    SERVICE_HEARTBEAT_TTL_SECONDS,
    ArqRuntimePool,
    RedisCommandClient,
    RedisConnectionSettings,
    RedisRuntime,
    RedisRuntimeError,
    ServiceHeartbeatStatus,
    create_redis_client,
    interpret_service_heartbeat,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.health import ServiceName

PASSWORD = "SYNTHETIC_REDIS_PASSWORD_0123456789"  # noqa: S105 - disposable test only


def redis_settings(**overrides: object) -> RedisConnectionSettings:
    values: dict[str, object] = {
        "host": "redis",
        "port": 6379,
        "database": 0,
        "password": SensitiveValue(PASSWORD),
        "tls": False,
        "connect_timeout_seconds": 2,
        "max_connections": 20,
    }
    values.update(overrides)
    return RedisConnectionSettings(**values)  # type: ignore[arg-type]


class FakeRedisClient:
    def __init__(self) -> None:
        self.ping_response: object = True
        self.get_response: object = None
        self.ttl_response: object = -2
        self.set_response: object = True
        self.delete_response: object = 0
        self.raise_on: str | None = None
        self.set_calls: list[tuple[str, bytes, int]] = []
        self.get_calls: list[str] = []
        self.ttl_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.closed: int = 0

    def _fail(self, operation: str) -> None:
        if self.raise_on == operation:
            raise RuntimeError(PASSWORD)

    async def ping(self) -> object:
        self._fail("ping")
        return self.ping_response

    async def set(self, key: str, value: bytes, *, ex: int) -> object:
        self._fail("set")
        self.set_calls.append((key, value, ex))
        return self.set_response

    async def get(self, key: str) -> object:
        self._fail("get")
        self.get_calls.append(key)
        return self.get_response

    async def ttl(self, key: str) -> object:
        self._fail("ttl")
        self.ttl_calls.append(key)
        return self.ttl_response

    async def delete(self, key: str) -> object:
        self._fail("delete")
        self.delete_calls.append(key)
        return self.delete_response

    async def aclose(self) -> None:
        self.closed += 1
        self._fail("close")


class FakeArqPool:
    def __init__(self) -> None:
        self.closed: int = 0
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.raise_on_close = False

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> object:
        self.calls.append((function, args, kwargs))
        return object()

    async def aclose(self) -> None:
        self.closed += 1
        if self.raise_on_close:
            raise RuntimeError(PASSWORD)


def runtime(
    client: FakeRedisClient,
    pool: FakeArqPool | None = None,
    *,
    settings: RedisConnectionSettings | None = None,
) -> RedisRuntime:
    async def pool_factory(_settings: RedisSettings) -> ArqRuntimePool:
        if pool is None:
            raise AssertionError("unexpected arq pool request")
        return cast(ArqRuntimePool, pool)

    return RedisRuntime(
        settings or redis_settings(),
        deployment_id="personal-ai",
        client_factory=lambda _settings: cast(RedisCommandClient, client),
        arq_pool_factory=pool_factory,
    )


def assert_not_started(service: RedisRuntime) -> None:
    assert service.started is False


@pytest.mark.unit
def test_settings_build_component_wise_clients_without_secret_repr() -> None:
    settings = redis_settings(port=6380, database=4, tls=True)
    arq = settings.arq_settings()

    assert arq.host == "redis"
    assert arq.port == 6380
    assert arq.database == 4
    assert arq.password == PASSWORD
    assert arq.ssl is True
    assert arq.conn_retries == 0
    assert repr(settings) == "RedisConnectionSettings(<redacted>)"
    assert PASSWORD not in repr(settings)
    assert settings.safe_log_fields() == {
        "redis": "configured",
        "redis_port": 6380,
        "redis_database": 4,
        "redis_tls": True,
    }


@pytest.mark.unit
async def test_redis_py_client_is_built_from_components_without_url_parser() -> None:
    client = create_redis_client(redis_settings(host="127.0.0.1", port=6380, database=3))
    concrete = cast(object, client)
    pool = concrete.connection_pool  # type: ignore[attr-defined]
    try:
        connection = pool.connection_kwargs
        assert connection["host"] == "127.0.0.1"
        assert connection["port"] == 6380
        assert connection["db"] == 3
        assert connection["password"] == PASSWORD
    finally:
        await client.aclose()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"host": "redis://user:secret@host"}, "REDIS_CONFIG_HOST_INVALID"),
        ({"host": "user@redis"}, "REDIS_CONFIG_HOST_INVALID"),
        ({"host": " redis"}, "REDIS_CONFIG_HOST_INVALID"),
        ({"host": "redis/path"}, "REDIS_CONFIG_HOST_INVALID"),
        ({"port": 0}, "REDIS_CONFIG_PORT_INVALID"),
        ({"port": True}, "REDIS_CONFIG_PORT_INVALID"),
        ({"database": -1}, "REDIS_CONFIG_DATABASE_INVALID"),
        ({"database": True}, "REDIS_CONFIG_DATABASE_INVALID"),
        ({"tls": 1}, "REDIS_CONFIG_TLS_INVALID"),
        ({"connect_timeout_seconds": 0}, "REDIS_CONFIG_TIMEOUT_INVALID"),
        ({"max_connections": 0}, "REDIS_CONFIG_POOL_INVALID"),
        ({"password": SensitiveValue("")}, "REDIS_CONFIG_PASSWORD_INVALID"),
        ({"password": SensitiveValue("bad\npassword")}, "REDIS_CONFIG_PASSWORD_INVALID"),
    ],
)
def test_settings_reject_credential_urls_and_invalid_components(
    overrides: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(RedisRuntimeError) as captured:
        redis_settings(**overrides)
    assert captured.value.code == code
    assert PASSWORD not in repr(captured.value)


@pytest.mark.unit
async def test_connect_probe_arq_notifier_and_idempotent_close() -> None:
    client = FakeRedisClient()
    pool = FakeArqPool()
    service = runtime(client, pool)

    await service.connect()
    assert service.started
    assert await service.probe()
    assert service.job_client is pool
    assert service.durable_job_notifier() is not None
    with pytest.raises(RedisRuntimeError) as captured:
        await service.connect()
    assert captured.value.code == "REDIS_ALREADY_STARTED"

    await service.close()
    assert_not_started(service)
    assert client.closed == 1
    assert pool.closed == 1
    await service.close()
    assert client.closed == 1
    assert pool.closed == 1


@pytest.mark.unit
async def test_connect_without_arq_supports_health_only_process() -> None:
    client = FakeRedisClient()
    service = runtime(client)
    await service.connect(with_arq=False)
    try:
        with pytest.raises(RedisRuntimeError) as captured:
            _ = service.job_client
        assert captured.value.code == "REDIS_ARQ_NOT_STARTED"
    finally:
        await service.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ping_response", "raise_on", "code"),
    [
        (False, None, "REDIS_PING_FAILED"),
        (None, None, "REDIS_PING_FAILED"),
        (True, "ping", "REDIS_CONNECT_FAILED"),
    ],
)
async def test_connect_failure_closes_and_uses_stable_code(
    ping_response: object,
    raise_on: str | None,
    code: str,
) -> None:
    client = FakeRedisClient()
    client.ping_response = ping_response
    client.raise_on = raise_on
    service = runtime(client)

    with pytest.raises(RedisRuntimeError) as captured:
        await service.connect(with_arq=False)
    assert captured.value.code == code
    assert PASSWORD not in repr(captured.value)
    assert client.closed == 1
    assert not service.started


@pytest.mark.unit
async def test_cancelled_arq_connect_closes_client_and_preserves_cancellation() -> None:
    client = FakeRedisClient()

    async def cancelled_pool(_settings: RedisSettings) -> ArqRuntimePool:
        raise asyncio.CancelledError

    service = RedisRuntime(
        redis_settings(),
        deployment_id="personal-ai",
        client_factory=lambda _settings: cast(RedisCommandClient, client),
        arq_pool_factory=cancelled_pool,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.connect()
    assert client.closed == 1
    assert service.started is False


@pytest.mark.unit
async def test_runtime_rejects_bad_deployment_and_unstarted_operations() -> None:
    client = FakeRedisClient()
    with pytest.raises(RedisRuntimeError) as captured:
        RedisRuntime(redis_settings(), deployment_id="Bad")
    assert captured.value.code == "REDIS_DEPLOYMENT_ID_INVALID"

    service = runtime(client)
    for operation in (
        service.ping,
        lambda: service.publish_heartbeat(ServiceName.APP),
        lambda: service.read_heartbeat(ServiceName.APP),
        lambda: service.clear_heartbeat(ServiceName.APP),
    ):
        with pytest.raises(RedisRuntimeError) as unstarted:
            await operation()
        assert unstarted.value.code == "REDIS_NOT_STARTED"


@pytest.mark.unit
async def test_heartbeat_is_fixed_content_free_and_strictly_read() -> None:
    client = FakeRedisClient()
    service = runtime(client)
    await service.connect(with_arq=False)
    try:
        await service.publish_heartbeat(ServiceName.APP)
        assert client.set_calls == [
            (
                "telegram-userbot:v1:personal-ai:service:app:heartbeat",
                b'{"service":"app","version":1}',
                SERVICE_HEARTBEAT_TTL_SECONDS,
            )
        ]
        serialized = repr(client.set_calls)
        assert PASSWORD not in serialized
        assert "message" not in serialized
        assert "body" not in serialized

        client.get_response = client.set_calls[0][1]
        client.ttl_response = 29
        observed = await service.read_heartbeat(ServiceName.APP)
        assert observed.status is ServiceHeartbeatStatus.ALIVE
        assert observed.ttl_seconds == 29
    finally:
        await service.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "ttl"),
    [
        (None, -2),
        (b'{"service":"app","version":1}', -2),
        (b'{"service":"app","version":1}', 0),
        (b'{"service":"app","version":1}', 31),
        (b'{"service":"app","version":1}', True),
        ('{"service":"app","version":1}', 20),
        (b'{"version":1,"service":"app"}', 20),
        (b'{"service":"worker","version":1}', 20),
        (b'{"service":"app","version":1,"body":"forbidden"}', 20),
    ],
)
def test_missing_expired_or_malformed_heartbeat_is_unknown(payload: object, ttl: object) -> None:
    observed = interpret_service_heartbeat(
        service=ServiceName.APP,
        payload=payload,
        ttl_seconds=ttl,
    )
    assert observed.status is ServiceHeartbeatStatus.UNKNOWN
    assert observed.ttl_seconds is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("operation", "code"),
    [
        ("ping", "REDIS_PING_FAILED"),
        ("set", "REDIS_HEARTBEAT_WRITE_FAILED"),
        ("get", "REDIS_HEARTBEAT_READ_FAILED"),
        ("ttl", "REDIS_HEARTBEAT_READ_FAILED"),
        ("delete", "REDIS_HEARTBEAT_DELETE_FAILED"),
    ],
)
async def test_runtime_operation_errors_have_stable_content_free_codes(
    operation: str,
    code: str,
) -> None:
    client = FakeRedisClient()
    service = runtime(client)
    await service.connect(with_arq=False)
    client.raise_on = operation

    async def run_operation() -> None:
        if operation == "ping":
            await service.ping()
        elif operation == "set":
            await service.publish_heartbeat(ServiceName.CONTROL)
        elif operation in {"get", "ttl"}:
            await service.read_heartbeat(ServiceName.CONTROL)
        else:
            await service.clear_heartbeat(ServiceName.CONTROL)

    try:
        with pytest.raises(RedisRuntimeError) as captured:
            await run_operation()
        assert captured.value.code == code
        assert PASSWORD not in repr(captured.value)
    finally:
        client.raise_on = None
        await service.close()


@pytest.mark.unit
async def test_write_delete_and_close_validate_driver_results() -> None:
    client = FakeRedisClient()
    pool = FakeArqPool()
    service = runtime(client, pool)
    await service.connect()

    client.set_response = False
    with pytest.raises(RedisRuntimeError) as write_error:
        await service.publish_heartbeat(ServiceName.WORKER)
    assert write_error.value.code == "REDIS_HEARTBEAT_WRITE_FAILED"

    client.delete_response = True
    with pytest.raises(RedisRuntimeError) as delete_error:
        await service.clear_heartbeat(ServiceName.WORKER)
    assert delete_error.value.code == "REDIS_HEARTBEAT_DELETE_FAILED"

    pool.raise_on_close = True
    client.raise_on = "close"
    with pytest.raises(RedisRuntimeError) as close_error:
        await service.close()
    assert close_error.value.code == "REDIS_CLOSE_FAILED"
    assert not service.started
    await service.close()
