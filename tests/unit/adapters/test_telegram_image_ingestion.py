import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import ClauseElement

import telegram_userbot.adapters.media.telegram_ingestion as ingestion_module
from telegram_userbot.adapters.media.storage import PrivateMediaStore, StoredMedia
from telegram_userbot.adapters.media.telegram_ingestion import TelegramImageIngestionService
from telegram_userbot.adapters.media.validation import ImageIngestionError, ImageIngestor
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.application.ports.telegram import TelegramPermanentError
from telegram_userbot.application.ports.telegram_peer import TelegramMediaBinding
from telegram_userbot.domain.messaging import MediaKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId

ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000001")
CONVERSATION_ID = UUID("018f0000-0000-7000-8000-000000000002")
MESSAGE_ID = UUID("018f0000-0000-7000-8000-000000000003")
MEDIA_ROW_ID = UUID("018f0000-0000-7000-8000-000000000004")
PROVIDER_ID = UUID("018f0000-0000-7000-8000-000000000005")
NOW = datetime(2026, 8, 26, tzinfo=UTC)


class _NoSource:
    async def iter_image(self, request: TelegramImageDownloadRequest) -> AsyncIterator[bytes]:
        del request
        yield b""


class _MappingsResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _MappingsResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _AttachSession:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _MappingsResult:
        self.statements.append(statement)
        return _MappingsResult(self._row)

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return MEDIA_ROW_ID


class _ThreadStore:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.deleted: list[tuple[str, bytes]] = []

    def write(self) -> StoredMedia:
        self.started.set()
        self.release.wait(timeout=5)
        return StoredMedia("object.png", b"h" * 32, 1, "image/png", 1, 1, True)

    def delete_verified(self, *, storage_key: str, expected_sha256: bytes) -> bool:
        self.deleted.append((storage_key, expected_sha256))
        return True


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback


class _SessionContext:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback


class _TerminalSession:
    def begin(self) -> _Transaction:
        return _Transaction()


class _TerminalSessionFactory:
    def __init__(self) -> None:
        self.session = _TerminalSession()

    def __call__(self) -> _SessionContext:
        return _SessionContext(self.session)


class _PermanentIngestor:
    async def ingest(self, *_args: object, **_kwargs: object) -> object:
        raise TelegramPermanentError("telegram_media_rejected")


def _request() -> TelegramImageDownloadRequest:
    return TelegramImageDownloadRequest(
        AccountId(ACCOUNT_ID),
        ConversationId(CONVERSATION_ID),
        MessageId(MESSAGE_ID),
        1,
        0,
    )


def _binding() -> TelegramMediaBinding:
    return TelegramMediaBinding(
        ACCOUNT_ID,
        CONVERSATION_ID,
        MESSAGE_ID,
        1,
        0,
        MediaKind.PHOTO,
        "v1:photo:1:2:3:",
        "image/jpeg",
        1,
    )


def _service(store: object) -> TelegramImageIngestionService:
    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding | None:
        return _binding()

    return TelegramImageIngestionService(
        cast(async_sessionmaker[AsyncSession], object()),
        load_binding=load,
        source=_NoSource(),
        ingestor=cast(ImageIngestor, object()),
        store=cast(PrivateMediaStore, store),
        now=lambda: NOW,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_attach_locks_and_revalidates_exact_current_revision() -> None:
    session = _AttachSession({"id": MEDIA_ROW_ID})
    service = _service(object())

    assert await service._attach_if_current(
        cast(AsyncSession, session),
        request=_request(),
        target=_binding(),
        media_object_id=PROVIDER_ID,
    )

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    lock_sql = str(cast(ClauseElement, session.statements[0]).compile(dialect=dialect))
    attach_sql = str(cast(ClauseElement, session.statements[1]).compile(dialect=dialect))
    assert "messages.current_revision_no" in lock_sql
    assert "messages.deleted_at IS NULL" in lock_sql
    assert "message_revisions.redacted_at IS NULL" in lock_sql
    assert "message_media.media_object_id IS NULL" in lock_sql
    assert "FOR UPDATE OF messages, message_revisions, message_media" in lock_sql
    assert "message_media.media_object_id IS NULL" in attach_sql
    assert "message_media.account_id" in attach_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_attach_rejects_stale_revision_without_writing() -> None:
    session = _AttachSession(None)
    service = _service(object())

    assert not await service._attach_if_current(
        cast(AsyncSession, session),
        request=_request(),
        target=_binding(),
        media_object_id=PROVIDER_ID,
    )
    assert len(session.statements) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_thread_write_is_awaited_hash_deleted_and_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ThreadStore()
    service = _service(store)
    rejected = AsyncMock()
    monkeypatch.setattr(service, "_reject", rejected)

    task = asyncio.create_task(
        service._write_in_thread(
            store.write,
            object_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
        )
    )
    assert await asyncio.to_thread(store.started.wait, 2)
    task.cancel()
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.deleted == [("object.png", b"h" * 32)]
    rejected.assert_awaited_once_with(
        PROVIDER_ID,
        ACCOUNT_ID,
        "image_ingest_cancelled",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permanent_source_failure_is_durably_rejected_and_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[tuple[str, UUID]] = []

    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def create_pending(self, *, object_id: UUID, **_kwargs: object) -> None:
            operations.append(("pending", object_id))

        async def mark_rejected(self, *, object_id: UUID, **_kwargs: object) -> bool:
            operations.append(("rejected", object_id))
            return True

    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding:
        return _binding()

    monkeypatch.setattr(
        "telegram_userbot.adapters.media.telegram_ingestion.MediaRepository",
        _Repository,
    )
    sessions = _TerminalSessionFactory()
    service = TelegramImageIngestionService(
        cast(async_sessionmaker[AsyncSession], sessions),
        load_binding=load,
        source=_NoSource(),
        ingestor=cast(ImageIngestor, _PermanentIngestor()),
        store=cast(PrivateMediaStore, object()),
        new_uuid=lambda: PROVIDER_ID,
        now=lambda: NOW,
    )
    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_attach_if_current", attach)

    outcome = await service.ingest(_request())

    assert outcome.status.value == "rejected"
    assert outcome.error_code == "image_source_rejected"
    assert operations == [("pending", PROVIDER_ID), ("rejected", PROVIDER_ID)]
    attach.assert_awaited_once_with(
        sessions.session,
        request=_request(),
        target=_binding(),
        media_object_id=PROVIDER_ID,
    )


class _FlowSession(_TerminalSession):
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _MappingsResult:
        self.statements.append(statement)
        return _MappingsResult(None)


class _FlowSessionFactory:
    def __init__(self) -> None:
        self.session = _FlowSession()

    def __call__(self) -> _SessionContext:
        return _SessionContext(self.session)


class _FlowRepository:
    create_error: Exception | None = None
    mark_ready_result = True
    mark_rejected_result = True
    operations: ClassVar[list[tuple[str, UUID]]] = []

    def __init__(self, _session: object) -> None:
        pass

    async def create_pending(self, *, object_id: UUID, **_kwargs: object) -> None:
        type(self).operations.append(("pending", object_id))
        if type(self).create_error is not None:
            error = type(self).create_error
            type(self).create_error = None
            assert error is not None
            raise error

    async def mark_ready(self, *, object_id: UUID, **_kwargs: object) -> bool:
        type(self).operations.append(("ready", object_id))
        return type(self).mark_ready_result

    async def mark_rejected(self, *, object_id: UUID, **_kwargs: object) -> bool:
        type(self).operations.append(("rejected", object_id))
        return type(self).mark_rejected_result


class _FlowIngestor:
    def __init__(self, result: object = object(), error: BaseException | None = None) -> None:
        self.result = result
        self.error = error

    async def ingest(self, *_args: object, **_kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        return self.result


class _FlowStore:
    def __init__(self) -> None:
        self.original = StoredMedia("original", b"o" * 32, 1, "image/png", 1, 1, True)
        self.provider = StoredMedia("provider", b"p" * 32, 1, "image/png", 1, 1, True)
        self.deleted: list[str] = []

    def store_original(self, **_kwargs: object) -> StoredMedia:
        return self.original

    def store_provider_copy(self, **_kwargs: object) -> StoredMedia:
        return self.provider

    def delete_verified(self, *, storage_key: str, expected_sha256: bytes) -> bool:
        del expected_sha256
        self.deleted.append(storage_key)
        return True


def _flow_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    binding: TelegramMediaBinding | None = None,
    ingestor: object | None = None,
    store: _FlowStore | None = None,
    ids: tuple[UUID, ...] = (MEDIA_ROW_ID, PROVIDER_ID),
) -> tuple[TelegramImageIngestionService, _FlowStore, _FlowSessionFactory]:
    sessions = _FlowSessionFactory()
    selected_store = _FlowStore() if store is None else store
    _FlowRepository.operations = []
    _FlowRepository.create_error = None
    _FlowRepository.mark_ready_result = True
    _FlowRepository.mark_rejected_result = True
    monkeypatch.setattr(ingestion_module, "MediaRepository", _FlowRepository)
    remaining = iter(ids)

    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding | None:
        return binding if binding is not None else _binding()

    service = TelegramImageIngestionService(
        cast(async_sessionmaker[AsyncSession], sessions),
        load_binding=load,
        source=_NoSource(),
        ingestor=cast(ImageIngestor, ingestor or _FlowIngestor()),
        store=cast(PrivateMediaStore, selected_store),
        new_uuid=lambda: next(remaining),
        now=lambda: NOW,
    )
    return service, selected_store, sessions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_skips_missing_or_scope_mismatched_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(service, "_load_binding", AsyncMock(return_value=None))
    skipped = await service.ingest(_request())
    assert skipped.status.value == "skipped"

    mismatch = _binding()
    mismatch = TelegramMediaBinding(
        ACCOUNT_ID,
        CONVERSATION_ID,
        MESSAGE_ID,
        2,
        mismatch.position,
        mismatch.kind,
        mismatch.opaque_file_reference,
        mismatch.declared_mime,
        mismatch.declared_size,
    )
    service, _store, _sessions = _flow_service(monkeypatch, binding=mismatch)
    failed = await service.ingest(_request())
    assert failed.status.value == "failed"
    assert failed.error_code == "image_binding_scope_mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_rejects_state_create_and_source_validation_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _sessions = _flow_service(monkeypatch)
    _FlowRepository.create_error = RuntimeError("db unavailable")
    created = await service.ingest(_request())
    assert created.error_code == "image_state_create_failed"

    rejected = AsyncMock()
    service, _store, _sessions = _flow_service(
        monkeypatch,
        ingestor=_FlowIngestor(error=ImageIngestionError("image_bad_magic")),
    )
    monkeypatch.setattr(service, "_reject_terminal", rejected)
    outcome = await service.ingest(_request())
    assert outcome.status.value == "rejected"
    assert outcome.error_code == "image_bad_magic"
    rejected.assert_awaited_once()

    service, _store, _sessions = _flow_service(
        monkeypatch,
        ingestor=_FlowIngestor(error=RuntimeError("download failed")),
    )
    generic_reject = AsyncMock()
    monkeypatch.setattr(service, "_reject", generic_reject)
    outcome = await service.ingest(_request())
    assert outcome.error_code == "image_ingest_failed"
    generic_reject.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_ingest_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_handles_original_commit_and_provider_copy_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(service, "_write_in_thread", AsyncMock(return_value=store.original))
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=False))
    reject = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    outcome = await service.ingest(_request())
    assert outcome.error_code == "image_original_commit_failed"
    assert store.deleted == ["original"]
    reject.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_original_commit_failed")

    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, RuntimeError("copy failed")]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    reject = AsyncMock()
    expire = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    monkeypatch.setattr(service, "_expire_unattached_original", expire)
    outcome = await service.ingest(_request())
    assert outcome.error_code == "image_provider_copy_failed"
    assert outcome.provider_object_id == PROVIDER_ID
    reject.assert_awaited_once_with(PROVIDER_ID, ACCOUNT_ID, "image_provider_copy_failed")
    expire.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_provider_copy_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_provider_attach_ready_and_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, store.provider]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_attach_if_current", attach)
    ready = await service.ingest(_request())
    assert ready.status.value == "ready"
    assert ready.original_object_id == MEDIA_ROW_ID
    assert ready.provider_object_id == PROVIDER_ID
    assert attach.await_count == 1

    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, store.provider]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_attach_if_current", AsyncMock(return_value=False))
    reject = AsyncMock()
    expire = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    monkeypatch.setattr(service, "_expire_unattached_original", expire)
    monkeypatch.setattr(service, "_delete_stored", delete)
    failed = await service.ingest(_request())
    assert failed.error_code == "image_provider_attach_failed"
    delete.assert_awaited_once_with(store.provider)
    reject.assert_awaited_once_with(PROVIDER_ID, ACCOUNT_ID, "image_provider_attach_failed")
    expire.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_provider_attach_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_cancellation_paths_preserve_rejection_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _sessions = _flow_service(
        monkeypatch,
        ingestor=_FlowIngestor(error=asyncio.CancelledError()),
    )
    reject = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    with pytest.raises(asyncio.CancelledError):
        await service.ingest(_request())
    reject.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_ingest_cancelled")

    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, asyncio.CancelledError()]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    reject = AsyncMock()
    expire = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    monkeypatch.setattr(service, "_expire_unattached_original", expire)
    with pytest.raises(asyncio.CancelledError):
        await service.ingest(_request())
    reject.assert_awaited_once_with(PROVIDER_ID, ACCOUNT_ID, "image_provider_copy_cancelled")
    expire.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_provider_copy_cancelled")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingestion_helpers_fail_closed_and_cover_terminal_attach_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, sessions = _flow_service(monkeypatch)
    session = _AttachSession({"id": MEDIA_ROW_ID})
    monkeypatch.setattr(session, "scalar", AsyncMock(return_value=None))
    assert not await service._attach_if_current(
        cast(AsyncSession, session),
        request=_request(),
        target=_binding(),
        media_object_id=PROVIDER_ID,
    )

    monkeypatch.setattr(ingestion_module, "MediaRepository", _FlowRepository)
    _FlowRepository.mark_ready_result = False
    assert not await service._mark_ready(PROVIDER_ID, ACCOUNT_ID, store.provider)
    _FlowRepository.mark_ready_result = True
    _FlowRepository.mark_rejected_result = False
    attach = AsyncMock()
    monkeypatch.setattr(service, "_attach_if_current", attach)
    await service._reject_terminal(
        request=_request(), target=_binding(), object_id=PROVIDER_ID, code="terminal"
    )
    attach.assert_not_awaited()

    _FlowRepository.mark_rejected_result = True
    await service._reject_terminal(
        request=_request(), target=_binding(), object_id=PROVIDER_ID, code="terminal"
    )
    attach.assert_awaited_once()

    class _ExplodingStore(_FlowStore):
        def delete_verified(self, **_kwargs: object) -> bool:
            raise OSError("delete failed")

    exploding = _ExplodingStore()
    service, _store, sessions = _flow_service(monkeypatch, store=exploding)
    await service._delete_stored(exploding.original)

    class _RejectExplodingRepository(_FlowRepository):
        async def mark_rejected(self, **_kwargs: object) -> bool:
            raise RuntimeError("reject failed")

    monkeypatch.setattr(ingestion_module, "MediaRepository", _RejectExplodingRepository)
    await service._reject(PROVIDER_ID, ACCOUNT_ID, "ignored")
    monkeypatch.setattr(
        sessions.session, "execute", AsyncMock(side_effect=RuntimeError("expire failed"))
    )
    await service._expire_unattached_original(PROVIDER_ID, ACCOUNT_ID, "ignored")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_cancellation_during_original_commit_and_provider_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(service, "_write_in_thread", AsyncMock(return_value=store.original))
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(side_effect=asyncio.CancelledError()))
    discard = AsyncMock()
    monkeypatch.setattr(service, "_discard_pending", discard)
    with pytest.raises(asyncio.CancelledError):
        await service.ingest(_request())
    discard.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, store.original)

    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, store.provider]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service, "_attach_if_current", AsyncMock(side_effect=asyncio.CancelledError())
    )
    reject = AsyncMock()
    expire = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    monkeypatch.setattr(service, "_expire_unattached_original", expire)
    with pytest.raises(asyncio.CancelledError):
        await service.ingest(_request())
    reject.assert_awaited_once_with(PROVIDER_ID, ACCOUNT_ID, "image_provider_attach_cancelled")
    expire.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_provider_attach_cancelled")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_provider_ready_false_and_helper_cleanup_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _sessions = _flow_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_write_in_thread",
        AsyncMock(side_effect=[store.original, store.provider]),
    )
    monkeypatch.setattr(service, "_mark_ready", AsyncMock(return_value=True))
    _FlowRepository.mark_ready_result = False
    monkeypatch.setattr(service, "_attach_if_current", AsyncMock())
    reject = AsyncMock()
    expire = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(service, "_reject", reject)
    monkeypatch.setattr(service, "_expire_unattached_original", expire)
    monkeypatch.setattr(service, "_delete_stored", delete)
    outcome = await service.ingest(_request())
    assert outcome.error_code == "image_provider_attach_failed"
    delete.assert_awaited_once_with(store.provider)
    reject.assert_awaited_once_with(PROVIDER_ID, ACCOUNT_ID, "image_provider_attach_failed")
    expire.assert_awaited_once_with(MEDIA_ROW_ID, ACCOUNT_ID, "image_provider_attach_failed")

    _FlowRepository.mark_ready_result = True
    await service._discard_pending(PROVIDER_ID, ACCOUNT_ID, store.original)
    await service._discard_provider_attempt(
        PROVIDER_ID,
        ACCOUNT_ID,
        None,
        MEDIA_ROW_ID,
        "provider_missing",
    )

    class _ReadyExplodingRepository(_FlowRepository):
        async def mark_ready(self, **_kwargs: object) -> bool:
            raise RuntimeError("ready failed")

    monkeypatch.setattr(ingestion_module, "MediaRepository", _ReadyExplodingRepository)
    assert not await TelegramImageIngestionService._mark_ready(
        service, PROVIDER_ID, ACCOUNT_ID, store.provider
    )
