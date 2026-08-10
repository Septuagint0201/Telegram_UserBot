from typing import Any

import pytest

from telegram_userbot.adapters.persistence.records import OutboxRecord
from telegram_userbot.adapters.queue.redis import DurableJobNotifier


class FakeArqRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> object:
        self.calls.append((function, args, kwargs))
        return object()


def record(payload: dict[str, Any] | None = None) -> OutboxRecord:
    job_id = "019c0000-0000-7000-8000-000000000001"
    return OutboxRecord(
        id=1,
        topic="durable_job.available",
        aggregate_type="background_job",
        aggregate_id=job_id,
        aggregate_version=2,
        payload=payload or {"job_id": job_id, "dispatch_generation": 2},
    )


@pytest.mark.unit
async def test_notifier_uses_stable_content_free_arq_identity() -> None:
    redis = FakeArqRedis()
    notifier = DurableJobNotifier(redis, queue_name="arq:test")
    await notifier.publish(record())
    assert redis.calls == [
        (
            "wake_durable_job",
            ("019c0000-0000-7000-8000-000000000001", 2),
            {
                "_job_id": ("durable:019c0000-0000-7000-8000-000000000001:generation:2"),
                "_queue_name": "arq:test",
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        OutboxRecord(
            1, "other", "background_job", "id", 1, {"job_id": "id", "dispatch_generation": 1}
        ),
        OutboxRecord(
            1, "durable_job.available", "other", "id", 1, {"job_id": "id", "dispatch_generation": 1}
        ),
        record({"job_id": "wrong", "dispatch_generation": 2}),
        record({"job_id": record().aggregate_id, "dispatch_generation": True}),
        record({"job_id": record().aggregate_id, "dispatch_generation": 1}),
        record({"job_id": record().aggregate_id, "dispatch_generation": 2, "body": "forbidden"}),
    ],
)
async def test_notifier_rejects_wrong_or_content_bearing_payload(bad: OutboxRecord) -> None:
    with pytest.raises(ValueError, match=r"outbox|payload|identity|generation"):
        await DurableJobNotifier(FakeArqRedis()).publish(bad)
