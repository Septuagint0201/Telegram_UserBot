"""One bounded media cleanup batch using the app's existing filesystem authority.

This entry point remains usable after account deletion disables Telegram startup.
It opens neither a Telegram Session nor a Redis/provider connection.
"""

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from sqlalchemy.ext.asyncio import async_sessionmaker

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanup, DurableMediaCleanupReport
from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    PostgresConnectionSettings,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION
from telegram_userbot.platform.config.production import ProductionProcess, ProductionSettings

MEDIA_ROOT = Path("/var/lib/telegram-userbot/media")


async def cleanup_once(settings: ProductionSettings) -> DurableMediaCleanupReport:
    endpoint = settings.database
    secrets = settings.load_secrets()
    engine = create_postgres_engine(
        PostgresConnectionSettings(
            host=endpoint.host,
            port=endpoint.port,
            database=endpoint.database,
            login_role=endpoint.login_role,
            runtime_role=endpoint.runtime_role,
            password=SensitiveValue(
                secrets.get(endpoint.password_secret_id).reveal_for_use().decode("ascii")
            ),
            sslmode=endpoint.sslmode,
            application_name="telegram_userbot_media_cleanup",
        )
    )
    try:
        if not await schema_is_ready(
            engine,
            EXPECTED_SCHEMA_REVISION,
            policy=DatabaseReadinessPolicy.for_production_process("app"),
        ):
            raise RuntimeError("MEDIA_CLEANUP_SCHEMA_NOT_READY")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            return await DurableMediaCleanup(
                repository=MediaRepository(session),
                store=PrivateMediaStore(MEDIA_ROOT),
                account_id=settings.deployment.runtime_identity.account_id,
            ).run_once(now=datetime.now(UTC), limit=50)
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
        stderr.write("MEDIA_CLEANUP_ARGUMENT_INVALID\n")
        return 2
    try:
        settings = ProductionSettings.load(ProductionProcess.APP, values)
        report = asyncio.run(cleanup_once(settings))
    except Exception:
        stderr.write("MEDIA_CLEANUP_FAILED\n")
        return 1
    stdout.write(
        f"deleted={report.deleted} missing={report.already_missing} failed={report.failed}\n"
    )
    return 1 if report.failed else 0


def main() -> int:
    return run(sys.argv[1:], os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
