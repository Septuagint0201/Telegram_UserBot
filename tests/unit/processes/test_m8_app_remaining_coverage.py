"""Fake-first coverage for app composition, admission, and media boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.processes.app as app_module
from telegram_userbot.adapters.media.telegram_ingestion import (
    TelegramImageIngestOutcome,
    TelegramImageIngestStatus,
)
from telegram_userbot.adapters.persistence.records import TelegramIngestResult
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.domain.messaging import (
    BodyKind,
    Direction,
    EventKind,
    MediaDescriptor,
    MediaKind,
    MessageBody,
    NormalizedTelegramEvent,
    PeerKind,
)
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.config.production import (
    DatabaseEndpoint,
    ProductionProcess,
    ProductionSettings,
    RedisEndpoint,
    SecretBundle,
)
from telegram_userbot.platform.health import (
    HEALTH_SNAPSHOT_VERSION,
    HealthState,
    RestoreGateState,
    ServiceHeartbeat,
    ServiceName,
    ServiceReadiness,
    ServiceStatusCode,
    disk_safety_ok,
)
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.processes.app import ProductionAppError, ProductionAppRuntime
from telegram_userbot.processes.app import run as app_run

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000001")
CONVERSATION_ID = UUID("01900000-0000-7000-8000-000000000002")
MESSAGE_ID = UUID("01900000-0000-7000-8000-000000000003")


class _Session:
    def __init__(self, state: object = None) -> None:
        self.state = state

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Session:
        return self


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _StatusRepository:
    heartbeats: ClassVar[list[ServiceHeartbeat]] = []
    error = False

    def __init__(self, _session: _Session, service: ServiceName) -> None:
        assert service is ServiceName.APP

    async def heartbeat(self, value: ServiceHeartbeat) -> None:
        if self.error:
            raise RuntimeError("synthetic status persistence failure")
        self.heartbeats.append(value)


class _FixedFactory:
    def __init__(self, value: object) -> None:
        self.value = value

    def __call__(self) -> object:
        return self.value


class _Result:
    def __init__(self, row: object) -> None:
        self.row = row

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return self.row


class _DatabaseSession(_Session):
    def __init__(self, row: object = None, state: object = None, *, error: bool = False) -> None:
        super().__init__(state)
        self.row = row
        self.error = error
        self.executed: list[object] = []

    async def execute(self, query: object) -> _Result:
        self.executed.append(query)
        if self.error:
            raise RuntimeError("synthetic database failure")
        return _Result(self.row)


class _GateRepository:
    gate: ClassVar[object] = None

    def __init__(self, _session: object) -> None:
        return None

    async def get(self, _deployment_id: str) -> object:
        return self.gate


class _Model:
    async def generate(self, _request: object) -> object:
        return object()

    def invalidate_profile(self, _profile_id: UUID, *, minimum_version: int | None = None) -> int:
        del minimum_version
        return 0


class _Scheduler:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready_value = ready
        self.wakes = 0
        self.recovery: object | None = None
        self.cleanup: object | None = None

    def ready(self) -> bool:
        return self.ready_value

    def wake(self) -> None:
        self.wakes += 1

    def bind_pending_image_recovery(self, callback: object) -> None:
        self.recovery = callback

    def bind_media_cleanup(self, callback: object) -> None:
        self.cleanup = callback

    async def run(self, _context: object) -> None:
        return None

    async def drain(self, _deadline: object) -> None:
        return None


class _Redis:
    def __init__(
        self, *, started: bool = False, probe_value: bool = True, error: bool = False
    ) -> None:
        self.started = started
        self.probe_value = probe_value
        self.error = error
        self.connect_calls = 0
        self.clear_calls = 0
        self.close_calls = 0

    async def connect(self, *, with_arq: bool) -> None:
        assert with_arq
        self.started = True
        self.connect_calls += 1

    async def probe(self) -> bool:
        return self.probe_value

    async def publish_heartbeat(self, _service: ServiceName) -> None:
        if self.error:
            raise RuntimeError("synthetic heartbeat failure")

    async def clear_heartbeat(self, _service: ServiceName) -> None:
        self.clear_calls += 1
        if self.error:
            raise RuntimeError("synthetic clear failure")

    async def close(self) -> None:
        self.close_calls += 1
        self.started = False
        if self.error:
            raise RuntimeError("synthetic close failure")


class _Ownership:
    def __init__(self, *, acquired: bool = False, probe_value: bool = True) -> None:
        self.acquired = acquired
        self.probe_value = probe_value
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> None:
        self.acquired = True
        self.acquire_calls += 1

    async def release(self) -> None:
        self.acquired = False
        self.release_calls += 1

    async def probe(self) -> bool:
        return self.probe_value


class _Telethon:
    def __init__(self, *, started: bool = False, error: bool = False) -> None:
        self.started = started
        self.error = error
        self.client = object()
        self.close_calls = 0
        self.intake_client = self

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False
        self.close_calls += 1
        if self.error:
            raise RuntimeError("synthetic telethon close failure")

    async def get_entity(self, value: object) -> object:
        return value

    def iter_download(self, _file: object, *, request_size: int) -> Any:
        async def chunks() -> Any:
            yield b"x" * request_size

        return chunks()


def _settings(*, process: ProductionProcess = ProductionProcess.APP) -> Any:
    identity = SimpleNamespace(
        account_id=ACCOUNT_ID,
        telegram_user_id=42,
        control_bot_user_id=43,
        control_bot_username="SyntheticBot",
        control_admin_user_ids=(43,),
    )
    return SimpleNamespace(
        process=process,
        bootstrap_maintenance=False,
        deployment=SimpleNamespace(
            deployment_id="synthetic-deployment",
            source_commit="a" * 40,
            runtime_identity=identity,
        ),
        database=DatabaseEndpoint("db", 5432, "app", "login", "runtime", "db", "require"),
        redis=RedisEndpoint("redis", 6379, "redis"),
    )


def _secrets() -> SecretBundle:
    return SecretBundle(
        (
            ("db", SensitiveValue(b"db-password")),
            ("redis", SensitiveValue(b"redis-password")),
            ("telegram_api_id", SensitiveValue(b"123")),
            ("telegram_api_hash", SensitiveValue(b"hash")),
            ("credential_master_keyring", SensitiveValue(b"opaque")),
        )
    )


def _runtime(tmp_path: Any = None) -> Any:
    runtime = cast(Any, object.__new__(ProductionAppRuntime))
    runtime._settings = _settings()
    runtime._secrets = _secrets()
    runtime._session_path = (tmp_path or __import__("pathlib").Path(".")) / "account.session"
    runtime._media_root = tmp_path or __import__("pathlib").Path(".")
    runtime._snapshot_path = runtime._media_root / "health.json"
    runtime._new_uuid = uuid7
    runtime._instance_id = uuid7()
    runtime._started_at = datetime.now(UTC) - timedelta(seconds=1)
    runtime._engine = cast(AsyncEngine, SimpleNamespace(dispose=AsyncMock()))
    runtime._sessions = cast(async_sessionmaker[AsyncSession], _SessionFactory())
    runtime._model = _Model()
    runtime._scheduler = _Scheduler()
    runtime._redis = _Redis()
    runtime._ownership = _Ownership()
    runtime._telethon = None
    runtime._live_client = object()
    runtime._telegram_gateway = None
    runtime._media_ingestion = None
    runtime._media_store = object()
    runtime._media_ingest_lock = asyncio.Lock()
    runtime._terminal_image_requests = set()
    runtime._managed_process = None
    runtime._disk_blocked_event = asyncio.Event()
    runtime._disk_recovery_required = False
    runtime._serve_loop_running = False
    runtime._started = False
    runtime._closed = False
    runtime._runtime_marker_observer = SimpleNamespace(
        ready=True,
        stop=lambda: None,
        run=AsyncMock(),
    )
    return runtime


def _request(position: int = 0) -> TelegramImageDownloadRequest:
    return TelegramImageDownloadRequest(
        AccountId(ACCOUNT_ID),
        ConversationId(CONVERSATION_ID),
        MessageId(MESSAGE_ID),
        1,
        position,
    )


@pytest.mark.unit
def test_app_constructor_rejects_process_model_scheduler_and_invalidator_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    with pytest.raises(ProductionAppError, match="APP_PROCESS_SETTINGS_INVALID"):
        ProductionAppRuntime(
            cast(ProductionSettings, _settings(process=ProductionProcess.WORKER)), _secrets()
        )

    monkeypatch.setattr(
        app_module, "create_postgres_engine", lambda _settings: cast(AsyncEngine, object())
    )
    with pytest.raises(ProductionAppError, match="APP_MODEL_GATEWAY_CONFIGURATION_INVALID"):
        ProductionAppRuntime(
            cast(ProductionSettings, _settings()),
            SecretBundle((("db", SensitiveValue(b"x")), ("redis", SensitiveValue(b"x")))),
            model=None,
            scheduler=_Scheduler(),
            media_root=tmp_path,
        )

    with pytest.raises(ProductionAppError, match="APP_MODEL_GATEWAY_REQUIRED"):
        ProductionAppRuntime(
            cast(ProductionSettings, _settings()),
            _secrets(),
            model=cast(Any, object()),
            scheduler=_Scheduler(),
            media_root=tmp_path,
        )

    class NoInvalidator:
        async def generate(self, _request: object) -> object:
            return object()

    with pytest.raises(ProductionAppError, match="APP_MODEL_INVALIDATION_REQUIRED"):
        ProductionAppRuntime(
            cast(ProductionSettings, _settings()),
            _secrets(),
            model=cast(Any, NoInvalidator()),
            scheduler=_Scheduler(),
            media_root=tmp_path,
        )

    with pytest.raises(ProductionAppError, match="APP_SCHEDULER_REQUIRED"):
        ProductionAppRuntime(
            cast(ProductionSettings, _settings()),
            _secrets(),
            model=cast(Any, _Model()),
            scheduler=cast(Any, object()),
            media_root=tmp_path,
        )


@pytest.mark.unit
def test_app_secret_and_dependency_helpers_fail_closed() -> None:
    runtime = _runtime()
    runtime._secrets = SecretBundle(
        (
            ("ascii", SensitiveValue(b"value")),
            ("bad", SensitiveValue(b"")),
            ("db", SensitiveValue(b"db")),
            ("redis", SensitiveValue(b"redis")),
        )
    )
    assert runtime._secret_ascii("ascii", "ERR") == "value"
    with pytest.raises(ProductionAppError, match="ERR"):
        runtime._secret_ascii("missing", "ERR")
    with pytest.raises(ProductionAppError, match="ERR"):
        runtime._secret_ascii("bad", "ERR")

    runtime._settings.redis = None
    with pytest.raises(ProductionAppError, match="APP_REDIS_SETTINGS_REQUIRED"):
        runtime._build_redis()
    runtime._settings.redis = RedisEndpoint("redis", 6379, "redis")
    assert runtime._build_redis().started is False
    assert runtime._database_settings().application_name == "telegram_userbot_app"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_client_and_telethon_requirement_edges() -> None:
    runtime = _runtime()
    with pytest.raises(ProductionAppError, match="APP_TELETHON_NOT_CONFIGURED"):
        runtime._require_telethon()
    telethon = _Telethon(started=True)
    runtime._telethon = telethon
    assert runtime._require_telethon() is telethon
    live = app_module._LiveTelethonClient(runtime._require_telethon)
    assert await live.get_entity("entity") == "entity"
    chunks = live.iter_download("file", request_size=2)
    assert [chunk async for chunk in chunks] == [b"xx"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "gate", "expected"),
    [
        (
            {"telegram_user_id": 42, "status": "active"},
            SimpleNamespace(account_id=ACCOUNT_ID, state=RestoreGateState.OPEN),
            (True, True, True),
        ),
        ({"telegram_user_id": 99, "status": "active"}, None, (True, False, False)),
        (None, None, (True, False, False)),
    ],
)
async def test_app_database_account_restore_projection(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
    gate: object,
    expected: tuple[bool, bool, bool],
) -> None:
    runtime = _runtime()
    session = _DatabaseSession(row)
    runtime._sessions = cast(async_sessionmaker[AsyncSession], _FixedFactory(session))
    monkeypatch.setattr(app_module, "RestoreGateRepository", _GateRepository)
    _GateRepository.gate = gate
    assert await runtime._database_account_and_restore_ready() == expected

    runtime._sessions = cast(
        async_sessionmaker[AsyncSession], _FixedFactory(_DatabaseSession(error=True))
    )
    assert await runtime._database_account_and_restore_ready() == (False, False, False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_peer_media_watermark_and_outbound_repository_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    captured: list[tuple[str, dict[str, object]]] = []

    class Repository:
        def __init__(self, _session: object, **_kwargs: object) -> None:
            return None

        async def admit_private(self, observation: object) -> object:
            captured.append(("admit", {"observation": observation}))
            return object()

        async def find_private_by_chat(self, **kwargs: object) -> object:
            captured.append(("existing", kwargs))
            return object()

        async def find_private_for_deleted_message(self, **kwargs: object) -> object:
            captured.append(("deleted", kwargs))
            return None

        async def resolve_image(self, request: object) -> object:
            captured.append(("image", {"request": request}))
            return None

        async def list_pending_images(self, **kwargs: object) -> tuple[object, ...]:
            captured.append(("pending", kwargs))
            return ()

        async def resolve_outbound(self, **kwargs: object) -> object:
            captured.append(("outbound", kwargs))
            return None

    monkeypatch.setattr(app_module, "PostgresTelegramPeerRepository", Repository)
    runtime._sessions = cast(async_sessionmaker[AsyncSession], _SessionFactory())
    resolver = runtime._peer_resolver()
    await cast(Any, resolver)._admit_private(SimpleNamespace())
    await cast(Any, resolver)._lookup_existing(7)
    await cast(Any, resolver)._lookup_deleted_message(8)
    assert {item[0] for item in captured} >= {"admit", "existing", "deleted"}
    assert await runtime._load_media_binding(_request()) is None
    assert await runtime._pending_image_requests() == ()
    await runtime._outbound_peer(ACCOUNT_ID, CONVERSATION_ID)

    class Cursor:
        def __init__(self, _session: object) -> None:
            return None

        async def load_ingest_watermark(self, **_kwargs: object) -> object:
            return None

    monkeypatch.setattr(app_module, "RuntimeCursorRepository", Cursor)
    assert await runtime._load_account_watermark() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_watermark_value_and_outbound_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    watermark = SimpleNamespace(scope="account", pts=1, pts_count=2, update_identity="u1")

    class Cursor:
        def __init__(self, _session: object) -> None:
            return None

        async def load_ingest_watermark(self, **_kwargs: object) -> object:
            return watermark

    class Lifecycle:
        calls: ClassVar[list[dict[str, object]]] = []

        def __init__(self, _session: object, **_kwargs: object) -> None:
            return None

        async def reconcile_outbound_message_id(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    monkeypatch.setattr(app_module, "RuntimeCursorRepository", Cursor)
    monkeypatch.setattr(app_module, "TelegramLifecycleRepository", Lifecycle)
    runtime._sessions = cast(async_sessionmaker[AsyncSession], _SessionFactory())
    loaded = await runtime._load_account_watermark()
    assert loaded is not None
    assert loaded.update_identity == "u1"
    await runtime._reconcile_outbound_message_id(9, 10, NOW)
    assert Lifecycle.calls[0]["telegram_random_id"] == 9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_media_ingestion_admission_and_recovery_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    request = _request()
    runtime._media_admission = AsyncMock(return_value=SimpleNamespace(allow_media_download=False))
    with pytest.raises(ProductionAppError, match="APP_MEDIA_INGESTION_NOT_CONFIGURED"):
        await runtime._require_media_ingestion()
    blocked = await runtime._ingest_image(request)
    assert blocked.status is TelegramImageIngestStatus.FAILED

    class Ingestion:
        def __init__(self, outcome: TelegramImageIngestOutcome) -> None:
            self.outcome = outcome
            self.calls = 0

        async def ingest(self, _request: object) -> TelegramImageIngestOutcome:
            self.calls += 1
            return self.outcome

    ingestion = Ingestion(TelegramImageIngestOutcome(TelegramImageIngestStatus.REJECTED, "bad"))
    runtime._media_admission = AsyncMock(return_value=SimpleNamespace(allow_media_download=True))
    runtime._media_ingestion = ingestion
    rejected = await runtime._ingest_image(request)
    assert rejected.status is TelegramImageIngestStatus.REJECTED
    assert request in runtime._terminal_image_requests

    runtime._pending_image_requests = AsyncMock(return_value=(request, _request(1)))
    runtime._ingest_image = AsyncMock(
        side_effect=(
            TelegramImageIngestOutcome(TelegramImageIngestStatus.READY),
            TelegramImageIngestOutcome(TelegramImageIngestStatus.SKIPPED),
        )
    )
    runtime._terminal_image_requests = {_request(1)}
    assert await runtime._recover_pending_images_once() == 1
    runtime._media_ingestion = runtime._build_media_ingestion()
    assert runtime._media_ingestion is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_media_and_operation_admission_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._sessions = cast(
        async_sessionmaker[AsyncSession], _FixedFactory(_DatabaseSession(error=True))
    )
    runtime._filesystem_admission = AsyncMock(
        return_value=disk_admission(total_bytes=100 * 1024**3, available_bytes=50 * 1024**3)
    )
    admission = await runtime._media_admission()
    assert admission.operational
    runtime._disk_recovery_required = True
    assert not await runtime._operation_admission()
    runtime._disk_recovery_required = False
    runtime._filesystem_admission = AsyncMock(
        return_value=disk_admission(total_bytes=100 * 1024**3, available_bytes=1)
    )
    assert not await runtime._operation_admission()
    with pytest.raises(ProductionAppError, match="APP_DISK_OPERATOR_RECOVERY_REQUIRED"):
        await runtime._require_operation_admission()

    async def broken_to_thread(*_args: object, **_kwargs: object) -> object:
        raise OSError("synthetic disk error")

    monkeypatch.setattr(cast(Any, app_module).asyncio, "to_thread", broken_to_thread)
    assert not (await runtime._filesystem_admission()).operational
    runtime._disk_recovery_required = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_start_admission_and_filesystem_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    process = cast(Any, SimpleNamespace(accepting_new_work=False))
    runtime._managed_process = process
    with pytest.raises(ProductionAppError, match="APP_DRAINING"):
        await runtime._require_start_admission()

    runtime._managed_process = None
    runtime._operation_admission = AsyncMock(return_value=False)
    with pytest.raises(ProductionAppError, match="APP_DISK_OPERATOR_RECOVERY_REQUIRED"):
        await runtime._require_start_admission()

    async def value_error_to_thread(*_args: object, **_kwargs: object) -> object:
        raise ValueError("synthetic disk value")

    monkeypatch.setattr(cast(Any, app_module).asyncio, "to_thread", value_error_to_thread)
    assert not (await runtime._filesystem_admission()).operational


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_ingest_batch_watermark_and_image_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._operation_admission = AsyncMock(return_value=True)
    event = NormalizedTelegramEvent(
        event_uuid=uuid7(),
        account_id=ACCOUNT_ID,
        conversation_id=CONVERSATION_ID,
        event_kind=EventKind.MESSAGE_CREATED,
        peer_kind=PeerKind.PRIVATE_USER,
        update_identity="u1",
        observed_at=NOW,
        telegram_message_id=1,
        direction=Direction.INCOMING,
        body=MessageBody(BodyKind.TEXT, "hello"),
        media=(MediaDescriptor(MediaKind.PHOTO, 0), MediaDescriptor(MediaKind.VOICE, 1)),
    )
    result = TelegramIngestResult(1, False, True, MESSAGE_ID, 1, "x")

    class IngestService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def ingest_batch(
            self, events: object, *, after_ingest: object
        ) -> tuple[TelegramIngestResult, ...]:
            if not events:
                return ()
            del events
            await cast(Any, after_ingest)(cast(Any, _Session()))
            return (result,)

    class Image:
        async def ingest(self, _request: object) -> TelegramImageIngestOutcome:
            return TelegramImageIngestOutcome(TelegramImageIngestStatus.READY)

    monkeypatch.setattr(app_module, "OrchestratedTelegramIngestService", IngestService)
    runtime._media_ingestion = Image()
    runtime._sessions = cast(async_sessionmaker[AsyncSession], _SessionFactory())
    runtime._ingest_image = AsyncMock(
        return_value=TelegramImageIngestOutcome(TelegramImageIngestStatus.READY)
    )
    assert await runtime._ingest_batch((event,), None) == (result,)
    runtime._media_ingestion = None
    assert await runtime._ingest_batch((), None) == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_runtime_task_and_start_for_serve_branches() -> None:
    runtime = _runtime()
    process = cast(Any, SimpleNamespace(draining=False, accepting_new_work=True))

    async def disk_wait(_process: object) -> None:
        return None

    runtime.start = AsyncMock(side_effect=ProductionAppError("APP_DISK_OPERATOR_RECOVERY_REQUIRED"))
    runtime._disk_recovery_required = True
    runtime._wait_in_disk_blocked_state = disk_wait
    assert not await runtime._start_for_serve(process)

    runtime._disk_recovery_required = False
    runtime.start = AsyncMock(side_effect=ProductionAppError("APP_DRAINING"))
    assert not await runtime._start_for_serve(process)

    runtime.start = AsyncMock(side_effect=ProductionAppError("APP_OTHER"))
    with pytest.raises(ProductionAppError, match="APP_OTHER"):
        await runtime._start_for_serve(process)

    async def done() -> None:
        return None

    t1 = asyncio.create_task(done())
    t2 = asyncio.create_task(done())
    t3 = asyncio.create_task(asyncio.Event().wait())
    t4 = asyncio.create_task(done())
    await asyncio.sleep(0)
    assert not await runtime._runtime_tasks_reached_disk_blocked(
        telethon_task=t1,
        scheduler_task=t2,
        disk_blocked_task=t3,
        marker_task=t4,
    )
    t3.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_close_health_status_and_entrypoint_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._runtime_marker_observer.stop = lambda: None
    runtime._redis = _Redis(started=True, error=True)
    runtime._ownership = _Ownership(acquired=True)
    runtime._telethon = _Telethon(started=True, error=True)
    await runtime._close_started_resources()
    assert runtime._telethon is None
    await runtime.close()
    await runtime.close()

    runtime = _runtime()
    runtime._schema_ready = AsyncMock(return_value=False)
    runtime._database_account_and_restore_ready = AsyncMock(return_value=(False, False, False))
    runtime._redis = _Redis(started=False)
    runtime._ownership = _Ownership(acquired=False)
    runtime._filesystem_admission = AsyncMock(
        return_value=disk_admission(total_bytes=100, available_bytes=50)
    )
    state = await runtime.health(UtcTimestamp(NOW))
    assert not state.database_ok

    for state_value in (
        HealthState.fail_closed(
            ServiceName.WORKER,
            observed_at=UtcTimestamp(NOW),
            draining=False,
            process_loop_ok=True,
        ),
    ):
        runtime._record_service_status = AsyncMock()
        await runtime._record_service_status(state_value, UtcTimestamp(NOW))

    stderr = StringIO()
    assert app_run(["bad"], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "APP_ARGUMENT_INVALID\n"

    monkeypatch.setattr(
        ProductionSettings, "load", lambda *_args: (_ for _ in ()).throw(ValueError())
    )
    stderr = StringIO()
    assert app_run([], {}, stderr=stderr) == 1
    assert stderr.getvalue() == "APP_RUNTIME_FAILED\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_close_records_stopped_before_disposing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    events: list[str] = []

    class OrderedRepository(_StatusRepository):
        async def heartbeat(self, value: ServiceHeartbeat) -> None:
            events.append("status")
            await super().heartbeat(value)

    async def close_started_resources() -> None:
        events.append("resources")

    async def dispose() -> None:
        events.append("engine")

    _StatusRepository.heartbeats = []
    _StatusRepository.error = False
    monkeypatch.setattr(app_module, "ServiceStatusRepository", OrderedRepository)
    runtime._close_started_resources = close_started_resources
    runtime._engine = cast(AsyncEngine, SimpleNamespace(dispose=dispose))

    await runtime.close()
    await runtime.close()

    assert events == ["status", "resources", "engine"]
    assert len(_StatusRepository.heartbeats) == 1
    heartbeat = _StatusRepository.heartbeats[0]
    assert heartbeat.readiness is ServiceReadiness.STOPPED
    assert heartbeat.status_code is ServiceStatusCode.STOPPED
    assert heartbeat.metadata.disk_band == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_close_suppresses_stopped_projection_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    _StatusRepository.error = True
    monkeypatch.setattr(app_module, "ServiceStatusRepository", _StatusRepository)

    await runtime.close()

    assert runtime._closed
    _StatusRepository.error = False


@pytest.mark.unit
def test_app_failure_code_and_disk_helpers_cover_ordering() -> None:
    base = HealthState(
        version=HEALTH_SNAPSHOT_VERSION,
        service=ServiceName.APP,
        observed_at=UtcTimestamp(NOW),
        heartbeat_at=UtcTimestamp(NOW),
        process_loop_ok=True,
        maintenance=False,
        draining=False,
        required_config_ok=True,
        disk_safety_ok=True,
        database_ok=True,
        redis_ok=True,
        schema_ok=True,
        restore_gate_open=True,
        account_ready=True,
        session_owned=True,
        telegram_ready=True,
    )
    assert (
        ProductionAppRuntime._failure_code(replace(base, telegram_ready=False))
        is ServiceStatusCode.TELEGRAM_NOT_READY
    )
    assert disk_safety_ok(total_bytes=100 * 1024**3, available_bytes=50 * 1024**3)
