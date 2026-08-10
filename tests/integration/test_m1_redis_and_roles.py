from datetime import UTC, datetime
from uuid import uuid7

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from telegram_userbot.adapters.persistence.records import NewJobRecord
from telegram_userbot.adapters.persistence.repositories import (
    DurableJobRepository,
    OutboxRepository,
)
from telegram_userbot.adapters.queue.redis import DurableJobNotifier

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.integration
@pytest.mark.recovery
async def test_redis_loss_rebuilds_from_postgres_outbox(
    db_session: AsyncSession, redis_url: str
) -> None:
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    arq_redis = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        jobs = DurableJobRepository(db_session)
        job_id = uuid7()
        await jobs.create(
            NewJobRecord(
                job_id,
                None,
                "memory",
                "refresh",
                b"r" * 32,
                {"conversation_id": str(uuid7())},
                NOW,
            )
        )
        outbox = OutboxRepository(db_session)
        initial = await outbox.claim_batch()
        await DurableJobNotifier(arq_redis).publish(initial[0])
        assert await redis.dbsize() > 0

        await redis.flushdb()
        assert await redis.dbsize() == 0
        assert await jobs.rebuild_notifications(queue_name="memory") == 1
        rebuilt = await outbox.claim_batch()
        latest = max(rebuilt, key=lambda record: record.aggregate_version)
        assert latest.aggregate_version == 2
        await DurableJobNotifier(arq_redis).publish(latest)
        await DurableJobNotifier(arq_redis).publish(latest)
        assert await redis.dbsize() > 0
    finally:
        await arq_redis.aclose()
        await redis.aclose()


@pytest.mark.integration
async def test_runtime_role_cannot_rewrite_audit(postgres_engine: AsyncEngine) -> None:
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
            assert await connection.scalar(text("SELECT COUNT(*) FROM accounts")) is not None
            with pytest.raises(DBAPIError):
                await connection.execute(text("UPDATE audit_log SET action = 'tamper'"))
        finally:
            await transaction.rollback()
