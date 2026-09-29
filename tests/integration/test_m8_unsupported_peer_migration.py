"""Exercise the 0028 upgrade and lossless partial downgrade with real rows."""

from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from telegram_userbot.adapters.persistence.schema import (
    accounts,
    message_events,
    service_instances,
    service_status_events,
)
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION
from tests.integration.test_m1_persistence import NOW

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "0028_m8_background_model_runtime"
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _conversation_nullable(connection: Connection) -> bool:
    return (
        connection.scalar(
            text(
                "SELECT is_nullable = 'YES' FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'message_events' "
                "AND column_name = 'conversation_id'"
            )
        )
        is True
    )


def _round_trip(connection: Connection) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    command.downgrade(config, PREVIOUS_REVISION)
    assert not _conversation_nullable(connection)

    instance_id = uuid7()
    status = {
        "instance_id": instance_id,
        "service_name": "app",
        "readiness": "starting",
        "status_code": "STARTING",
        "metadata": {"deployment_id": "migration-regression"},
    }
    connection.execute(
        insert(service_instances).values(
            **status,
            started_at=NOW,
            last_heartbeat_at=NOW,
            schema_revision=PREVIOUS_REVISION,
        )
    )
    historical_revisions = ["0027_m8_model_run_claim", PREVIOUS_REVISION]
    for revision in historical_revisions:
        connection.execute(
            insert(service_status_events).values(
                **status, event_kind="started", schema_revision=revision, occurred_at=NOW
            )
        )

    command.upgrade(config, "head")
    assert _conversation_nullable(connection)
    assert (
        connection.scalar(
            select(service_instances.c.schema_revision).where(
                service_instances.c.instance_id == instance_id
            )
        )
        == EXPECTED_SCHEMA_REVISION
    )
    # The new process must be able to write its own status event after upgrade.
    connection.execute(
        insert(service_status_events).values(
            **status,
            event_kind="started",
            schema_revision=EXPECTED_SCHEMA_REVISION,
            occurred_at=NOW,
        )
    )
    historical_revisions.append(EXPECTED_SCHEMA_REVISION)

    # An empty/scoped event set can roll back without falsifying status history.
    command.downgrade(config, PREVIOUS_REVISION)
    assert not _conversation_nullable(connection)
    assert (
        connection.scalar(
            select(service_instances.c.schema_revision).where(
                service_instances.c.instance_id == instance_id
            )
        )
        == PREVIOUS_REVISION
    )
    assert (
        list(
            connection.scalars(
                select(service_status_events.c.schema_revision)
                .where(service_status_events.c.instance_id == instance_id)
                .order_by(service_status_events.c.id)
            )
        )
        == historical_revisions
    )
    command.upgrade(config, "head")

    account_id, event_uuid = uuid7(), uuid7()
    connection.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="synthetic-migration",
            status="active",
        )
    )
    connection.execute(
        insert(message_events).values(
            event_uuid=event_uuid,
            account_id=account_id,
            conversation_id=None,
            event_kind="incoming.create",
            fingerprint_version=1,
            update_fingerprint=event_uuid.bytes * 2,
            ordering_key="unsupported-peer",
            metadata_schema_version=1,
            metadata={},
        )
    )
    # Rollback must fail atomically rather than deleting an unsupported event.
    with (
        pytest.raises(RuntimeError, match="MIGRATION_0029_DOWNGRADE_REQUIRES_SCOPED_EVENTS"),
        connection.begin_nested(),
    ):
        command.downgrade(config, PREVIOUS_REVISION)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
        EXPECTED_SCHEMA_REVISION
    )
    assert _conversation_nullable(connection)
    assert connection.execute(
        select(message_events.c.account_id, message_events.c.conversation_id).where(
            message_events.c.event_uuid == event_uuid
        )
    ).one() == (account_id, None)


@pytest.mark.integration
async def test_unsupported_peer_upgrade_preserves_status_and_refuses_lossy_downgrade(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(_round_trip)
        finally:
            await transaction.rollback()
