"""Whole-scope completion needs independent physical evidence and survives restore."""

import hashlib
import hmac
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import psycopg
import pytest
from psycopg.rows import dict_row
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanup
from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from tests.integration.test_m5_context_media import NOW, seed_scope
from tests.integration.test_m8_scope_erasure import _advance, _media, _request
from tests.operations.test_m8_restore_and_systemd import _load_script

pytestmark = pytest.mark.asyncio(loop_scope="session")
KEY = b"e" * 32


@pytest.mark.integration
async def test_inventory_requires_a_live_owner_for_retained_ready_files(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    _, _, _, sibling_revision = await seed_scope(db_session, existing_account_id=account)
    first, second, paths = await _media(db_session, tmp_path, account, sibling_revision)
    await db_session.execute(
        update(s.media_objects)
        .where(s.media_objects.c.id.in_((first, second)))
        .values(expires_at=NOW + timedelta(days=10))
    )
    orphan = uuid7()
    orphan_path = tmp_path / str(account) / f"{orphan}.png"
    orphan_path.write_bytes(b"ready-upload-never-attached")
    await db_session.execute(
        insert(s.media_objects).values(
            id=orphan,
            account_id=account,
            object_kind="original",
            status="ready",
            storage_key=f"{account}/{orphan}.png",
            sha256=hashlib.sha256(orphan_path.read_bytes()).digest(),
            byte_size=orphan_path.stat().st_size,
            retention_class="media_original_30d",
            expires_at=NOW + timedelta(days=1),
        )
    )
    request = await _request(db_session, account, conversation)
    await _advance(db_session, account, request)
    assert await _inventory(db_session, account, tmp_path) == 1
    assert orphan_path.exists()
    assert all(path.exists() for path in paths)
    assert not (tmp_path / ".erased" / str(account) / str(orphan)).exists()
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    result = await DurableMediaCleanup(
        repository=MediaRepository(db_session),
        store=PrivateMediaStore(tmp_path),
        account_id=account,
    ).run_once(now=NOW + timedelta(days=2))
    await db_session.execute(text("RESET ROLE"))
    assert result.failed == 0
    assert result.deleted == 1
    assert all(path.exists() for path in paths)
    await _advance(db_session, account, request)
    assert (
        await db_session.scalar(
            select(s.data_erasure_requests.c.state).where(s.data_erasure_requests.c.id == request)
        )
        == "completed"
    )


async def _inventory(session: AsyncSession, account: UUID, root: Path) -> int:
    await session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
    report = await DurableMediaCleanup(
        repository=MediaRepository(session), store=PrivateMediaStore(root), account_id=account
    ).run_once(now=NOW)
    await session.execute(text("RESET ROLE"))
    return report.failed


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_completion_waits_for_inventory_and_redacts_preferences(
    db_session: AsyncSession,
    tmp_path: Path,
    scope: str,
) -> None:
    account, conversation, _, _ = await seed_scope(db_session)
    _, sibling, _, _ = await seed_scope(db_session, existing_account_id=account)
    contacts = tuple(
        (
            await db_session.scalars(
                select(s.conversations.c.contact_id)
                .where(s.conversations.c.id.in_((conversation, sibling)))
                .order_by(s.conversations.c.id)
            )
        ).all()
    )
    for contact in contacts:
        await db_session.execute(
            insert(s.proactive_contact_settings).values(
                id=uuid7(),
                account_id=account,
                contact_id=contact,
                version_no=1,
                relationship_level="close",
                timezone_name="Asia/Tokyo",
            )
        )
    policy = uuid7()
    await db_session.execute(
        insert(s.proactive_policies).values(
            id=policy, account_id=account, version_no=1, settings_json={"private": "preference"}
        )
    )
    request = await _request(db_session, account, conversation, scope=scope)
    await _advance(db_session, account, request)
    assert not await db_session.scalar(
        select(s.erasure_ledger.c.id).where(s.erasure_ledger.c.request_id == request)
    )
    # Unknown files (including a restored file whose row is absent) cannot be certified.
    path = tmp_path / str(account) / f"{uuid7()}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan-private-image")
    assert await _inventory(db_session, account, tmp_path) == 1
    assert path.exists()
    await _advance(db_session, account, request)
    assert not await db_session.scalar(
        select(s.erasure_media_checks.c.request_id).where(
            s.erasure_media_checks.c.request_id == request
        )
    )
    path.unlink()  # Explicit isolated-test repair; runtime never guesses ownership.
    assert await _inventory(db_session, account, tmp_path) == 0
    # A worker cannot manufacture filesystem evidence.
    await db_session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
    with pytest.raises(DBAPIError) as caught:
        async with db_session.begin_nested():
            await db_session.execute(
                insert(s.erasure_media_checks).values(request_id=uuid7(), checked_at=NOW)
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    await db_session.execute(text("RESET ROLE"))
    with pytest.raises(RuntimeError, match="synthetic crash before commit"):  # noqa: PT012
        async with db_session.begin_nested():
            await _advance(db_session, account, request)
            raise RuntimeError("synthetic crash before commit")
    assert not await db_session.scalar(
        select(s.erasure_ledger.c.id).where(s.erasure_ledger.c.request_id == request)
    )
    await _advance(db_session, account, request)
    row = (
        await db_session.execute(
            select(s.data_erasure_requests).where(s.data_erasure_requests.c.id == request)
        )
    ).one()
    assert (row.state, row.completed_at, row.last_error_code, row.requested_by) == (
        "completed",
        NOW,
        None,
        None,
    )
    ledger = (
        await db_session.execute(
            select(s.erasure_ledger).where(s.erasure_ledger.c.request_id == request)
        )
    ).one()
    assert ledger.account_scope_hmac == hmac.digest(KEY, account.bytes, "sha256")
    assert ledger.target_scope_hmac == hmac.digest(KEY, (row.contact_id or account).bytes, "sha256")
    assert not await MemoryRepository(db_session).reconcile_erasure_request(
        account_id=account, request_id=request, erasure_scope_secret=KEY, now=NOW
    )
    settings = (
        await db_session.execute(
            select(s.proactive_contact_settings).where(
                s.proactive_contact_settings.c.account_id == account
            )
        )
    ).all()
    for setting in settings:
        erased = scope == "account" or setting.contact_id == row.contact_id
        assert (setting.relationship_level is None) == erased
        assert (setting.timezone_name is None) == erased
    policy_row = (
        await db_session.execute(
            select(s.proactive_policies).where(s.proactive_policies.c.id == policy)
        )
    ).one()
    assert policy_row.settings_json == ({} if scope == "account" else {"private": "preference"})
    with pytest.raises(DBAPIError, match="ERASURE_METADATA_IMMUTABLE"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(s.proactive_contact_settings)
                .where(s.proactive_contact_settings.c.metadata_erased_at.is_not(None))
                .values(relationship_level="close")
            )


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["contact", "account"])
async def test_restore_stages_intent_and_rechecks_each_generation(  # noqa: PLR0915 - two-phase restore proof
    isolated_scope_erasure_engine: AsyncEngine,
    tmp_path: Path,
    scope: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = isolated_scope_erasure_engine
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    memory = uuid7()
    async with sessions() as session, session.begin():
        account, conversation, _, revision = await seed_scope(session)
        contact = await session.scalar(
            select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
        )
        await session.execute(
            insert(s.memories).values(
                id=memory,
                account_id=account,
                contact_id=contact,
                conversation_id=conversation,
                memory_type="fact",
                semantic_key_hash=b"m" * 32,
                status="active",
                current_version_no=1,
            )
        )
        await session.execute(
            insert(s.memory_versions).values(
                id=uuid7(),
                account_id=account,
                memory_id=memory,
                version_no=1,
                operation="create",
                payload_schema_version=1,
                payload={"text": "private-memory"},
                rendered_text="private-memory",
                importance=0.5,
                confidence=0.5,
                time_precision="unknown",
                validator_policy_version="test-v1",
                acceptance_kind="migration",
            )
        )
    assert isinstance(contact, UUID)
    request = uuid7()
    entry = {
        "kind": "erasure",
        "scope_type": scope,
        "request_id": str(request),
        "request_idempotency_key": hashlib.sha256(request.bytes).hexdigest(),
        "policy_version": 1,
        "target_scope_hmac": hmac.digest(
            KEY, (contact if scope == "contact" else account).bytes, "sha256"
        ).hex(),
        "completed_at": NOW.isoformat(),
    }
    module = _load_script("restore_gate")
    memory_request = uuid7()
    memory_entry = {
        **entry,
        "scope_type": "memory",
        "request_id": str(memory_request),
        "target_scope_hmac": hmac.digest(KEY, memory.bytes, "sha256").hex(),
        "request_idempotency_key": hashlib.sha256(memory_request.bytes).hexdigest(),
    }
    entries = (entry, memory_entry)
    dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET ROLE telegram_userbot_migrator")
        module._reset_gate(connection, "test-primary", account)
        module._stage_scope_entries(
            connection,
            deployment_id="test-primary",
            account_id=account,
            generation=1,
            scope_secret=KEY,
            entries=entries,
        )
        connection.commit()
        with pytest.raises(ValueError, match="RESTORE_ERASURE_INCOMPLETE"):
            module._verify_scope_entries(connection, account, KEY, (entry,))
        assert (
            connection.execute(
                "SELECT text_content FROM message_revisions WHERE id=%s", (revision,)
            ).fetchall()[0]["text_content"]
            is not None
        )
    async with sessions() as session, session.begin():
        await _advance(session, account, request)
    async with sessions() as session:
        assert await _inventory(session, account, tmp_path) == 0
    async with sessions() as session, session.begin():
        await _advance(session, account, request)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET ROLE telegram_userbot_migrator")
        for _ in range(2):
            module._stage_scope_entries(
                connection,
                deployment_id="test-primary",
                account_id=account,
                generation=1,
                scope_secret=KEY,
                entries=entries,
            )
            module._verify_scope_entries(connection, account, KEY, (entry,))
            module._replay_ledger(
                connection,
                account_id=account,
                scope_secret=KEY,
                entries=entries,
                replayers=module._resolve_replayers(None),
            )
        module._verify_database_integrity(connection, account)
        assert (
            connection.execute(
                "SELECT text_content FROM message_revisions WHERE id=%s", (revision,)
            ).fetchall()[0]["text_content"]
            is None
        )
        monkeypatch.setattr(module, "_export_connection", lambda: nullcontext(connection))
        monkeypatch.setattr(module, "_read_secret", lambda _path: KEY)
        connection.execute("SET ROLE telegram_userbot_export_runtime")
        output = tmp_path / "ledger.jsonl"
        assert (
            module.export_ledger(
                deployment_id="test-primary",
                account_id=account,
                output_path=output,
                snapshot_id="mixed-scope-ledger",
            )
            == 2
        )
        assert b"private-memory" not in output.read_bytes()
        assert str(account).encode() not in output.read_bytes()
        _, exported = module._load_ledger(
            output,
            expected_digest=hashlib.sha256(output.read_bytes()).hexdigest(),
            deployment_id="test-primary",
            account_id=account,
            scope_secret=KEY,
            supported_scopes=frozenset({"memory", "contact", "account"}),
        )
        assert {value["scope_type"] for value in exported} == {scope, "memory"}
        connection.execute("SET ROLE telegram_userbot_migrator")
        module._reset_gate(connection, "test-primary", account)
        module._stage_scope_entries(
            connection,
            deployment_id="test-primary",
            account_id=account,
            generation=2,
            scope_secret=KEY,
            entries=entries,
        )
        with pytest.raises(ValueError, match="RESTORE_ERASURE_INCOMPLETE"):
            module._verify_scope_entries(connection, account, KEY, (entry,))
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM erasure_media_checks WHERE request_id=%s", (request,)
            ).fetchall()[0]["n"]
            == 0
        )
        connection.commit()
