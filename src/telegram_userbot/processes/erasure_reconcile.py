"""Bounded offline erasure reconciliation, including while the restore gate is closed."""

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.schema import data_erasure_requests
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION
from telegram_userbot.platform.config.production import ProductionProcess, ProductionSettings
from telegram_userbot.processes.worker import _database_settings


async def reconcile_once(settings: ProductionSettings) -> int:
    secrets = settings.load_secrets()
    secret = secrets.get("erasure_hmac_key").reveal_for_use()
    engine = create_postgres_engine(_database_settings(settings, secrets))
    try:
        if not await schema_is_ready(
            engine,
            EXPECTED_SCHEMA_REVISION,
            policy=DatabaseReadinessPolicy.for_production_process("worker"),
        ):
            raise RuntimeError("ERASURE_SCHEMA_NOT_READY")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        account = settings.deployment.runtime_identity.account_id
        async with sessions() as session:
            requests = tuple(
                (
                    await session.scalars(
                        select(data_erasure_requests.c.id)
                        .where(
                            data_erasure_requests.c.account_id == account,
                            data_erasure_requests.c.state != "completed",
                        )
                        .order_by(data_erasure_requests.c.updated_at, data_erasure_requests.c.id)
                        .limit(50)
                    )
                ).all()
            )
        advanced = 0
        for request in requests:
            async with sessions() as session, session.begin():
                advanced += await MemoryRepository(session).reconcile_erasure_request(
                    account_id=account,
                    request_id=request,
                    erasure_scope_secret=secret,
                    now=datetime.now(UTC),
                )
        return advanced
    finally:
        await engine.dispose()


def run(
    argv: Sequence[str],
    values: Mapping[str, str],
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    if argv:
        stderr.write("ERASURE_ARGUMENT_INVALID\n")
        return 2
    try:
        count = asyncio.run(
            reconcile_once(ProductionSettings.load(ProductionProcess.WORKER, values))
        )
    except Exception:
        stderr.write("ERASURE_RECONCILIATION_FAILED\n")
        return 1
    stdout.write(f"advanced={count}\n")
    return 0


def main() -> int:
    return run(sys.argv[1:], os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
