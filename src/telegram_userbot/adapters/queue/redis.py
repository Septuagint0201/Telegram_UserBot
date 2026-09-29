"""Redis/arq runtime adapter; PostgreSQL remains the source of truth."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis

from telegram_userbot.adapters.persistence.records import OutboxRecord
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.health import ServiceName

_DEPLOYMENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_HEARTBEAT_VERSION = 1
SERVICE_HEARTBEAT_TTL_SECONDS = 30
RUNTIME_MARKER_TTL_SECONDS = 7 * 24 * 60 * 60
_RUNTIME_MARKER_MAX_ID = (1 << 63) - 1
_RUNTIME_MARKER_MAX_ORDER = f"{_RUNTIME_MARKER_MAX_ID:020d}"
RUNTIME_MARKER_TOPICS = frozenset(
    {
        "model.credential.changed",
        "model.config.activated",
        "control.command.requested",
        "control.command.completed",
    }
)
_RUNTIME_MARKER_CAS_SCRIPT = f"""
local current = redis.call('GET', KEYS[1])
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if ok and type(decoded) == 'table' and type(decoded.outbox_order) == 'string' and
       string.len(decoded.outbox_order) == 20 and
       string.match(decoded.outbox_order, '^%d+$') ~= nil and
       decoded.outbox_order <= '{_RUNTIME_MARKER_MAX_ORDER}' then
        if decoded.outbox_order >= ARGV[2] then
            return 0
        end
    end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
""".strip()


class ArqRedisClient(Protocol):
    async def enqueue_job(
        self,
        function: str,
        *args: object,
        _job_id: str | None = None,
        _queue_name: str | None = None,
    ) -> object: ...


class RedisCommandClient(Protocol):
    async def ping(self) -> object: ...

    async def set(self, key: str, value: bytes, *, ex: int) -> object: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    async def get(self, key: str) -> object: ...

    async def ttl(self, key: str) -> object: ...

    async def delete(self, key: str) -> object: ...

    async def aclose(self) -> None: ...


class ArqRuntimePool(ArqRedisClient, Protocol):
    async def aclose(self) -> None: ...


class RedisRuntimeError(RuntimeError):
    """A stable, content-free Redis failure safe for operations logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class RedisConnectionSettings:
    """Component-wise Redis settings with no credential-bearing URL boundary."""

    host: str
    port: int
    database: int
    password: SensitiveValue[str]
    tls: bool = False
    connect_timeout_seconds: int = 2
    max_connections: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise RedisRuntimeError("REDIS_CONFIG_HOST_INVALID")
        host = self.host.strip()
        if (
            not host
            or len(host) > 253
            or host != self.host
            or "://" in host
            or any(character.isspace() or character in "\x00\r\n/@\\" for character in host)
        ):
            raise RedisRuntimeError("REDIS_CONFIG_HOST_INVALID")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise RedisRuntimeError("REDIS_CONFIG_PORT_INVALID")
        if type(self.database) is not int or not 0 <= self.database <= 65535:
            raise RedisRuntimeError("REDIS_CONFIG_DATABASE_INVALID")
        if type(self.tls) is not bool:
            raise RedisRuntimeError("REDIS_CONFIG_TLS_INVALID")
        if (
            type(self.connect_timeout_seconds) is not int
            or not 1 <= self.connect_timeout_seconds <= 30
        ):
            raise RedisRuntimeError("REDIS_CONFIG_TIMEOUT_INVALID")
        if type(self.max_connections) is not int or not 1 <= self.max_connections <= 1000:
            raise RedisRuntimeError("REDIS_CONFIG_POOL_INVALID")
        if not isinstance(self.password, SensitiveValue):
            raise RedisRuntimeError("REDIS_CONFIG_PASSWORD_INVALID")
        password = self.password.reveal_for_use()
        try:
            password_bytes = password.encode("utf-8") if isinstance(password, str) else b""
        except UnicodeEncodeError:
            raise RedisRuntimeError("REDIS_CONFIG_PASSWORD_INVALID") from None
        if (
            not isinstance(password, str)
            or not password
            or len(password_bytes) > 1024
            or any(character in password for character in "\x00\r\n")
        ):
            raise RedisRuntimeError("REDIS_CONFIG_PASSWORD_INVALID")

    def arq_settings(self) -> RedisSettings:
        return RedisSettings(
            host=self.host,
            port=self.port,
            database=self.database,
            password=self.password.reveal_for_use(),
            ssl=self.tls,
            conn_timeout=self.connect_timeout_seconds,
            conn_retries=0,
            max_connections=self.max_connections,
        )

    def safe_log_fields(self) -> dict[str, str | int | bool]:
        return {
            "redis": "configured",
            "redis_port": self.port,
            "redis_database": self.database,
            "redis_tls": self.tls,
        }

    def __repr__(self) -> str:
        return "RedisConnectionSettings(<redacted>)"


class ServiceHeartbeatStatus(StrEnum):
    ALIVE = "alive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ServiceHeartbeat:
    """Strict, content-free observation suitable for Control Bot status output."""

    service: ServiceName
    status: ServiceHeartbeatStatus
    ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class RuntimeGenerationMarker:
    """Strict metadata-only outbox generation broadcast through Redis."""

    outbox_id: int
    topic: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    payload: Mapping[str, str | int | None]
    payload_schema_version: int
    account_id: UUID | None

    @classmethod
    def from_outbox(cls, record: OutboxRecord) -> RuntimeGenerationMarker:
        aggregate_id, payload = _validated_runtime_payload(record)
        return cls(
            record.id,
            record.topic,
            record.aggregate_type,
            aggregate_id,
            record.aggregate_version,
            payload,
            record.payload_schema_version,
            record.account_id,
        )


type RedisClientFactory = Callable[[RedisConnectionSettings], RedisCommandClient]
type ArqPoolFactory = Callable[[RedisSettings], Awaitable[ArqRuntimePool]]


def create_redis_client(settings: RedisConnectionSettings) -> RedisCommandClient:
    """Construct redis-py directly from components; no URL or proxy environment is read."""

    client = Redis(
        host=settings.host,
        port=settings.port,
        db=settings.database,
        password=settings.password.reveal_for_use(),
        ssl=settings.tls,
        socket_connect_timeout=settings.connect_timeout_seconds,
        socket_timeout=settings.connect_timeout_seconds,
        max_connections=settings.max_connections,
        decode_responses=False,
        health_check_interval=10,
    )
    return cast(RedisCommandClient, client)


async def create_arq_pool(settings: RedisSettings) -> ArqRuntimePool:
    return cast(ArqRuntimePool, await create_pool(settings))


def _heartbeat_key(deployment_id: str, service: ServiceName) -> str:
    return f"telegram-userbot:v1:{deployment_id}:service:{service.value}:heartbeat"


def _heartbeat_payload(service: ServiceName) -> bytes:
    # Fixed bytes are deliberately used instead of a caller-provided mapping. This keeps
    # message bodies, identifiers, exception text, and secrets out of service discovery.
    return f'{{"service":"{service.value}","version":{_HEARTBEAT_VERSION}}}'.encode()


def interpret_service_heartbeat(
    *,
    service: ServiceName,
    payload: object,
    ttl_seconds: object,
) -> ServiceHeartbeat:
    """Treat missing, malformed, persistent, or expired records as unknown."""

    if (
        not isinstance(payload, bytes)
        or payload != _heartbeat_payload(service)
        or type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= SERVICE_HEARTBEAT_TTL_SECONDS
    ):
        return ServiceHeartbeat(service, ServiceHeartbeatStatus.UNKNOWN, None)
    return ServiceHeartbeat(service, ServiceHeartbeatStatus.ALIVE, ttl_seconds)


class RedisRuntime:
    """Own the process Redis clients, arq pool, probes, and service heartbeat keys."""

    def __init__(
        self,
        settings: RedisConnectionSettings,
        *,
        deployment_id: str,
        client_factory: RedisClientFactory = create_redis_client,
        arq_pool_factory: ArqPoolFactory = create_arq_pool,
    ) -> None:
        if not isinstance(deployment_id, str) or _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
            raise RedisRuntimeError("REDIS_DEPLOYMENT_ID_INVALID")
        self._settings = settings
        self._deployment_id = deployment_id
        self._client_factory = client_factory
        self._arq_pool_factory = arq_pool_factory
        self._client: RedisCommandClient | None = None
        self._arq_pool: ArqRuntimePool | None = None

    @property
    def started(self) -> bool:
        return self._client is not None

    @property
    def job_client(self) -> ArqRedisClient:
        if self._arq_pool is None:
            raise RedisRuntimeError("REDIS_ARQ_NOT_STARTED")
        return self._arq_pool

    def durable_job_notifier(self, *, queue_name: str = "arq:durable") -> DurableJobNotifier:
        return DurableJobNotifier(self.job_client, queue_name=queue_name)

    async def connect(self, *, with_arq: bool = True) -> None:
        if self._client is not None or self._arq_pool is not None:
            raise RedisRuntimeError("REDIS_ALREADY_STARTED")
        client: RedisCommandClient | None = None
        pool: ArqRuntimePool | None = None
        try:
            client = self._client_factory(self._settings)
            ping = await client.ping()
        except BaseException as error:
            await self._close_safely(pool, client)
            if not isinstance(error, Exception):
                raise
            raise RedisRuntimeError("REDIS_CONNECT_FAILED") from None
        if ping is not True:
            await self._close_safely(pool, client)
            raise RedisRuntimeError("REDIS_PING_FAILED")
        if with_arq:
            try:
                pool = await self._arq_pool_factory(self._settings.arq_settings())
            except BaseException as error:
                await self._close_safely(pool, client)
                if not isinstance(error, Exception):
                    raise
                raise RedisRuntimeError("REDIS_CONNECT_FAILED") from None
        self._client = client
        self._arq_pool = pool

    async def ping(self) -> None:
        client = self._require_client()
        try:
            response = await client.ping()
        except Exception:
            raise RedisRuntimeError("REDIS_PING_FAILED") from None
        if response is not True:
            raise RedisRuntimeError("REDIS_PING_FAILED")

    async def probe(self) -> bool:
        try:
            await self.ping()
        except RedisRuntimeError:
            return False
        return True

    async def publish_heartbeat(self, service: ServiceName) -> None:
        client = self._require_client()
        try:
            stored = await client.set(
                _heartbeat_key(self._deployment_id, service),
                _heartbeat_payload(service),
                ex=SERVICE_HEARTBEAT_TTL_SECONDS,
            )
        except Exception:
            raise RedisRuntimeError("REDIS_HEARTBEAT_WRITE_FAILED") from None
        if stored is not True:
            raise RedisRuntimeError("REDIS_HEARTBEAT_WRITE_FAILED")

    async def read_heartbeat(self, service: ServiceName) -> ServiceHeartbeat:
        client = self._require_client()
        key = _heartbeat_key(self._deployment_id, service)
        try:
            payload = await client.get(key)
            ttl_seconds = await client.ttl(key)
        except Exception:
            raise RedisRuntimeError("REDIS_HEARTBEAT_READ_FAILED") from None
        return interpret_service_heartbeat(
            service=service,
            payload=payload,
            ttl_seconds=ttl_seconds,
        )

    async def clear_heartbeat(self, service: ServiceName) -> None:
        client = self._require_client()
        try:
            deleted = await client.delete(_heartbeat_key(self._deployment_id, service))
        except Exception:
            raise RedisRuntimeError("REDIS_HEARTBEAT_DELETE_FAILED") from None
        if type(deleted) is not int or deleted < 0:
            raise RedisRuntimeError("REDIS_HEARTBEAT_DELETE_FAILED")

    async def publish_generation_marker(self, record: OutboxRecord) -> RuntimeGenerationMarker:
        """Atomically keep the greatest outbox marker for a topic.

        A scheduler-leader handoff can leave an older relay attempt in flight.
        The fixed Lua CAS compares a zero-padded decimal string rather than a
        Lua number, so the full PostgreSQL BIGINT range retains exact ordering.
        PostgreSQL remains canonical truth regardless of the Redis result.
        """

        marker = RuntimeGenerationMarker.from_outbox(record)
        payload = _encode_runtime_marker(marker)
        try:
            stored = await self._require_client().eval(
                _RUNTIME_MARKER_CAS_SCRIPT,
                1,
                _runtime_marker_key(self._deployment_id, marker.topic),
                payload,
                _outbox_order(marker.outbox_id),
                str(RUNTIME_MARKER_TTL_SECONDS),
            )
        except Exception:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_WRITE_FAILED") from None
        if type(stored) is not int or stored not in {0, 1}:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_WRITE_FAILED")
        return marker

    async def read_generation_marker(self, topic: str) -> RuntimeGenerationMarker | None:
        """Read and revalidate the latest broadcast marker for one fixed topic."""

        if topic not in RUNTIME_MARKER_TOPICS:
            raise ValueError("runtime marker topic is unsupported")
        try:
            payload = await self._require_client().get(
                _runtime_marker_key(self._deployment_id, topic)
            )
        except Exception:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_READ_FAILED") from None
        if payload is None:
            return None
        try:
            marker = _decode_runtime_marker(payload)
        except TypeError, ValueError, UnicodeError, json.JSONDecodeError:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_INVALID") from None
        if marker.topic != topic:
            raise RedisRuntimeError("REDIS_RUNTIME_MARKER_INVALID")
        return marker

    async def close(self) -> None:
        pool = self._arq_pool
        client = self._client
        self._arq_pool = None
        self._client = None
        if await self._close_safely(pool, client):
            raise RedisRuntimeError("REDIS_CLOSE_FAILED")

    def _require_client(self) -> RedisCommandClient:
        if self._client is None:
            raise RedisRuntimeError("REDIS_NOT_STARTED")
        return self._client

    @staticmethod
    async def _close_safely(
        pool: ArqRuntimePool | None,
        client: RedisCommandClient | None,
    ) -> bool:
        failed = False
        if pool is not None:
            try:
                await pool.aclose()
            except Exception:
                failed = True
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                failed = True
        return failed


class DurableJobNotifier:
    """Publish content-free, idempotent notifications derived from the outbox."""

    def __init__(self, redis: ArqRedisClient, *, queue_name: str = "arq:durable") -> None:
        self._redis = redis
        self._queue_name = queue_name

    async def publish(self, record: OutboxRecord) -> None:
        payload = _validated_payload(record)
        try:
            await self._redis.enqueue_job(
                "wake_durable_job",
                payload["job_id"],
                payload["dispatch_generation"],
                _job_id=(
                    f"durable:{payload['job_id']}:generation:{payload['dispatch_generation']}"
                ),
                _queue_name=self._queue_name,
            )
        except Exception:
            raise RedisRuntimeError("REDIS_JOB_ENQUEUE_FAILED") from None


def _validated_payload(record: OutboxRecord) -> Mapping[str, str | int]:
    if record.topic != "durable_job.available" or record.aggregate_type != "background_job":
        raise ValueError("outbox record is not a durable job wake-up")
    if set(record.payload) != {"job_id", "dispatch_generation"}:
        raise ValueError("durable job wake-up payload contains unsupported fields")
    job_id = record.payload["job_id"]
    generation = record.payload["dispatch_generation"]
    if not isinstance(job_id, str) or job_id != record.aggregate_id:
        raise ValueError("durable job wake-up identity mismatch")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or generation != record.aggregate_version
    ):
        raise ValueError("durable job wake-up generation mismatch")
    return {"job_id": job_id, "dispatch_generation": generation}


def _runtime_marker_key(deployment_id: str, topic: str) -> str:
    slug = {
        "model.credential.changed": "model-credential",
        "model.config.activated": "model-config",
        "control.command.requested": "control-requested",
        "control.command.completed": "control-completed",
    }.get(topic)
    if slug is None:
        raise ValueError("runtime marker topic is unsupported")
    return f"telegram-userbot:v1:{deployment_id}:runtime-marker:{slug}"


def _validated_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise TypeError("runtime marker UUID is invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("runtime marker UUID is invalid") from None
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("runtime marker UUID is invalid")
    return parsed


def _validated_runtime_payload(
    record: OutboxRecord,
) -> tuple[UUID, Mapping[str, str | int | None]]:
    if (
        record.topic not in RUNTIME_MARKER_TOPICS
        or type(record.id) is not int
        or not 1 <= record.id <= _RUNTIME_MARKER_MAX_ID
        or type(record.aggregate_version) is not int
        or not 1 <= record.aggregate_version <= _RUNTIME_MARKER_MAX_ID
        or type(record.payload_schema_version) is not int
        or record.payload_schema_version != 1
        or not isinstance(record.payload, dict)
    ):
        raise ValueError("runtime outbox record is invalid")
    aggregate_id = _validated_uuid(record.aggregate_id)
    if record.topic == "model.credential.changed":
        if record.account_id is not None:
            raise ValueError("model credential marker is invalid")
        if record.aggregate_type != "model_credential" or set(record.payload) != {
            "profile_id",
            "credential_version_no",
            "status",
        }:
            raise ValueError("model credential marker is invalid")
        profile_id = _validated_uuid(record.payload["profile_id"])
        status = record.payload["status"]
        credential_version = record.payload["credential_version_no"]
        if status not in {"active", "deleted"} or (
            (status == "active" and (type(credential_version) is not int or credential_version < 1))
            or (status == "deleted" and credential_version is not None)
        ):
            raise ValueError("model credential marker is invalid")
        return aggregate_id, {
            "profile_id": str(profile_id),
            "credential_version_no": cast(int | None, credential_version),
            "status": cast(str, status),
        }
    if record.topic == "model.config.activated":
        if record.account_id is not None:
            raise ValueError("model config marker is invalid")
        if record.aggregate_type != "model_profile" or set(record.payload) != {
            "profile_id",
            "config_version_no",
            "state",
        }:
            raise ValueError("model config marker is invalid")
        profile_id = _validated_uuid(record.payload["profile_id"])
        config_version = record.payload["config_version_no"]
        state = record.payload["state"]
        if (
            profile_id != aggregate_id
            or type(config_version) is not int
            or config_version < 1
            or state not in {"active", "disabled"}
        ):
            raise ValueError("model config marker is invalid")
        return aggregate_id, {
            "profile_id": str(profile_id),
            "config_version_no": config_version,
            "state": cast(str, state),
        }
    if (
        not isinstance(record.account_id, UUID)
        or record.account_id.int == 0
        or record.aggregate_type != "control_command"
        or set(record.payload) != {"command_id"}
    ):
        raise ValueError("control command marker is invalid")
    command_id = _validated_uuid(record.payload["command_id"])
    expected_version = 1 if record.topic == "control.command.requested" else 2
    if command_id != aggregate_id or record.aggregate_version != expected_version:
        raise ValueError("control command marker is invalid")
    return aggregate_id, {"command_id": str(command_id)}


def _encode_runtime_marker(marker: RuntimeGenerationMarker) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "outbox_id": marker.outbox_id,
            "outbox_order": _outbox_order(marker.outbox_id),
            "topic": marker.topic,
            "aggregate_type": marker.aggregate_type,
            "aggregate_id": str(marker.aggregate_id),
            "aggregate_version": marker.aggregate_version,
            "payload_schema_version": marker.payload_schema_version,
            "account_id": str(marker.account_id) if marker.account_id is not None else None,
            "payload": dict(marker.payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _unique_runtime_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("runtime marker payload contains a duplicate key")
        value[key] = item
    return value


def _reject_runtime_constant(_value: str) -> object:
    raise ValueError("runtime marker payload contains a non-finite number")


def _decode_runtime_marker(payload: object) -> RuntimeGenerationMarker:
    if not isinstance(payload, bytes) or len(payload) > 1024:
        raise ValueError("runtime marker payload is invalid")
    raw = json.loads(
        payload.decode("ascii"),
        object_pairs_hook=_unique_runtime_object,
        parse_constant=_reject_runtime_constant,
    )
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "outbox_id",
        "outbox_order",
        "topic",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "payload_schema_version",
        "account_id",
        "payload",
    }:
        raise ValueError("runtime marker payload is invalid")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or type(raw["outbox_id"]) is not int
        or not 1 <= raw["outbox_id"] <= _RUNTIME_MARKER_MAX_ID
        or raw["outbox_order"] != _outbox_order(raw["outbox_id"])
        or type(raw["payload_schema_version"]) is not int
        or raw["payload_schema_version"] != 1
    ):
        raise ValueError("runtime marker payload is invalid")
    raw_account_id = raw["account_id"]
    account_id = None if raw_account_id is None else _validated_uuid(raw_account_id)
    record = OutboxRecord(
        id=raw["outbox_id"],
        topic=raw["topic"],
        aggregate_type=raw["aggregate_type"],
        aggregate_id=raw["aggregate_id"],
        aggregate_version=raw["aggregate_version"],
        payload=raw["payload"],
        payload_schema_version=raw["payload_schema_version"],
        account_id=account_id,
    )
    aggregate_id, validated_payload = _validated_runtime_payload(record)
    return RuntimeGenerationMarker(
        record.id,
        record.topic,
        record.aggregate_type,
        aggregate_id,
        record.aggregate_version,
        validated_payload,
        record.payload_schema_version,
        record.account_id,
    )


def _outbox_order(outbox_id: int) -> str:
    if type(outbox_id) is not int or not 1 <= outbox_id <= _RUNTIME_MARKER_MAX_ID:
        raise ValueError("runtime outbox id is invalid")
    return f"{outbox_id:020d}"


__all__ = [
    "RUNTIME_MARKER_TOPICS",
    "RUNTIME_MARKER_TTL_SECONDS",
    "SERVICE_HEARTBEAT_TTL_SECONDS",
    "ArqRedisClient",
    "DurableJobNotifier",
    "RedisConnectionSettings",
    "RedisRuntime",
    "RedisRuntimeError",
    "RuntimeGenerationMarker",
    "ServiceHeartbeat",
    "ServiceHeartbeatStatus",
    "create_arq_pool",
    "create_redis_client",
    "interpret_service_heartbeat",
]
