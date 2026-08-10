"""Redis/arq wake-up adapter; PostgreSQL remains the source of truth."""

from collections.abc import Mapping
from typing import Protocol

from telegram_userbot.adapters.persistence.records import OutboxRecord


class ArqRedisClient(Protocol):
    async def enqueue_job(
        self,
        function: str,
        *args: object,
        _job_id: str | None = None,
        _queue_name: str | None = None,
    ) -> object: ...


class DurableJobNotifier:
    """Publish content-free, idempotent notifications derived from the outbox."""

    def __init__(self, redis: ArqRedisClient, *, queue_name: str = "arq:durable") -> None:
        self._redis = redis
        self._queue_name = queue_name

    async def publish(self, record: OutboxRecord) -> None:
        payload = _validated_payload(record)
        await self._redis.enqueue_job(
            "wake_durable_job",
            payload["job_id"],
            payload["dispatch_generation"],
            _job_id=(f"durable:{payload['job_id']}:generation:{payload['dispatch_generation']}"),
            _queue_name=self._queue_name,
        )


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
