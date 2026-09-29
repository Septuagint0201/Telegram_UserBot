"""Scope deletion must preserve isolation, retry evidence and unfinished work."""

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanup
from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.domain.messaging import EventKind
from telegram_userbot.processes.worker import DurableCompensationPublisher
from tests.integration.test_m3_telegram_lifecycle import normalized
from tests.integration.test_m5_context_media import NOW, seed_scope

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _request(
    session: AsyncSession, account: UUID, conversation: UUID, *, scope: str = "contact"
) -> UUID:
    contact = await session.scalar(
        select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
    )
    request_id = uuid7()
    await session.execute(
        insert(s.data_erasure_requests).values(
            id=request_id,
            account_id=account,
            scope_type=scope,
            contact_id=contact if scope == "contact" else None,
            state="requested",
            requested_by="synthetic-scope-erasure",
            request_idempotency_key=hashlib.sha256(request_id.bytes).digest(),
            policy_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return request_id


async def _advance(session: AsyncSession, account: UUID, request: UUID) -> None:
    await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    assert await MemoryRepository(session).reconcile_erasure_request(
        account_id=account, request_id=request, erasure_scope_secret=b"e" * 32, now=NOW
    )
    await session.execute(text("RESET ROLE"))


async def _media(
    session: AsyncSession, root: Path, account: UUID, revision: UUID
) -> tuple[UUID, UUID, tuple[Path, Path]]:
    original, provider = uuid7(), uuid7()
    paths: list[Path] = []
    for object_id, parent in ((original, None), (provider, original)):
        key = f"{account}/{object_id}.png"
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"synthetic-erasure-media-" + object_id.bytes
        path.write_bytes(payload)
        paths.append(path)
        await session.execute(
            insert(s.media_objects).values(
                id=object_id,
                account_id=account,
                object_kind="original" if parent is None else "provider_copy",
                parent_object_id=parent,
                status="ready",
                storage_key=key,
                sha256=hashlib.sha256(payload).digest(),
                byte_size=len(payload),
                retention_class="media_original_30d"
                if parent is None
                else "media_provider_copy_24h",
                expires_at=NOW + timedelta(days=1),
            )
        )
    await session.execute(
        insert(s.message_media).values(
            id=uuid7(),
            account_id=account,
            message_revision_id=revision,
            media_object_id=provider,
            media_kind="photo",
            position=1,
            metadata_schema_version=1,
        )
    )
    return original, provider, (paths[0], paths[1])


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_scope_redaction_and_physical_media_are_isolated_and_replay_safe(
    db_session: AsyncSession, tmp_path: Path, scope: str
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, sibling, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    other_account, _, _, other_revision = await seed_scope(db_session)
    store = PrivateMediaStore(tmp_path / "media")
    original, provider, paths = await _media(db_session, tmp_path / "media", account, revision)
    _, _, sibling_paths = await _media(db_session, tmp_path / "media", account, sibling_revision)
    _, _, other_paths = await _media(db_session, tmp_path / "media", other_account, other_revision)
    request = await _request(db_session, account, conversation, scope=scope)
    if scope == "account":
        # Old fail-closed requests can resume only for the known unimplemented code.
        await db_session.execute(
            update(s.data_erasure_requests)
            .where(s.data_erasure_requests.c.id == request)
            .values(state="failed", last_error_code="ERASURE_SCOPE_PIPELINE_UNAVAILABLE")
        )
    await _advance(db_session, account, request)
    version = await db_session.scalar(
        select(s.conversations.c.mode_version).where(s.conversations.c.id == conversation)
    )
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.conversations.c.mode_version).where(s.conversations.c.id == conversation)
        )
        == version
    )
    bodies = {
        row[0]: row[1]
        for row in (
            await db_session.execute(
                select(s.message_revisions.c.id, s.message_revisions.c.text_content)
            )
        ).all()
    }
    assert bodies[revision] is None
    assert (bodies[sibling_revision] is None) == (scope == "account")
    assert bodies[other_revision] == "SYNTHETIC_PRIVATE_CONTEXT_BODY"
    assert await db_session.scalar(
        select(s.conversations.c.contact_paused).where(s.conversations.c.id == sibling)
    ) == (scope == "account")
    assert await db_session.scalar(
        select(s.accounts.c.status).where(s.accounts.c.id == account)
    ) == ("deleting" if scope == "account" else "active")
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.state).where(s.data_erasure_requests.c.id == request)
        )
        == "media_cleanup"
    )
    assert all(path.exists() for path in paths)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    for sql in (
        "UPDATE accounts SET display_label = 'not-authorized'",
        "UPDATE message_revisions SET revision_no = revision_no + 1",
        "SELECT ciphertext FROM model_credential_versions",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with db_session.begin_nested():
                await db_session.execute(text(sql))
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    await db_session.execute(text("RESET ROLE"))

    # Exercise the existing app filesystem owner and real role, including commits.
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    cleanup = DurableMediaCleanup(
        repository=MediaRepository(db_session), store=store, account_id=account
    )
    report = await cleanup.run_once(now=NOW)
    assert report.deleted == (4 if scope == "account" else 2)
    assert report.failed == 0
    await db_session.execute(text("RESET ROLE"))
    assert all(not path.exists() for path in paths)
    assert all(path.exists() == (scope == "contact") for path in sibling_paths)
    assert all(path.exists() for path in other_paths)
    await _advance(db_session, account, request)
    state = (
        await db_session.execute(
            select(
                s.data_erasure_requests.c.state,
                s.data_erasure_requests.c.completed_at,
                s.data_erasure_requests.c.last_error_code,
            ).where(s.data_erasure_requests.c.id == request)
        )
    ).one()
    assert state == ("derived_cleanup", None, "ERASURE_MEDIA_INVENTORY_PENDING")
    assert not await db_session.scalar(
        select(s.erasure_ledger.c.id).where(s.erasure_ledger.c.request_id == request)
    )
    for object_id in (original, provider):
        assert (
            await db_session.execute(
                select(
                    s.media_objects.c.status,
                    s.media_objects.c.storage_key,
                    s.media_objects.c.sha256,
                ).where(s.media_objects.c.id == object_id)
            )
        ).one() == ("deleted", None, None)
    # A late Telegram edit must not recreate a redacted revision.
    chat = await db_session.scalar(
        select(s.conversations.c.telegram_chat_id).where(s.conversations.c.id == conversation)
    )
    assert isinstance(chat, int)
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    result = await TelegramLifecycleRepository(db_session).ingest(
        normalized(
            account_id=account,
            conversation_id=conversation,
            telegram_chat_id=chat,
            update_identity="late-edit-after-erasure",
            kind=EventKind.MESSAGE_EDITED,
            text_content="MUST_NOT_REAPPEAR",
        )
    )
    assert result.message_id is None
    assert (
        await db_session.scalar(
            select(s.message_revisions.c.text_content).where(s.message_revisions.c.id == revision)
        )
        is None
    )


@pytest.mark.integration
async def test_shared_media_is_not_expired_by_contact_erasure(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    _, _, _, other_revision = await seed_scope(db_session, existing_account_id=account)
    _, provider, paths = await _media(db_session, tmp_path, account, revision)
    await db_session.execute(
        insert(s.message_media).values(
            id=uuid7(),
            account_id=account,
            message_revision_id=other_revision,
            media_object_id=provider,
            media_kind="photo",
            position=1,
            metadata_schema_version=1,
        )
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.last_error_code).where(
                s.data_erasure_requests.c.id == request
            )
        )
        == "ERASURE_MEDIA_SHARED_SCOPE"
    )
    assert not await MediaRepository(db_session).claim_expired(now=NOW)
    assert all(path.exists() for path in paths)


@pytest.mark.integration
async def test_failed_media_delete_retains_path_hash_and_retry_until_verified(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    account, conversation, _, revision = await seed_scope(db_session)
    store = PrivateMediaStore(tmp_path)
    _, _, paths = await _media(db_session, tmp_path, account, revision)
    original_bytes = paths[0].read_bytes()
    paths[0].write_bytes(b"different-file-must-not-be-deleted")
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    cleanup = DurableMediaCleanup(repository=MediaRepository(db_session), store=store)
    report = await cleanup.run_once(now=NOW)
    assert report.failed == 1
    assert report.deleted == 1
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.state).where(s.data_erasure_requests.c.id == request)
        )
        == "media_cleanup"
    )
    assert (await cleanup.run_once(now=NOW + timedelta(seconds=59))).deleted == 0
    paths[0].write_bytes(original_bytes)
    assert (await cleanup.run_once(now=NOW + timedelta(minutes=1))).deleted == 1
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


@pytest.mark.integration
async def test_compensation_rearms_successful_pending_scope_without_hot_loop(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            async with sessions() as session, session.begin():
                account, conversation, _, _ = await seed_scope(session)
                request = await _request(session, account, conversation)
            publisher = DurableCompensationPublisher(sessions, batch_limit=1)
            await publisher.publish(now=NOW)
            async with sessions() as session, session.begin():
                await session.execute(
                    update(s.background_jobs)
                    .where(s.background_jobs.c.id == request)
                    .values(state="succeeded", updated_at=NOW, completed_at=NOW)
                )
            await publisher.publish(now=NOW + timedelta(seconds=59))
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(s.background_jobs.c.state).where(s.background_jobs.c.id == request)
                    )
                    == "succeeded"
                )
            # An older cooldown request must not fill the batch and starve a new request.
            async with sessions() as session, session.begin():
                newer = await _request(session, account, conversation)
                await session.execute(
                    update(s.data_erasure_requests)
                    .where(s.data_erasure_requests.c.id == newer)
                    .values(created_at=NOW + timedelta(seconds=30))
                )
            await publisher.publish(now=NOW + timedelta(seconds=59))
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(s.background_jobs.c.state).where(s.background_jobs.c.id == newer)
                    )
                    == "pending"
                )
            await publisher.publish(now=NOW + timedelta(minutes=1))
            async with sessions() as session, session.begin():
                assert (
                    await session.scalar(
                        select(s.background_jobs.c.state).where(s.background_jobs.c.id == request)
                    )
                    == "pending"
                )
                await session.execute(
                    update(s.data_erasure_requests)
                    .where(s.data_erasure_requests.c.id == request)
                    .values(
                        state="derived_cleanup",
                        last_error_code="ERASURE_MEDIA_INVENTORY_PENDING",
                    )
                )
                await session.execute(
                    update(s.background_jobs)
                    .where(s.background_jobs.c.id == request)
                    .values(state="succeeded", updated_at=NOW, completed_at=NOW)
                )
            await publisher.publish(now=NOW + timedelta(minutes=2))
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(s.background_jobs.c.state).where(s.background_jobs.c.id == request)
                    )
                    == "pending"
                )
        finally:
            await transaction.rollback()


@pytest.mark.integration
async def test_scope_erasure_serializes_with_an_inflight_telegram_projection(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as seed, seed.begin():
        account, conversation, _, _ = await seed_scope(seed)
        chat = await seed.scalar(
            select(s.conversations.c.telegram_chat_id).where(s.conversations.c.id == conversation)
        )
        assert isinstance(chat, int)
    started = asyncio.Event()

    async def erase() -> None:
        async with sessions() as worker, worker.begin():
            started.set()
            request = await _request(worker, account, conversation)
            await _advance(worker, account, request)

    task: asyncio.Task[None] | None = None
    try:
        async with sessions() as app, app.begin():
            await app.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
            await app.execute(
                select(s.accounts.c.id)
                .where(s.accounts.c.id == account)
                .with_for_update(key_share=True)
            )
            task = asyncio.create_task(erase())
            await started.wait()
            result = await TelegramLifecycleRepository(app).ingest(
                normalized(
                    account_id=account,
                    conversation_id=conversation,
                    telegram_chat_id=chat,
                    update_identity="concurrent-create",
                    message_id=11,
                    text_content="LATE_PRIVATE_BODY",
                )
            )
            assert result.message_id is not None
        async with asyncio.timeout(10):
            await task
        async with sessions() as reader:
            assert set(
                await reader.scalars(
                    select(s.message_revisions.c.text_content).where(
                        s.message_revisions.c.account_id == account
                    )
                )
            ) == {None}
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
