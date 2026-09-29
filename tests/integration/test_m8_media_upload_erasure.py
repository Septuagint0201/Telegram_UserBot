"""Real PostgreSQL upload provenance, erasure fencing and shared-family recovery."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanup
from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.media.validation import ImageIngestor
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from tests.integration.test_m5_context_media import NOW, seed_scope
from tests.integration.test_m8_scope_erasure import _advance, _media, _request
from tests.unit.adapters.test_media import image_bytes

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _pending(session: AsyncSession, account: UUID, revision: UUID) -> UUID:
    message, version = (
        await session.execute(
            select(s.message_revisions.c.message_id, s.message_revisions.c.revision_no).where(
                s.message_revisions.c.id == revision
            )
        )
    ).one()
    object_id = uuid7()
    await MediaRepository(session).create_pending(
        object_id=object_id,
        account_id=account,
        object_kind="original",
        parent_object_id=None,
        created_at=NOW,
        source_message_id=message,
        source_revision_no=version,
    )
    return object_id


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_pending_uploads_are_in_scope_before_attachment_and_replay_after_crash(
    db_session: AsyncSession,
    tmp_path: Path,
    scope: str,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, _, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    first = await _pending(db_session, account, revision)
    sibling = await _pending(db_session, account, sibling_revision)
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    stored = store.store_original(account_id=account, object_id=first, image=image)
    other = store.store_original(account_id=account, object_id=sibling, image=image)
    request = await _request(db_session, account, conversation, scope=scope)
    await _advance(db_session, account, request)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    repository = MediaRepository(db_session)
    assert not await repository.mark_ready(
        object_id=first, account_id=account, stored=stored, ready_at=NOW
    )
    leases = await repository.claim_expired(now=NOW, account_id=account)
    assert {item.object_id for item in leases} == (
        {first} if scope == "contact" else {first, sibling}
    )
    await repository.commit_cleanup_boundary()
    # Crash after unlink and before acknowledgment: the lease expires and absence is rechecked.
    for lease in leases:
        assert store.erase_object(
            account_id=account,
            object_id=lease.object_id,
            storage_key=lease.storage_key,
            expected_sha256=lease.sha256,
        )
    cleanup = DurableMediaCleanup(repository=repository, store=store, account_id=account)
    report = await cleanup.run_once(now=NOW + timedelta(minutes=6))
    # The sibling pending upload is now abandoned too; its ordinary timeout is independent.
    assert report.failed == 0
    assert report.already_missing == len(leases)
    assert not store.resolve_key(other.storage_key, must_exist=False).exists()
    await db_session.execute(text("RESET ROLE"))
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.last_error_code).where(
                s.data_erasure_requests.c.id == request
            )
        )
        == "ERASURE_MEDIA_INVENTORY_PENDING"
    )
    with pytest.raises(RuntimeError, match="media_object_erased"):
        store.store_original(account_id=account, object_id=first, image=image)


@pytest.mark.integration
async def test_shared_family_does_not_block_exclusive_cleanup_and_last_owner_releases_it(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, sibling, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    original, provider, shared_paths = await _media(db_session, tmp_path, account, revision)
    await db_session.execute(
        insert(s.message_media).values(
            id=uuid7(),
            account_id=account,
            message_revision_id=sibling_revision,
            media_object_id=provider,
            media_kind="photo",
            position=1,
            metadata_schema_version=1,
        )
    )
    exclusive = await _pending(db_session, account, revision)
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    own = store.store_original(account_id=account, object_id=exclusive, image=image)
    assert await MediaRepository(db_session).mark_ready(
        object_id=exclusive, account_id=account, stored=own, ready_at=NOW
    )
    first = await _request(db_session, account, conversation)
    await _advance(db_session, account, first)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    cleanup = DurableMediaCleanup(repository=MediaRepository(db_session), store=store)
    report = await cleanup.run_once(now=NOW)
    assert report.deleted == 1
    assert report.failed == 0
    assert all(path.exists() for path in shared_paths)
    await db_session.execute(text("RESET ROLE"))
    assert (
        await db_session.scalar(
            select(s.media_objects.c.delete_requested_at).where(s.media_objects.c.id == original)
        )
        is None
    )
    second = await _request(db_session, account, sibling)
    await _advance(db_session, account, second)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    assert (await cleanup.run_once(now=NOW)).deleted == 2
    await db_session.execute(text("RESET ROLE"))
    for request in (first, second):
        await _advance(db_session, account, request)
        assert (
            await db_session.scalar(
                select(s.erasure_progress.c.state).where(
                    s.erasure_progress.c.request_id == request,
                    s.erasure_progress.c.step_name == "physical_media",
                )
            )
            == "completed"
        )
    assert not any(path.exists() for path in shared_paths)


@pytest.mark.integration
async def test_source_erasure_blocks_late_uploads_links_and_resurrection(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, _, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    object_id = await _pending(db_session, account, revision)
    request = await _request(db_session, account, conversation)
    with pytest.raises(DBAPIError, match="MEDIA_ERASURE_WRITE_BLOCKED"):
        async with db_session.begin_nested():
            await _pending(db_session, account, revision)
    await _advance(db_session, account, request)
    statements = (
        insert(s.message_media).values(
            id=uuid7(),
            account_id=account,
            message_revision_id=sibling_revision,
            media_object_id=object_id,
            media_kind="photo",
            position=1,
            metadata_schema_version=1,
        ),
        update(s.media_objects).where(s.media_objects.c.id == object_id).values(status="ready"),
        update(s.media_objects)
        .where(s.media_objects.c.id == object_id)
        .values(delete_requested_at=None),
        update(s.media_objects)
        .where(s.media_objects.c.id == object_id)
        .values(source_revision_id=None),
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="MEDIA_"):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    report = await DurableMediaCleanup(
        repository=MediaRepository(db_session), store=PrivateMediaStore(tmp_path)
    ).run_once(now=NOW)
    assert report.already_missing == 1
    with pytest.raises(DBAPIError, match="MEDIA_ERASURE_IMMUTABLE"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.media_objects)
                .where(s.media_objects.c.id == object_id)
                .values(status="pending")
            )


@pytest.mark.integration
@pytest.mark.parametrize("legacy_status", ["rejected", "failed"])
async def test_abandoned_legacy_pending_and_rejected_rows_are_not_declared_clean_without_file_check(
    db_session: AsyncSession,
    tmp_path: Path,
    legacy_status: str,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    object_id = uuid7()
    await MediaRepository(db_session).create_pending(
        object_id=object_id,
        account_id=account,
        object_kind="original",
        parent_object_id=None,
        created_at=NOW,
    )
    await MediaRepository(db_session).mark_rejected(
        object_id=object_id, account_id=account, error_code="crashed"
    )
    await db_session.execute(
        update(s.media_objects)
        .where(s.media_objects.c.id == object_id)
        .values(status=legacy_status)
    )
    directory = tmp_path / str(account) / "aa"
    directory.mkdir(parents=True)
    legacy = directory / ".ingest-oldfile"
    legacy.write_bytes(b"unattributed private bytes")
    assert not await MediaRepository(db_session).claim_expired(now=NOW + timedelta(minutes=4))
    cleanup = DurableMediaCleanup(
        repository=MediaRepository(db_session), store=PrivateMediaStore(tmp_path)
    )
    report = await cleanup.run_once(now=NOW + timedelta(minutes=5))
    assert report.failed == 1
    assert legacy.exists()
    request = await _request(db_session, account, conversation, scope="account")
    await _advance(db_session, account, request)
    cleanup = DurableMediaCleanup(
        repository=MediaRepository(db_session),
        store=PrivateMediaStore(tmp_path),
        account_id=account,
    )
    assert (await cleanup.run_once(now=NOW + timedelta(minutes=6))).failed == 0
    assert not legacy.exists()
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.media_objects.c.status).where(s.media_objects.c.id == object_id)
        )
        == "deleted"
    )


@pytest.mark.integration
async def test_contact_waits_for_unattributed_legacy_upload_cleanup(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    object_id = uuid7()
    await MediaRepository(db_session).create_pending(
        object_id=object_id,
        account_id=account,
        object_kind="original",
        parent_object_id=None,
        created_at=NOW,
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.last_error_code).where(
                s.data_erasure_requests.c.id == request
            )
        )
        == "ERASURE_MEDIA_UNATTRIBUTED_UPLOAD"
    )
    report = await DurableMediaCleanup(
        repository=MediaRepository(db_session),
        store=PrivateMediaStore(tmp_path),
        account_id=account,
    ).run_once(now=NOW + timedelta(minutes=5))
    assert report.already_missing == 1
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.last_error_code).where(
                s.data_erasure_requests.c.id == request
            )
        )
        == "ERASURE_MEDIA_INVENTORY_PENDING"
    )


@pytest.mark.integration
async def test_upload_provenance_cannot_be_lost_by_migration_rollback(
    isolated_scope_erasure_session: AsyncSession,
) -> None:
    session = isolated_scope_erasure_session
    account, _, _, revision = await seed_scope(session)
    await _pending(session, account, revision)
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    connection = await session.connection()

    def downgrade(sync_connection):  # type: ignore[no-untyped-def]
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = sync_connection
        command.downgrade(config, "0032_scope_metadata_erasure")

    with pytest.raises(RuntimeError, match="MIGRATION_0033_DOWNGRADE_REQUIRES_NO_MEDIA_CLEANUP"):
        async with connection.begin_nested():
            await connection.run_sync(downgrade)
    assert (
        await session.scalar(text("SELECT version_num FROM alembic_version"))
        == "0036_worker_complete"
    )
