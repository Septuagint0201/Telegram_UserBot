"""Production app composition and the sole Telethon Session ownership boundary."""

from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, Protocol, TextIO, cast
from uuid import UUID, uuid7

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanup
from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.media.telegram_ingestion import (
    TelegramImageIngestionService,
    TelegramImageIngestOutcome,
    TelegramImageIngestStatus,
)
from telegram_userbot.adapters.media.validation import ImageIngestor
from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    PostgresConnectionSettings,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.adapters.persistence.ownership import (
    PostgresSessionOwnership,
    SessionOwnershipTarget,
)
from telegram_userbot.adapters.persistence.runtime_cursors import RuntimeCursorRepository
from telegram_userbot.adapters.persistence.schema import accounts
from telegram_userbot.adapters.persistence.service_status import (
    RestoreGateRepository,
    ServiceStatusRepository,
)
from telegram_userbot.adapters.persistence.telegram_peer import PostgresTelegramPeerRepository
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.adapters.queue.redis import RedisConnectionSettings, RedisRuntime
from telegram_userbot.adapters.telegram_user.media import (
    TelethonImageSource,
    TelethonOpaqueMediaResolver,
)
from telegram_userbot.adapters.telegram_user.peer import (
    TelethonBoundPeerResolver,
    TelethonEntityClient,
    TelethonPeerAdmissionResolver,
)
from telegram_userbot.adapters.telegram_user.telethon import TelethonTelegramGateway
from telegram_userbot.adapters.telegram_user.telethon_runtime import (
    TelethonSessionRuntime,
    TelethonSessionRuntimeError,
    TelethonSessionSettings,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import TelegramUpdateWatermark
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.application.ports.model import ModelGateway
from telegram_userbot.application.ports.telegram import TelegramGateway
from telegram_userbot.domain.messaging import NormalizedTelegramEvent
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION, RESOURCE_PROFILE
from telegram_userbot.platform.config.production import (
    ProductionConfigurationError,
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.crypto import CredentialCryptoError, parse_credential_keyring
from telegram_userbot.platform.health import (
    DEFAULT_HEALTH_SNAPSHOT_PATH,
    HEALTH_SNAPSHOT_VERSION,
    HealthState,
    ServiceName,
)
from telegram_userbot.platform.health.disk import (
    DEFAULT_MEDIA_QUOTA_BYTES,
    DiskAdmission,
    disk_admission,
)
from telegram_userbot.platform.health.status import (
    RestoreGateState,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
)
from telegram_userbot.platform.runtime import ManagedProcess, TerminationDeadline
from telegram_userbot.processes.app_scheduler import build_production_app_scheduler
from telegram_userbot.processes.conversation_runtime import OrchestratedTelegramIngestService
from telegram_userbot.processes.durable_queue_inventory import (
    owner_required_surfaces_composed,
)
from telegram_userbot.processes.model_gateway import build_production_model_gateway
from telegram_userbot.processes.runtime_outbox import (
    APP_RUNTIME_MARKER_TOPICS,
    CanonicalRuntimeMarkerConsumer,
    ModelProfileInvalidator,
    RuntimeMarkerObserver,
)

SESSION_PATH = Path("/var/lib/telegram-userbot/session/account.session")
MEDIA_ROOT = Path("/var/lib/telegram-userbot/media")
MEDIA_RECONCILE_LIMIT = 50
_MODEL_INPUT_KEY_PURPOSE = b"model-input-fingerprint"


class ProductionAppError(RuntimeError):
    """Stable, content-free app composition failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AppSchedulerContext:
    sessions: async_sessionmaker[AsyncSession]
    model: ModelGateway
    telegram: TelegramGateway
    redis: RedisRuntime
    owner_instance_id: UUID
    operation_admission: Callable[[], Awaitable[bool]]


class AppScheduler(Protocol):
    """Explicit app-side durable work consumer; no implicit provider is allowed."""

    def ready(self) -> bool: ...

    def wake(self) -> None: ...

    def bind_pending_image_recovery(self, callback: Callable[[], Awaitable[int]]) -> None: ...

    def bind_media_cleanup(self, callback: Callable[[], Awaitable[int]]) -> None: ...

    async def run(self, context: AppSchedulerContext) -> None: ...

    async def drain(self, deadline: TerminationDeadline) -> None: ...


class _EntityMediaClient(Protocol):
    def get_entity(self, entity: object) -> Awaitable[object]: ...

    def iter_download(self, file: object, *, request_size: int) -> AsyncIterator[bytes]: ...


class _LiveTelethonClient:
    """Expose only reviewed entity/media calls from the current sole owner."""

    def __init__(self, runtime: Callable[[], TelethonSessionRuntime]) -> None:
        self._runtime = runtime

    async def get_entity(self, entity: object) -> object:
        client = cast(_EntityMediaClient, self._runtime().intake_client)
        return await client.get_entity(entity)

    def iter_download(self, file: object, *, request_size: int) -> AsyncIterator[bytes]:
        client = cast(_EntityMediaClient, self._runtime().intake_client)
        return client.iter_download(file, request_size=request_size)


class ProductionAppRuntime:
    """Own PostgreSQL/Redis/Telethon in the documented startup and drain order."""

    def __init__(  # noqa: PLR0913 - production side-effect boundaries remain explicit
        self,
        settings: ProductionSettings,
        secrets: SecretBundle,
        *,
        model: ModelGateway | None = None,
        scheduler: AppScheduler | None = None,
        session_path: Path = SESSION_PATH,
        media_root: Path = MEDIA_ROOT,
        snapshot_path: Path = DEFAULT_HEALTH_SNAPSHOT_PATH,
        new_uuid: Callable[[], UUID] = uuid7,
    ) -> None:
        if settings.process is not ProductionProcess.APP:
            raise ProductionAppError("APP_PROCESS_SETTINGS_INVALID")
        self._settings = settings
        self._secrets = secrets
        self._session_path = session_path
        self._media_root = media_root
        self._media_store = PrivateMediaStore(media_root)
        self._snapshot_path = snapshot_path
        self._new_uuid = new_uuid
        self._instance_id = self._new_uuid()
        self._started_at = datetime.now(UTC)
        self._engine = self._build_engine()
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        if model is None:
            try:
                keyring = parse_credential_keyring(
                    secrets.get("credential_master_keyring"),
                    expected_deployment_id=settings.deployment.deployment_id,
                )
                model = build_production_model_gateway(
                    self._sessions,
                    keyring=keyring,
                    input_hmac_key=keyring.derive_runtime_key(_MODEL_INPUT_KEY_PURPOSE),
                    media_root=media_root,
                )
            except (KeyError, CredentialCryptoError) as error:
                raise ProductionAppError("APP_MODEL_GATEWAY_CONFIGURATION_INVALID") from error
        if scheduler is None:
            scheduler = build_production_app_scheduler(settings)
        if not isinstance(model, ModelGateway):
            raise ProductionAppError("APP_MODEL_GATEWAY_REQUIRED")
        if any(
            not callable(getattr(scheduler, name, None))
            for name in (
                "ready",
                "wake",
                "bind_pending_image_recovery",
                "bind_media_cleanup",
                "run",
                "drain",
            )
        ):
            raise ProductionAppError("APP_SCHEDULER_REQUIRED")
        self._model = model
        self._scheduler = scheduler
        self._scheduler.bind_pending_image_recovery(self._recover_pending_images_once)
        self._scheduler.bind_media_cleanup(self._cleanup_expired_media_once)
        self._redis = self._build_redis()
        identity = settings.deployment.runtime_identity
        invalidator = cast(ModelProfileInvalidator, model)
        if not callable(getattr(invalidator, "invalidate_profile", None)):
            raise ProductionAppError("APP_MODEL_INVALIDATION_REQUIRED")
        self._runtime_marker_consumer = CanonicalRuntimeMarkerConsumer(
            sessions=self._sessions,
            topics=APP_RUNTIME_MARKER_TOPICS,
            account_id=identity.account_id,
            model_invalidator=invalidator,
            control_requested=lambda _command_id: self._scheduler.wake(),
        )
        self._runtime_marker_observer = RuntimeMarkerObserver(
            redis=self._redis,
            topics=APP_RUNTIME_MARKER_TOPICS,
            handler=self._runtime_marker_consumer.handle,
            compensate=self._runtime_marker_consumer.compensate,
        )
        self._ownership = PostgresSessionOwnership(
            self._database_settings(),
            SessionOwnershipTarget.telegram_session(
                deployment_id=settings.deployment.deployment_id,
                telegram_account_id=identity.telegram_user_id,
            ),
        )
        self._telethon: TelethonSessionRuntime | None = None
        self._live_client = _LiveTelethonClient(self._require_telethon)
        self._telegram_gateway: TelegramGateway | None = None
        self._media_ingestion: TelegramImageIngestionService | None = None
        self._media_ingest_lock = asyncio.Lock()
        self._terminal_image_requests: set[TelegramImageDownloadRequest] = set()
        self._managed_process: ManagedProcess | None = None
        # Set when a deadline-bounded Telethon teardown cannot be proven to
        # have completed.  A later cleanup pass must not release the account
        # lock while the detached client may still be using the Session file.
        self._teardown_incomplete = False
        self._disk_blocked_event = asyncio.Event()
        self._disk_recovery_required = False
        self._serve_loop_running = False
        self._started = False
        self._closed = False

    def _secret_ascii(self, secret_id: str, code: str) -> str:
        try:
            raw = self._secrets.get(secret_id).reveal_for_use()
            value = raw.decode("ascii")
        except KeyError, UnicodeDecodeError:
            raise ProductionAppError(code) from None
        if not value:
            raise ProductionAppError(code)
        return value

    def _database_settings(self) -> PostgresConnectionSettings:
        endpoint = self._settings.database
        return PostgresConnectionSettings(
            host=endpoint.host,
            port=endpoint.port,
            database=endpoint.database,
            login_role=endpoint.login_role,
            password=SensitiveValue(
                self._secret_ascii(endpoint.password_secret_id, "APP_DATABASE_SECRET_INVALID")
            ),
            runtime_role=endpoint.runtime_role,
            sslmode=endpoint.sslmode,
            application_name="telegram_userbot_app",
        )

    def _build_engine(self) -> AsyncEngine:
        return create_postgres_engine(self._database_settings())

    def _build_redis(self) -> RedisRuntime:
        endpoint = self._settings.redis
        if endpoint is None:
            raise ProductionAppError("APP_REDIS_SETTINGS_REQUIRED")
        return RedisRuntime(
            RedisConnectionSettings(
                host=endpoint.host,
                port=endpoint.port,
                database=0,
                password=SensitiveValue(
                    self._secret_ascii(endpoint.password_secret_id, "APP_REDIS_SECRET_INVALID")
                ),
            ),
            deployment_id=self._settings.deployment.deployment_id,
        )

    async def _database_account_and_restore_ready(self) -> tuple[bool, bool, bool]:
        identity = self._settings.deployment.runtime_identity
        try:
            async with self._sessions() as session:
                account = (
                    (
                        await session.execute(
                            select(accounts.c.telegram_user_id, accounts.c.status).where(
                                accounts.c.id == identity.account_id,
                                accounts.c.deleted_at.is_(None),
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                gate = await RestoreGateRepository(session).get(
                    self._settings.deployment.deployment_id
                )
        except Exception:
            return False, False, False
        account_ready = (
            account is not None
            and account["telegram_user_id"] == identity.telegram_user_id
            and account["status"] == "active"
        )
        restore_ready = (
            gate is not None
            and gate.account_id == identity.account_id
            and gate.state is RestoreGateState.OPEN
        )
        return True, account_ready, restore_ready

    async def _schema_ready(self) -> bool:
        return await schema_is_ready(
            self._engine,
            EXPECTED_SCHEMA_REVISION,
            policy=DatabaseReadinessPolicy.for_production_process("app"),
        )

    def _peer_resolver(self) -> TelethonPeerAdmissionResolver:
        identity = self._settings.deployment.runtime_identity

        async def admit(observation):  # type: ignore[no-untyped-def]
            async with self._sessions() as session, session.begin():
                return await PostgresTelegramPeerRepository(
                    session,
                    new_uuid=self._new_uuid,
                ).admit_private(observation)

        async def existing(telegram_user_id: int):  # type: ignore[no-untyped-def]
            async with self._sessions() as session:
                return await PostgresTelegramPeerRepository(
                    session,
                    new_uuid=self._new_uuid,
                ).find_private_by_chat(
                    account_id=identity.account_id,
                    telegram_user_id=telegram_user_id,
                )

        async def deleted(telegram_message_id: int):  # type: ignore[no-untyped-def]
            async with self._sessions() as session:
                return await PostgresTelegramPeerRepository(
                    session,
                    new_uuid=self._new_uuid,
                ).find_private_for_deleted_message(
                    account_id=identity.account_id,
                    telegram_message_id=telegram_message_id,
                )

        return TelethonPeerAdmissionResolver(
            client=cast(TelethonEntityClient, self._live_client),
            account_id=identity.account_id,
            managed_telegram_user_id=identity.telegram_user_id,
            admit_private=admit,
            lookup_existing=existing,
            lookup_deleted_message=deleted,
        )

    def _require_telethon(self) -> TelethonSessionRuntime:
        if self._telethon is None:
            raise ProductionAppError("APP_TELETHON_NOT_CONFIGURED")
        return self._telethon

    def _force_termination_requested(self) -> bool:
        process = getattr(self, "_managed_process", None)
        return bool(
            process is not None and getattr(process, "force_termination_requested", False) is True
        )

    def _has_process_deadline_seam(self) -> bool:
        process = getattr(self, "_managed_process", None)
        return bool(
            process is not None and callable(getattr(process, "await_before_deadline", None))
        )

    @staticmethod
    def _process_deadline(process: object | None) -> TerminationDeadline | None:
        if process is None:
            return None
        deadline = getattr(process, "termination_deadline", None)
        return deadline if isinstance(deadline, TerminationDeadline) else None

    def _force_after_dependency_deadline(self) -> NoReturn:
        process = getattr(self, "_managed_process", None)
        force = getattr(process, "force_terminate_after_deadline", None)
        if callable(force):
            force()
        raise ProductionAppError("APP_DRAIN_DEADLINE_EXCEEDED")

    async def _await_cleanup(
        self,
        awaitable: Awaitable[object],
        deadline: TerminationDeadline | None,
    ) -> object:
        """Run teardown through the process-owned monotonic deadline when present.

        ``ManagedProcess`` is the only layer allowed to force-exit the PID.  A
        direct unit/maintenance caller may not have that seam, in which case
        the adapter's own deadline (if supplied) remains the last bounded
        guard.  Keeping this wrapper here prevents each dependency cleanup
        path from inventing a different timeout policy.
        """

        process = getattr(self, "_managed_process", None)
        await_before_deadline = (
            getattr(process, "await_before_deadline", None) if process is not None else None
        )
        if deadline is not None and callable(await_before_deadline):
            return await cast(
                Callable[[Awaitable[object], TerminationDeadline], Awaitable[object]],
                await_before_deadline,
            )(awaitable, deadline)
        return await awaitable

    @staticmethod
    def _telethon_close_call(
        runtime: object,
        deadline: TerminationDeadline | None,
    ) -> Awaitable[object]:
        """Call both the reviewed runtime contract and legacy test doubles.

        M8's concrete ``TelethonSessionRuntime.close`` accepts a keyword
        deadline.  A few pre-M8 fakes intentionally expose a no-argument close;
        signature inspection keeps those tests useful without swallowing a
        ``TypeError`` raised *inside* the close implementation.
        """

        close = cast(Callable[..., Awaitable[object]], runtime.close)  # type: ignore[attr-defined]
        if deadline is None:
            return close()
        try:
            parameters = inspect.signature(close).parameters.values()
        except TypeError, ValueError:
            # C-extension/opaque callables are assumed to implement the M8
            # contract; an actual invocation error is intentionally propagated.
            return close(deadline=deadline)
        accepts_deadline = any(
            parameter.name == "deadline" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return close(deadline=deadline) if accepts_deadline else close()

    async def _close_telethon(
        self,
        runtime: object,
        deadline: TerminationDeadline | None,
    ) -> None:
        # The concrete adapter already enforces the same monotonic deadline.
        # When a ManagedProcess seam is available, leave cancellation-resistant
        # disconnect work inside the outer task so the process can force-exit;
        # nesting two timers would turn a detached adapter task into an
        # apparently settled app task.
        adapter_deadline = (
            None if deadline is not None and self._has_process_deadline_seam() else deadline
        )
        try:
            await self._await_cleanup(
                self._telethon_close_call(runtime, adapter_deadline), deadline
            )
        except TelethonSessionRuntimeError as error:
            if error.args == ("TELETHON_DEADLINE_EXCEEDED",):
                self._teardown_incomplete = True
                if deadline is not None and self._has_process_deadline_seam():
                    self._force_after_dependency_deadline()
            raise

    @staticmethod
    def _telethon_start_call(
        runtime: object,
        deadline: TerminationDeadline | None,
    ) -> Awaitable[object]:
        start = cast(Callable[..., Awaitable[object]], runtime.start)  # type: ignore[attr-defined]
        if deadline is None:
            return start()
        try:
            parameters = inspect.signature(start).parameters.values()
        except TypeError, ValueError:
            return start(deadline=deadline)
        accepts_deadline = any(
            parameter.name == "deadline" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return start(deadline=deadline) if accepts_deadline else start()

    async def _load_media_binding(self, request: TelegramImageDownloadRequest):  # type: ignore[no-untyped-def]
        async with self._sessions() as session:
            return await PostgresTelegramPeerRepository(
                session,
                new_uuid=self._new_uuid,
            ).resolve_image(request)

    async def _load_account_watermark(self) -> TelegramUpdateWatermark | None:
        account_id = self._settings.deployment.runtime_identity.account_id
        async with self._sessions() as session:
            value = await RuntimeCursorRepository(session).load_ingest_watermark(
                account_id=account_id,
                scope="account",
            )
        if value is None:
            return None
        return TelegramUpdateWatermark(
            value.scope,
            value.pts,
            value.pts_count,
            value.update_identity,
        )

    async def _reconcile_outbound_message_id(
        self,
        telegram_random_id: int,
        telegram_message_id: int,
        observed_at: datetime,
    ) -> None:
        """Persist Telegram's exact random-id mapping before update intake continues."""

        account_id = self._settings.deployment.runtime_identity.account_id
        async with self._sessions() as session, session.begin():
            await TelegramLifecycleRepository(
                session,
                new_uuid=self._new_uuid,
            ).reconcile_outbound_message_id(
                account_id=account_id,
                telegram_random_id=telegram_random_id,
                telegram_message_id=telegram_message_id,
                now=observed_at,
            )

    def _build_media_ingestion(self) -> TelegramImageIngestionService:
        return TelegramImageIngestionService(
            self._sessions,
            load_binding=self._load_media_binding,
            source=TelethonImageSource(
                self._live_client,
                TelethonOpaqueMediaResolver(self._load_media_binding),
            ),
            ingestor=ImageIngestor(),
            store=self._media_store,
            new_uuid=self._new_uuid,
        )

    def _require_media_ingestion(self) -> TelegramImageIngestionService:
        if self._media_ingestion is None:
            raise ProductionAppError("APP_MEDIA_INGESTION_NOT_CONFIGURED")
        return self._media_ingestion

    async def _pending_image_requests(
        self,
        limit: int = MEDIA_RECONCILE_LIMIT,
    ) -> tuple[TelegramImageDownloadRequest, ...]:
        account_id = self._settings.deployment.runtime_identity.account_id
        async with self._sessions() as session:
            return await PostgresTelegramPeerRepository(
                session,
                new_uuid=self._new_uuid,
            ).list_pending_images(account_id=account_id, limit=limit)

    async def _ingest_image(
        self,
        request: TelegramImageDownloadRequest,
    ) -> TelegramImageIngestOutcome:
        async with self._media_ingest_lock:
            if not (await self._media_admission()).allow_media_download:
                return TelegramImageIngestOutcome(
                    TelegramImageIngestStatus.FAILED,
                    "image_media_quota_blocked",
                )
            outcome = await self._require_media_ingestion().ingest(request)
            if outcome.status is TelegramImageIngestStatus.REJECTED:
                # Avoid repeatedly downloading a permanently invalid object during
                # this process lifetime. A restart deliberately gives it one more
                # chance in case the Telegram file reference was refreshed.
                self._terminal_image_requests.add(request)
            return outcome

    async def _recover_pending_images_once(self) -> int:
        """Repair images missed after canonical event/cursor commit.

        This scan never mutates the Telegram watermark. The exact current media
        slot is revalidated and attached with compare-and-set semantics by the
        ingestion service.
        """

        recovered = 0
        for request in await self._pending_image_requests():
            if request in self._terminal_image_requests:
                continue
            outcome = await self._ingest_image(request)
            if outcome.status is TelegramImageIngestStatus.READY:
                recovered += 1
        return recovered

    async def _cleanup_expired_media_once(self) -> int:
        """Delete one bounded batch while retaining durable cleanup state."""

        async with self._sessions() as session:
            report = await DurableMediaCleanup(
                repository=MediaRepository(session),
                store=self._media_store,
                account_id=self._settings.deployment.runtime_identity.account_id,
            ).run_once(
                now=datetime.now(UTC),
                limit=MEDIA_RECONCILE_LIMIT,
            )
        return report.deleted + report.already_missing

    async def _media_admission(self) -> DiskAdmission:
        account_id = self._settings.deployment.runtime_identity.account_id
        try:
            async with self._sessions() as session:
                media_bytes = await MediaRepository(session).ready_bytes(account_id=account_id)
        except Exception:
            # Database ambiguity must never authorize another media side effect.
            media_bytes = DEFAULT_MEDIA_QUOTA_BYTES
        return await self._filesystem_admission(media_bytes=media_bytes)

    async def _operation_admission(self) -> bool:
        """Synchronously recheck the hard disk gate before model/send work."""

        if self._disk_recovery_required:
            return False
        admission = await self._filesystem_admission()
        if not admission.operational:
            self._mark_disk_blocked()
            return False
        return True

    async def _require_operation_admission(self) -> None:
        if not await self._operation_admission():
            raise ProductionAppError("APP_DISK_OPERATOR_RECOVERY_REQUIRED")

    async def _require_start_admission(self) -> None:
        """Fence each startup side effect against a concurrent process drain."""

        process = self._managed_process
        if process is not None and not process.accepting_new_work:
            raise ProductionAppError("APP_DRAINING")
        await self._require_operation_admission()
        process = self._managed_process
        if process is not None and not process.accepting_new_work:
            raise ProductionAppError("APP_DRAINING")

    def _mark_disk_blocked(self) -> None:
        """Enter a sticky, operator-recovered disk safety state.

        Docker uses ``restart: unless-stopped`` for this process. Exiting on a
        persistent 95%/free-space breach would therefore reconnect the sole
        Telegram Session on every restart. Keep the process alive and NOT_READY
        instead; after space is reclaimed an operator restarts the container.
        """

        self._disk_recovery_required = True
        self._disk_blocked_event.set()

    async def _wait_in_disk_blocked_state(self, process: ManagedProcess) -> None:
        """Disconnect side-effect owners, then wait for explicit operator action."""

        self._mark_disk_blocked()
        deadline = TerminationDeadline(
            started_at=MonotonicInstant(asyncio.get_running_loop().time()),
            grace_seconds=30.0,
        )
        try:
            await self._await_cleanup(self._scheduler.drain(deadline), deadline)
        except Exception:
            if self._force_termination_requested():
                raise
        await self._close_started_resources(deadline=deadline)
        await process.wait_for_drain()

    async def _filesystem_admission(self, *, media_bytes: int = 0) -> DiskAdmission:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, self._media_root)
            return disk_admission(
                total_bytes=usage.total,
                available_bytes=usage.free,
                media_bytes=media_bytes,
            )
        except OSError, ValueError:
            return disk_admission(total_bytes=1, available_bytes=0)

    async def _ingest_batch(
        self,
        events: tuple[NormalizedTelegramEvent, ...],
        watermark: TelegramUpdateWatermark | None,
    ) -> object:
        if not await self._operation_admission():
            raise ProductionAppError("APP_DISK_OPERATOR_RECOVERY_REQUIRED")
        account_id = self._settings.deployment.runtime_identity.account_id
        service = OrchestratedTelegramIngestService(
            self._sessions,
            new_uuid=self._new_uuid,
        )

        async def record(session: AsyncSession) -> None:
            if watermark is None:
                return
            repository = RuntimeCursorRepository(session)
            current = await repository.load_ingest_watermark(
                account_id=account_id,
                scope=watermark.scope,
            )
            saved = await repository.record_durable_ingest(
                account_id=account_id,
                scope=watermark.scope,
                pts=watermark.pts,
                pts_count=watermark.pts_count,
                update_identity=watermark.update_identity,
                durable_ingested_at=max(
                    (event.observed_at for event in events), default=datetime.now(UTC)
                ),
                expected_version=None if current is None else current.version,
            )
            if saved is None:
                raise ProductionAppError("APP_TELEGRAM_WATERMARK_CONFLICT")

        results = await service.ingest_batch(events, after_ingest=record)
        # Telethon emits catch-up updates before its runtime exposes the connected
        # client. Their image rows remain durable and are picked up by the recovery
        # scan once startup completes.
        if not events or self._media_ingestion is None:
            return results
        for event, result in zip(events, results, strict=True):
            if (
                result.duplicate
                or result.message_id is None
                or result.revision_no is None
                or event.conversation_id is None
            ):
                continue
            for media in event.media:
                if not media.kind.image_download_eligible:
                    continue
                await self._ingest_image(
                    TelegramImageDownloadRequest(
                        AccountId(account_id),
                        ConversationId(event.conversation_id),
                        MessageId(result.message_id),
                        result.revision_no,
                        media.position,
                    )
                )
        return results

    async def _outbound_peer(self, account_id: UUID, conversation_id: UUID):  # type: ignore[no-untyped-def]
        async with self._sessions() as session:
            return await PostgresTelegramPeerRepository(
                session,
                new_uuid=self._new_uuid,
            ).resolve_outbound(
                account_id=account_id,
                conversation_id=conversation_id,
            )

    async def start(self, *, deadline: TerminationDeadline | None = None) -> None:
        if self._started:
            return
        if self._closed:
            raise ProductionAppError("APP_RUNTIME_CLOSED")
        if self._settings.bootstrap_maintenance:
            raise ProductionAppError("APP_MAINTENANCE_ACTIVE")
        await self._require_start_admission()
        if not await self._schema_ready():
            raise ProductionAppError("APP_SCHEMA_NOT_READY")
        await self._require_start_admission()
        database_ok, account_ready, restore_ready = await self._database_account_and_restore_ready()
        if not database_ok:
            raise ProductionAppError("APP_DATABASE_UNAVAILABLE")
        if not account_ready:
            raise ProductionAppError("APP_ACCOUNT_NOT_READY")
        if not restore_ready:
            raise ProductionAppError("APP_RESTORE_GATE_CLOSED")
        try:
            await self._ownership.acquire()
            await self._require_start_admission()
            await self._redis.connect(with_arq=True)
            await self._require_start_admission()
            identity = self._settings.deployment.runtime_identity
            self._telethon = TelethonSessionRuntime(
                TelethonSessionSettings(
                    account_id=identity.account_id,
                    telegram_user_id=identity.telegram_user_id,
                    session_path=self._session_path,
                    api_id=int(
                        self._secret_ascii("telegram_api_id", "APP_TELEGRAM_API_ID_INVALID")
                    ),
                    api_hash=SensitiveValue(
                        self._secret_ascii("telegram_api_hash", "APP_TELEGRAM_API_HASH_INVALID")
                    ),
                ),
                resolve_admission=self._peer_resolver(),
                ingest=lambda event: self._ingest_batch((event,), None),
                ingest_batch=self._ingest_batch,
                load_watermark=self._load_account_watermark,
                reconcile_outbound_message_id=self._reconcile_outbound_message_id,
                new_uuid=self._new_uuid,
            )
            await self._require_start_admission()
            await self._telethon_start_call(self._telethon, deadline)
            await self._require_start_admission()
            self._media_ingestion = self._build_media_ingestion()
            self._telegram_gateway = TelethonTelegramGateway(
                self._telethon.client,
                TelethonBoundPeerResolver(self._outbound_peer),
            )
            self._started = True
        except BaseException:
            cleanup_deadline = deadline or self._process_deadline(
                getattr(self, "_managed_process", None)
            )
            try:
                await self._close_started_resources(deadline=cleanup_deadline)
            except BaseException:
                # Never replace the startup/cancellation error with a best-
                # effort cleanup failure.  A force boundary is different: it
                # must propagate so no live dependency is released afterward.
                if self._force_termination_requested():
                    raise
            raise

    async def _start_for_serve(self, process: ManagedProcess) -> bool:  # noqa: PLR0911, PLR0912
        """Start only while admission remains open; handle sticky disk recovery."""

        initial_deadline = self._process_deadline(process)
        wait_for_drain = getattr(process, "wait_for_drain", None)
        if initial_deadline is not None or not callable(wait_for_drain):
            try:
                await self.start(deadline=initial_deadline)
                await self._require_start_admission()
            except ProductionAppError as error:
                if error.code == "APP_DRAINING":
                    return False
                if self._disk_recovery_required:
                    await self._wait_in_disk_blocked_state(process)
                    return False
                raise
            return True

        # Startup itself owns side effects (Redis, Session and catch-up). Race
        # it against the managed drain event so a signal during connect cannot
        # leave the serve task waiting indefinitely before the supervisor sees
        # the task and applies its deadline.
        start_task = asyncio.create_task(self.start(), name="app-startup")
        drain_task = asyncio.create_task(wait_for_drain(), name="app-startup-drain")
        try:
            done, _ = await asyncio.wait(
                (start_task, drain_task), return_when=asyncio.FIRST_COMPLETED
            )
            if start_task in done:
                await self._cancel_start_waiter(drain_task)
                await start_task
            else:
                deadline = self._process_deadline(process)
                if deadline is None:
                    process.request_drain()
                    deadline = self._process_deadline(process)
                settle = getattr(process, "require_settled_before_deadline", None)
                if deadline is not None and callable(settle):
                    await settle(start_task, deadline, cancel=True)
                else:
                    await self._cancel_start_waiter(start_task)
                if start_task.done() and not start_task.cancelled():
                    startup_error = start_task.exception()
                    if startup_error is not None:
                        raise startup_error
                return False
        finally:
            await self._cancel_start_waiter(drain_task)
        try:
            await self._require_start_admission()
        except ProductionAppError as error:
            if error.code == "APP_DRAINING":
                return False
            if self._disk_recovery_required:
                await self._wait_in_disk_blocked_state(process)
                return False
            raise
        return True

    async def _cancel_start_waiter(self, task: asyncio.Task[object]) -> None:
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task

    async def _runtime_tasks_reached_disk_blocked(
        self,
        *,
        telethon_task: asyncio.Task[None],
        scheduler_task: asyncio.Task[None],
        disk_blocked_task: asyncio.Task[bool],
        marker_task: asyncio.Task[None],
    ) -> bool:
        """Wait for the first runtime exit and surface non-disk failures."""

        done, _ = await asyncio.wait(
            (telethon_task, scheduler_task, disk_blocked_task, marker_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disk_blocked_task in done:
            return True
        for task in done:
            if task.cancelled():
                continue
            task_error = task.exception()
            if task_error is not None:
                raise task_error
        return False

    async def serve(self, process: ManagedProcess) -> None:  # noqa: PLR0912
        bound_process = self._managed_process is None
        if bound_process:
            self._managed_process = process
        self._serve_loop_running = True
        telethon_task: asyncio.Task[None] | None = None
        scheduler_task: asyncio.Task[None] | None = None
        disk_blocked_task: asyncio.Task[bool] | None = None
        marker_task: asyncio.Task[None] | None = None
        try:
            if not await self._start_for_serve(process):
                return
            if not await self._operation_admission():
                await self._wait_in_disk_blocked_state(process)
                return
            telegram = self._telegram_gateway
            if telegram is None:
                raise ProductionAppError("APP_TELEGRAM_GATEWAY_UNAVAILABLE")
            context = AppSchedulerContext(
                self._sessions,
                self._model,
                telegram,
                self._redis,
                self._instance_id,
                self._operation_admission,
            )
            telethon_task = asyncio.create_task(
                self._require_telethon().run_until_disconnected(),
                name="app-telethon-session-owner",
            )
            scheduler_task = asyncio.create_task(
                self._scheduler.run(context),
                name="app-durable-scheduler",
            )
            disk_blocked_task = asyncio.create_task(
                self._disk_blocked_event.wait(),
                name="app-disk-blocked-wait",
            )
            marker_task = asyncio.create_task(
                self._runtime_marker_observer.run(),
                name="app-runtime-marker-observer",
            )
            try:
                disk_blocked = await self._runtime_tasks_reached_disk_blocked(
                    telethon_task=telethon_task,
                    scheduler_task=scheduler_task,
                    disk_blocked_task=disk_blocked_task,
                    marker_task=marker_task,
                )
            except BaseException:
                # A child failure would otherwise enter this ``finally`` before
                # ManagedProcess has observed the serve task and established its
                # one-way deadline.  Request drain here so sibling cancellation
                # is bounded by the same process budget.
                process.request_drain()
                raise
            if disk_blocked:
                await self._wait_in_disk_blocked_state(process)
                return
            if not process.draining:
                process.request_drain()
                raise ProductionAppError("APP_RUNTIME_STOPPED")
        finally:
            self._serve_loop_running = False
            self._runtime_marker_observer.stop()
            deadline = getattr(process, "termination_deadline", None)
            for pending_task in (
                marker_task,
                disk_blocked_task,
                scheduler_task,
                telethon_task,
            ):
                if pending_task is None:
                    continue
                if deadline is not None and callable(
                    getattr(process, "require_settled_before_deadline", None)
                ):
                    # The shared seam cancels first and force-exits if a
                    # dependency suppresses cancellation.  Do not suppress its
                    # terminal exception: releasing ownership after that point
                    # would be unsafe.
                    await cast(
                        Callable[..., Awaitable[None]],
                        process.require_settled_before_deadline,
                    )(
                        pending_task,
                        deadline,
                        cancel=True,
                    )
                else:
                    if not pending_task.done():
                        pending_task.cancel()
                    with suppress(BaseException):
                        await pending_task
            if bound_process:
                self._managed_process = None

    async def drain(self, deadline: TerminationDeadline) -> None:
        marker_observer = getattr(self, "_runtime_marker_observer", None)
        if marker_observer is not None:
            marker_observer.stop()
        errors: list[Exception] = []
        try:
            await self._await_cleanup(self._scheduler.drain(deadline), deadline)
        except Exception as error:
            if self._force_termination_requested():
                raise
            errors.append(error)
        runtime = self._telethon
        if runtime is not None:
            try:
                await self._close_telethon(runtime, deadline)
            except Exception as error:
                if self._force_termination_requested():
                    raise
                # A deadline-bounded disconnect that did not complete leaves
                # the Session ownership ambiguous.  Keep the runtime reference
                # and prevent the later generic close path from releasing the
                # account lock; ManagedProcess will force-exit at the boundary.
                self._teardown_incomplete = True
                errors.append(error)
            else:
                self._telethon = None
                self._telegram_gateway = None
                self._media_ingestion = None
                self._teardown_incomplete = False
        if errors and deadline is not None:
            raise errors[0]

    async def _close_started_resources(
        self,
        *,
        deadline: TerminationDeadline | None = None,
    ) -> None:
        marker_observer = getattr(self, "_runtime_marker_observer", None)
        if marker_observer is not None:
            marker_observer.stop()
        runtime = self._telethon
        if runtime is not None:
            try:
                await self._close_telethon(runtime, deadline)
            except Exception as error:
                retention_required = isinstance(error, TelethonSessionRuntimeError) or bool(
                    getattr(runtime, "teardown_pending", False)
                )
                self._started = False
                self._telegram_gateway = None
                self._media_ingestion = None
                if retention_required:
                    self._teardown_incomplete = True
                    raise
                self._telethon = None
                self._teardown_incomplete = False
                # A non-contract test double may report a local cleanup error
                # after it has already become unusable; do not strand the
                # remaining independently-owned dependencies in that case.
            self._telethon = None
            self._telegram_gateway = None
            self._media_ingestion = None
            self._teardown_incomplete = False
        else:
            self._telegram_gateway = None
            self._media_ingestion = None
        self._started = False

        errors: list[Exception] = []

        async def run_cleanup(operation: Callable[[], Awaitable[object]]) -> None:
            try:
                await self._await_cleanup(operation(), deadline)
            except Exception as error:
                if self._force_termination_requested():
                    raise
                errors.append(error)

        if getattr(self._redis, "started", False):
            await run_cleanup(lambda: self._redis.clear_heartbeat(ServiceName.APP))
            await run_cleanup(self._redis.close)
        if getattr(self._ownership, "acquired", False):
            await run_cleanup(self._ownership.release)
        if errors and deadline is not None:
            raise errors[0]

    async def close(self, *, deadline: TerminationDeadline | None = None) -> None:
        if self._closed:
            return
        # Persist the terminal projection while PostgreSQL is still usable.
        # Redis heartbeat removal alone would otherwise leave /server_status
        # reporting the last READY row until the stale-heartbeat window elapses.
        if deadline is None:
            await self._record_stopped()
        else:
            await self._await_cleanup(self._record_stopped(), deadline)
        if deadline is None:
            await self._close_started_resources()
            await self._engine.dispose()
        else:
            await self._close_started_resources(deadline=deadline)
            await self._await_cleanup(self._engine.dispose(), deadline)
        self._closed = True

    async def health(self, observed_at: UtcTimestamp) -> HealthState:
        schema_ok = await self._schema_ready()
        database_ok, account_ready, restore_ready = await self._database_account_and_restore_ready()
        redis_ok = await self._redis.probe() if self._redis.started else False
        if redis_ok:
            try:
                await self._redis.publish_heartbeat(ServiceName.APP)
            except Exception:
                redis_ok = False
        session_owned = await self._ownership.probe() if self._ownership.acquired else False
        telegram_ready = self._telethon is not None and self._telethon.started
        try:
            scheduler_ready = self._scheduler.ready() is True and owner_required_surfaces_composed(
                "app"
            )
            marker_ready = self._runtime_marker_observer.ready
        except Exception:
            scheduler_ready = False
            marker_ready = False
        process = self._managed_process
        disk = await self._filesystem_admission()
        if not disk.operational:
            self._mark_disk_blocked()
        disk_operational = disk.operational and not self._disk_recovery_required
        state = HealthState(
            version=HEALTH_SNAPSHOT_VERSION,
            service=ServiceName.APP,
            observed_at=observed_at,
            heartbeat_at=observed_at,
            process_loop_ok=self._serve_loop_running,
            maintenance=self._settings.bootstrap_maintenance,
            draining=process.draining if process is not None else False,
            required_config_ok=scheduler_ready and marker_ready,
            disk_safety_ok=disk_operational,
            database_ok=database_ok,
            redis_ok=redis_ok,
            schema_ok=schema_ok,
            restore_gate_open=restore_ready,
            account_ready=account_ready,
            session_owned=session_owned,
            telegram_ready=telegram_ready,
        )
        await self._record_service_status(state, observed_at)
        return state

    async def _record_service_status(
        self,
        state: HealthState,
        observed_at: UtcTimestamp,
    ) -> None:
        if not state.database_ok:
            return
        ready = all(
            (
                state.process_loop_ok,
                not state.maintenance,
                not state.draining,
                state.required_config_ok,
                state.disk_safety_ok,
                state.redis_ok,
                state.schema_ok,
                state.restore_gate_open,
                state.account_ready,
                state.session_owned,
                state.telegram_ready,
            )
        )
        if state.draining:
            readiness = ServiceReadiness.DRAINING
            status = ServiceStatusCode.DRAINING
        else:
            readiness = ServiceReadiness.READY if ready else ServiceReadiness.NOT_READY
            status = ServiceStatusCode.READY if ready else self._failure_code(state)
        heartbeat = ServiceHeartbeat(
            instance_id=self._instance_id,
            service_name=ServiceName.APP,
            started_at=self._started_at,
            heartbeat_at=observed_at.value,
            readiness=readiness,
            status_code=status,
            metadata=ServiceStatusMetadata(
                deployment_id=self._settings.deployment.deployment_id,
                source_commit_prefix=self._settings.deployment.source_commit[:12],
                resource_profile=RESOURCE_PROFILE,
                disk_band=("normal" if state.disk_safety_ok else "critical"),
            ),
            last_successful_operation_at=observed_at.value if ready else None,
        )
        with suppress(Exception):
            async with self._sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.APP).heartbeat(heartbeat)

    async def _record_stopped(self) -> None:
        heartbeat = ServiceHeartbeat(
            instance_id=self._instance_id,
            service_name=ServiceName.APP,
            started_at=self._started_at,
            heartbeat_at=datetime.now(UTC),
            readiness=ServiceReadiness.STOPPED,
            status_code=ServiceStatusCode.STOPPED,
            metadata=ServiceStatusMetadata(
                deployment_id=self._settings.deployment.deployment_id,
                source_commit_prefix=self._settings.deployment.source_commit[:12],
                resource_profile=RESOURCE_PROFILE,
                disk_band="unknown",
            ),
        )
        with suppress(Exception):
            async with self._sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.APP).heartbeat(heartbeat)

    @staticmethod
    def _failure_code(state: HealthState) -> ServiceStatusCode:
        checks = (
            (not state.process_loop_ok, ServiceStatusCode.PROCESS_LOOP_FAILED),
            (state.maintenance, ServiceStatusCode.MAINTENANCE_ACTIVE),
            (state.draining, ServiceStatusCode.DRAINING),
            (not state.disk_safety_ok, ServiceStatusCode.DISK_SAFETY_NOT_READY),
            (not state.required_config_ok, ServiceStatusCode.REQUIRED_CONFIG_NOT_READY),
            (not state.database_ok, ServiceStatusCode.DATABASE_UNAVAILABLE),
            (not state.redis_ok, ServiceStatusCode.REDIS_UNAVAILABLE),
            (not state.schema_ok, ServiceStatusCode.SCHEMA_NOT_READY),
            (not state.restore_gate_open, ServiceStatusCode.RESTORE_GATE_CLOSED),
            (state.account_ready is not True, ServiceStatusCode.ACCOUNT_NOT_READY),
            (state.session_owned is not True, ServiceStatusCode.SESSION_NOT_OWNED),
            (state.telegram_ready is not True, ServiceStatusCode.TELEGRAM_NOT_READY),
        )
        return next(code for failed, code in checks if failed)

    async def run_managed(self) -> None:
        process = ManagedProcess(
            service=ServiceName.APP,
            snapshot_path=self._snapshot_path,
            health_provider=self.health,
            drain_hooks=(self.drain,),
        )
        self._managed_process = process
        primary_error: BaseException | None = None
        try:
            await process.run(self.serve)
        except BaseException as error:
            primary_error = error
        finally:
            # ``os._exit`` is the production force boundary.  Injected test
            # terminators raise instead, so explicitly skip all further
            # cleanup when that boundary has been requested; a detached
            # Telethon disconnect may still own the Session file.
            if not process.force_termination_requested:
                deadline = process.termination_deadline
                try:
                    if deadline is None:
                        await self.close()
                    else:
                        await self.close(deadline=deadline)
                except BaseException as cleanup_error:
                    if process.force_termination_requested:
                        raise
                    if primary_error is None:
                        primary_error = cleanup_error
                finally:
                    self._managed_process = None
            else:
                self._managed_process = None
        if primary_error is not None:
            raise primary_error


def run(
    argv: Sequence[str],
    values: Mapping[str, str],
    *,
    model: ModelGateway | None = None,
    scheduler: AppScheduler | None = None,
    stderr: TextIO = sys.stderr,
) -> int:
    """Build the concrete provider/scheduler unless a test seam is injected."""

    if argv:
        stderr.write("APP_ARGUMENT_INVALID\n")
        return 2
    runtime: ProductionAppRuntime | None = None
    try:
        settings = ProductionSettings.load(ProductionProcess.APP, values)
        runtime = ProductionAppRuntime(
            settings,
            settings.load_secrets(),
            model=model,
            scheduler=scheduler,
        )
        asyncio.run(runtime.run_managed())
    except ProductionAppError, ProductionConfigurationError:
        stderr.write("APP_CONFIGURATION_REJECTED\n")
        return 2
    except Exception:
        stderr.write("APP_RUNTIME_FAILED\n")
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:], cast(Mapping[str, str], __import__("os").environ))


if __name__ == "__main__":
    raise SystemExit(main())
