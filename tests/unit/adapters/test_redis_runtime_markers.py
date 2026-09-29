from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.records import OutboxRecord
from telegram_userbot.adapters.queue import redis as redis_module
from telegram_userbot.adapters.queue.redis import (
    RedisCommandClient,
    RedisConnectionSettings,
    RedisRuntime,
    RedisRuntimeError,
    RuntimeGenerationMarker,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

ACCOUNT_ID = UUID(int=1)
COMMAND_ID = UUID(int=2)
PASSWORD = "SYNTHETIC_MARKER_PASSWORD"  # noqa: S105 - disposable test value


class _Client:
    def __init__(self) -> None:
        self.eval_result: object = 1
        self.get_response: object = None
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def ping(self) -> object:
        return True

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        self.eval_calls.append((script, numkeys, args))
        return self.eval_result

    async def get(self, key: str) -> object:
        del key
        return self.get_response

    async def aclose(self) -> None:
        return None


def _settings() -> RedisConnectionSettings:
    return RedisConnectionSettings(
        host="redis",
        port=6379,
        database=0,
        password=SensitiveValue(PASSWORD),
    )


def _record(outbox_id: int = 1) -> OutboxRecord:
    return OutboxRecord(
        id=outbox_id,
        topic="control.command.requested",
        aggregate_type="control_command",
        aggregate_id=str(COMMAND_ID),
        aggregate_version=1,
        payload={"command_id": str(COMMAND_ID)},
        account_id=ACCOUNT_ID,
    )


def _runtime(client: _Client) -> RedisRuntime:
    return RedisRuntime(
        _settings(),
        deployment_id="personal-ai",
        client_factory=lambda _settings: cast(RedisCommandClient, client),
    )


@pytest.mark.unit
async def test_generation_marker_uses_zero_padded_bigint_order_and_metadata_only_payload() -> None:
    client = _Client()
    service = _runtime(client)
    await service.connect(with_arq=False)
    try:
        marker = await service.publish_generation_marker(_record((1 << 63) - 1))
        assert marker.outbox_id == (1 << 63) - 1
        _, numkeys, args = client.eval_calls[-1]
        assert numkeys == 1
        key, payload, order, ttl = args
        assert key == "telegram-userbot:v1:personal-ai:runtime-marker:control-requested"
        assert order == "09223372036854775807"
        assert ttl == "604800"
        decoded = json.loads(cast(bytes, payload))
        assert decoded["outbox_order"] == order
        assert "body" not in decoded
        assert "text" not in decoded
    finally:
        await service.close()


@pytest.mark.unit
@pytest.mark.parametrize("outbox_id", [0, 1 << 63])
async def test_generation_marker_rejects_ids_outside_postgres_bigint_range(outbox_id: int) -> None:
    client = _Client()
    service = _runtime(client)
    await service.connect(with_arq=False)
    try:
        with pytest.raises(ValueError, match=r"runtime outbox|invalid"):
            await service.publish_generation_marker(_record(outbox_id))
        assert client.eval_calls == []
    finally:
        await service.close()


@pytest.mark.unit
@pytest.mark.parametrize("eval_result", [True, 2, None, "1"])
async def test_generation_marker_requires_integer_cas_result(eval_result: object) -> None:
    client = _Client()
    client.eval_result = eval_result
    service = _runtime(client)
    await service.connect(with_arq=False)
    try:
        with pytest.raises(RedisRuntimeError) as captured:
            await service.publish_generation_marker(_record())
        assert captured.value.code == "REDIS_RUNTIME_MARKER_WRITE_FAILED"
    finally:
        await service.close()


@pytest.mark.unit
async def test_generation_marker_read_revalidates_payload_and_requested_topic() -> None:
    client = _Client()
    service = _runtime(client)
    await service.connect(with_arq=False)
    try:
        client.get_response = b"{}"
        with pytest.raises(RedisRuntimeError) as malformed:
            await service.read_generation_marker("control.command.requested")
        assert malformed.value.code == "REDIS_RUNTIME_MARKER_INVALID"

        marker = RuntimeGenerationMarker.from_outbox(_record())
        payload = redis_module._encode_runtime_marker(marker)
        client.get_response = payload
        with pytest.raises(RedisRuntimeError) as wrong_topic:
            await service.read_generation_marker("control.command.completed")
        assert wrong_topic.value.code == "REDIS_RUNTIME_MARKER_INVALID"

        client.get_response = payload
        observed = await service.read_generation_marker("control.command.requested")
        assert observed == marker
    finally:
        await service.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"command_id": str(COMMAND_ID), "extra": "forbidden"},
        {"command_id": "not-a-uuid"},
    ],
)
async def test_generation_marker_from_outbox_rejects_unsupported_payload_fields(
    payload: dict[str, Any],
) -> None:
    record = _record()
    with pytest.raises(ValueError, match="marker"):
        RuntimeGenerationMarker.from_outbox(
            OutboxRecord(
                id=record.id,
                topic=record.topic,
                aggregate_type=record.aggregate_type,
                aggregate_id=record.aggregate_id,
                aggregate_version=record.aggregate_version,
                payload=payload,
                payload_schema_version=record.payload_schema_version,
                account_id=record.account_id,
            )
        )


@pytest.mark.unit
def test_cas_script_compares_decimal_order_without_lua_number_conversion() -> None:
    script = redis_module._RUNTIME_MARKER_CAS_SCRIPT
    assert "decoded.outbox_order >= ARGV[2]" in script
    assert "tonumber" not in script
    assert "string.len(decoded.outbox_order) == 20" in script
    assert "decoded.outbox_order <= '09223372036854775807'" in script


@pytest.mark.unit
def test_cas_script_overwrites_malformed_existing_marker() -> None:
    script = redis_module._RUNTIME_MARKER_CAS_SCRIPT
    assert "return -1" not in script
    assert "if ok and type(decoded) == 'table'" in script
    assert "redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])" in script
