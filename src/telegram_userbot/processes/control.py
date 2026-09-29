"""Production Control Bot, key-only Web App, and health composition root."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, override
from uuid import UUID, uuid7

import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from telegram_userbot.adapters.llm import build_production_capability_probe
from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    PostgresConnectionSettings,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.service_status import (
    RestoreGateRepository,
    ServiceStatusRepository,
)
from telegram_userbot.adapters.queue.redis import (
    RedisConnectionSettings,
    RedisRuntime,
    RedisRuntimeError,
)
from telegram_userbot.adapters.telegram_bot.composition import (
    ControlDispatcherFactory,
    TransactionalModelKeyMutationPort,
)
from telegram_userbot.adapters.telegram_bot.context_control_backend import (
    DurableContextControlBackend,
)
from telegram_userbot.adapters.telegram_bot.durable_control import (
    DurableControlUpdateExecutor,
    PostgresBotOffsetStore,
)
from telegram_userbot.adapters.telegram_bot.http import (
    HttpxTelegramBotSender,
    TelegramBotAPI,
    TelegramBotIdentity,
)
from telegram_userbot.adapters.telegram_bot.model_control_backend import ModelCapabilityProbe
from telegram_userbot.adapters.telegram_bot.polling import ControlBotPoller
from telegram_userbot.adapters.telegram_bot.status_provider import (
    ContentFreeServerStatusProvider,
    PostgresAvailabilityProbe,
    PostgresServiceProjectionSource,
)
from telegram_userbot.adapters.webapp.app import (
    DirectPeerClientResolver,
    FixedWindowClientRateLimiter,
    create_key_web_app,
)
from telegram_userbot.adapters.webapp.auth import TelegramInitDataVerifier
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
    ReadinessPolicy,
    ServiceName,
    disk_safety_ok,
)
from telegram_userbot.platform.health.status import (
    RestoreGateState,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
)
from telegram_userbot.platform.runtime import ManagedProcess, TerminationDeadline
from telegram_userbot.processes.durable_queue_inventory import (
    owner_required_surfaces_composed,
)
from telegram_userbot.processes.runtime_outbox import (
    CONTROL_RUNTIME_MARKER_TOPICS,
    CanonicalRuntimeMarkerConsumer,
    RuntimeMarkerObserver,
)

PREVIEW_MAINTENANCE_INTERVAL_SECONDS = 60.0


class ControlProcessError(RuntimeError):
    """Stable, content-free control composition failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ControlWebServer(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


class PreviewDeletionMaintenance(Protocol):
    async def run_once(self, *, now: datetime) -> int: ...


class DurablePreviewDeletionMaintenance:
    """Run one bounded preview deletion batch under the Bot API owner."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher_factory: ControlDispatcherFactory,
    ) -> None:
        self._sessions = sessions
        self._dispatcher_factory = dispatcher_factory

    async def run_once(self, *, now: datetime) -> int:
        async with self._sessions() as session:
            backend: DurableContextControlBackend = self._dispatcher_factory.context_backend(
                session
            )
            return await backend.delete_due(now=now)


class ManagedUvicornServer(uvicorn.Server):
    """Leave POSIX signal ownership exclusively to :class:`ManagedProcess`."""

    @override
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def create_control_web_server(application: Starlette) -> ControlWebServer:
    config = uvicorn.Config(
        application,
        host="0.0.0.0",  # noqa: S104 - container-only port exposed through the gateway
        port=8080,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
        server_header=False,
        date_header=False,
        limit_concurrency=32,
        backlog=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
    )
    return ManagedUvicornServer(config)


def _process_accepts_new_work(process: ManagedProcess) -> bool:
    """Read admission anew after awaits; a signal may have closed it."""

    return process.accepting_new_work


@dataclass(slots=True)
class _AdmissionGate:
    maintenance: bool
    process: ManagedProcess | None = None

    def __call__(self) -> bool:
        return not self.maintenance and self.process is not None and self.process.accepting_new_work


@dataclass(frozen=True, slots=True)
class ControlComponents:
    settings: ProductionSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: RedisRuntime
    api: TelegramBotAPI
    poller: ControlBotPoller
    web_server: ControlWebServer
    preview_maintenance: PreviewDeletionMaintenance
    account_id: UUID
    admission: _AdmissionGate
    instance_id: UUID
    started_at: datetime


class ProductionControlApplication:
    """Own control-plane dependencies without owning a Telethon Session."""

    def __init__(self, components: ControlComponents) -> None:
        self._components = components
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._web_task: asyncio.Task[None] | None = None
        self._preview_maintenance_task: asyncio.Task[None] | None = None
        self._runtime_marker_task: asyncio.Task[None] | None = None
        self._command_completion_event = asyncio.Event()
        self._last_completed_command_id: UUID | None = None
        self._command_completion_generation = 0
        self._runtime_marker_consumer = CanonicalRuntimeMarkerConsumer(
            sessions=components.sessions,
            topics=CONTROL_RUNTIME_MARKER_TOPICS,
            account_id=components.account_id,
            control_completed=self._command_completed,
        )
        self._runtime_marker_observer = RuntimeMarkerObserver(
            redis=components.redis,
            topics=CONTROL_RUNTIME_MARKER_TOPICS,
            handler=self._runtime_marker_consumer.handle,
            compensate=self._runtime_marker_consumer.compensate,
        )
        self._stop_lock = asyncio.Lock()
        self._closed = False
        self._serve_loop_running = False

    def _managed_process(self) -> object | None:
        return self._components.admission.process

    def _termination_deadline(self) -> TerminationDeadline | None:
        process = self._managed_process()
        deadline = getattr(process, "termination_deadline", None)
        return deadline if isinstance(deadline, TerminationDeadline) else None

    def _force_termination_requested(self) -> bool:
        process = self._managed_process()
        return bool(
            process is not None and getattr(process, "force_termination_requested", False) is True
        )

    @staticmethod
    def _consume_detached_task(task: asyncio.Future[Any]) -> None:
        with suppress(BaseException):
            task.exception()

    @classmethod
    def _detach_task(cls, task: asyncio.Future[Any]) -> None:
        if task.done():
            cls._consume_detached_task(task)
        else:
            task.add_done_callback(cls._consume_detached_task)

    async def _await_bounded(
        self,
        awaitable: Awaitable[Any],
        deadline: TerminationDeadline | None,
    ) -> Any:
        process = self._managed_process()
        await_before_deadline = getattr(process, "await_before_deadline", None)
        if deadline is not None and callable(await_before_deadline):
            return await await_before_deadline(awaitable, deadline)
        if deadline is None:
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        remaining = deadline.remaining_seconds(MonotonicInstant(asyncio.get_running_loop().time()))
        if remaining <= 0:
            if not task.done():
                task.cancel()
            self._detach_task(task)
            force = getattr(process, "force_terminate_after_deadline", None)
            if callable(force):
                force()
            raise ControlProcessError("CONTROL_DRAIN_DEADLINE_EXCEEDED")
        timer = asyncio.create_task(asyncio.sleep(remaining), name="control-drain-deadline")
        done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            timer.cancel()
            with suppress(BaseException):
                await timer
            return await task
        if not task.done():
            task.cancel()
        self._detach_task(task)
        force = getattr(process, "force_terminate_after_deadline", None)
        if callable(force):
            force()
        raise ControlProcessError("CONTROL_DRAIN_DEADLINE_EXCEEDED")

    async def health(self, observed_at: UtcTimestamp) -> HealthState:
        """Observe full readiness while leaving liveness process-local."""

        database_ok, schema_ok, restore_open, redis_ok = await asyncio.gather(
            self._database_available(),
            self._schema_ready(),
            self._restore_gate_open(),
            self._redis_available(),
        )
        if redis_ok:
            try:
                await self._components.redis.publish_heartbeat(ServiceName.CONTROL)
            except RedisRuntimeError:
                redis_ok = False
        state = HealthState(
            version=HEALTH_SNAPSHOT_VERSION,
            service=ServiceName.CONTROL,
            observed_at=observed_at,
            heartbeat_at=observed_at,
            process_loop_ok=self._serve_loop_running,
            maintenance=self._components.settings.bootstrap_maintenance,
            draining=(
                self._components.admission.process is not None
                and self._components.admission.process.draining
            ),
            required_config_ok=(
                owner_required_surfaces_composed("control") and self._runtime_marker_observer.ready
            ),
            disk_safety_ok=_disk_safe(),
            database_ok=database_ok,
            redis_ok=redis_ok,
            schema_ok=schema_ok,
            restore_gate_open=restore_open,
            control_bot_ready=self._components.poller.identity_verified,
            web_api_ready=(
                self._components.web_server.started and not self._components.web_server.should_exit
            ),
        )
        if database_ok and not await self._persist_status(state):
            return replace(
                state,
                database_ok=False,
                schema_ok=False,
                restore_gate_open=False,
            )
        return state

    async def serve(self, process: ManagedProcess) -> None:  # noqa: PLR0912
        self._components.admission.process = process
        self._serve_loop_running = True
        primary_error: BaseException | None = None
        try:
            if not _process_accepts_new_work(process):
                return
            await self._components.redis.connect(with_arq=False)
            # Redis is the first acquired startup resource. A drain requested
            # while connecting must not be followed by HTTP/Bot task creation.
            if not _process_accepts_new_work(process):
                return
            self._web_task = asyncio.create_task(
                self._components.web_server.serve(), name="control-web-api"
            )
            tasks: set[asyncio.Task[object]] = {self._web_task}
            self._runtime_marker_task = asyncio.create_task(
                self._runtime_marker_observer.run(),
                name="control-runtime-marker-observer",
            )
            tasks.add(self._runtime_marker_task)
            self._preview_maintenance_task = asyncio.create_task(
                self._run_preview_maintenance(),
                name="control-preview-deletion-maintenance",
            )
            tasks.add(self._preview_maintenance_task)
            if not self._components.settings.bootstrap_maintenance:
                self._poll_task = asyncio.create_task(
                    self._components.poller.run(self._stop), name="control-bot-poller"
                )
                tasks.add(self._poll_task)
            drain_task = asyncio.create_task(process.wait_for_drain(), name="control-drain-wait")
            tasks.add(drain_task)
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if drain_task in done:
                return
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    raise error  # noqa: TRY301
            raise ControlProcessError("CONTROL_CHILD_EXITED")  # noqa: TRY301
        except BaseException as error:
            primary_error = error
            if not process.draining:
                process.request_drain()
            raise
        finally:
            deadline = self._termination_deadline()
            if not self._force_termination_requested():
                try:
                    await self._request_stop(deadline)
                except BaseException as cleanup_error:
                    if primary_error is None:
                        primary_error = cleanup_error
                self._serve_loop_running = False
                try:
                    await self._close(deadline)
                except BaseException as cleanup_error:
                    if primary_error is None:
                        primary_error = cleanup_error
            else:
                self._serve_loop_running = False
            if primary_error is not None and sys.exc_info()[1] is None:
                raise primary_error

    async def drain(self, deadline: TerminationDeadline) -> None:
        await self._request_stop(deadline)

    async def _request_stop(self, deadline: TerminationDeadline | None = None) -> None:
        async with self._stop_lock:
            self._stop.set()
            self._command_completion_event.set()
            self._runtime_marker_observer.stop()
            self._components.web_server.should_exit = True
            await self._settle_task(self._poll_task, deadline, cancel=True)
            await self._settle_task(self._preview_maintenance_task, deadline, cancel=True)
            await self._settle_task(self._runtime_marker_task, deadline, cancel=True)
            await self._settle_task(self._web_task, deadline)

    async def _settle_task(
        self,
        task: asyncio.Task[Any] | None,
        deadline: TerminationDeadline | None,
        *,
        cancel: bool = False,
    ) -> None:
        """Settle one child without exceeding the process drain budget."""

        if task is None:
            return
        process = self._managed_process()
        require_settled = getattr(process, "require_settled_before_deadline", None)
        if deadline is not None and callable(require_settled):
            await require_settled(task, deadline, cancel=cancel)
            return
        if cancel and not task.done():
            task.cancel()
        try:
            if deadline is None:
                await task
            else:
                remaining = deadline.remaining_seconds(
                    MonotonicInstant(asyncio.get_running_loop().time())
                )
                if remaining <= 0:
                    self._detach_task(task)
                    return
                timer = asyncio.create_task(asyncio.sleep(remaining), name="control-drain-deadline")
                done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    timer.cancel()
                    with suppress(BaseException):
                        await timer
                    return
                if not task.done():
                    task.cancel()
                self._detach_task(task)
                return
        except asyncio.CancelledError:
            return

    def _command_completed(self, command_id: UUID) -> None:
        """Record a content-free local wake after canonical completion validation."""

        if command_id == self._last_completed_command_id:
            return
        self._last_completed_command_id = command_id
        self._command_completion_generation += 1
        self._command_completion_event.set()

    @property
    def command_completion_generation(self) -> int:
        """Return the process-local cursor for completion wake consumers."""

        return self._command_completion_generation

    async def wait_for_command_completion(self, *, after_generation: int) -> tuple[int, UUID]:
        """Wait for a newer canonical completion without losing an early wake.

        Redis can duplicate or coalesce hints, so this is deliberately a local
        generation cursor rather than an event stream. Callers that need command
        details re-read PostgreSQL using the returned content-free identity.
        """

        if (
            type(after_generation) is not int
            or after_generation < 0
            or after_generation > self._command_completion_generation
        ):
            raise ValueError("control completion generation is invalid")
        while self._command_completion_generation <= after_generation:
            if self._stop.is_set() or self._closed:
                raise ControlProcessError("CONTROL_COMPLETION_WAIT_STOPPED")
            # No await occurs between the generation check and clear, so a
            # callback cannot be lost on the single asyncio event loop.
            self._command_completion_event.clear()
            completion_task = asyncio.create_task(self._command_completion_event.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                done, pending = await asyncio.wait(
                    (completion_task, stop_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    await task
            finally:
                for task in (completion_task, stop_task):
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        command_id = self._last_completed_command_id
        if command_id is None:
            raise ControlProcessError("CONTROL_COMPLETION_CURSOR_INVALID")
        return self._command_completion_generation, command_id

    async def _run_preview_maintenance(self) -> None:
        while not self._stop.is_set():
            # Repository leases and backoff retain retry state. A transient
            # Bot/DB failure must not terminate the Control process.
            with suppress(Exception):
                await self._components.preview_maintenance.run_once(now=datetime.now(UTC))
            try:
                async with asyncio.timeout(PREVIEW_MAINTENANCE_INTERVAL_SECONDS):
                    await self._stop.wait()
            except TimeoutError:
                pass

    async def _close(self, deadline: TerminationDeadline | None = None) -> None:
        """Release control dependencies within the process drain budget.

        Cleanup is deliberately retryable: ``_closed`` is set only after every
        dependency has reached its terminal state.  A failed/expired operation
        therefore keeps the composition root available for a later explicit
        retry, while a managed process can force-exit at its single deadline
        boundary before another live dependency is released.
        """

        if self._closed:
            return

        errors: list[BaseException] = []

        async def run_cleanup(
            operation: Callable[[], Awaitable[Any]],
            *,
            ignore_errors: bool = False,
        ) -> None:
            try:
                await self._await_bounded(operation(), deadline)
            except BaseException as error:
                # A force boundary is terminal.  Do not continue into another
                # dependency after a cancellation-resistant operation escaped.
                if self._force_termination_requested():
                    raise
                # Heartbeat deletion is advisory; a stale row is covered by the
                # projection's freshness window and must not prevent the rest of
                # control-plane teardown.  Deadline errors remain terminal even
                # for advisory operations because no later dependency is safe to
                # release once the shared budget is exhausted.
                if (
                    isinstance(error, ControlProcessError)
                    and error.code == "CONTROL_DRAIN_DEADLINE_EXCEEDED"
                ):
                    raise
                if not ignore_errors:
                    errors.append(error)

        await run_cleanup(self._record_stopped)
        if self._components.redis.started:
            await run_cleanup(
                lambda: self._components.redis.clear_heartbeat(ServiceName.CONTROL),
                ignore_errors=True,
            )
            await run_cleanup(self._components.redis.close)
        await run_cleanup(self._components.api.aclose)
        await run_cleanup(self._components.engine.dispose)
        if errors:
            # Preserve the first cleanup diagnosis.  With a live managed
            # process this is observed by ``serve`` and leaves ``_closed``
            # false so an operator can retry safely.
            raise errors[0]
        self._closed = True
        self._components.admission.process = None

    async def _database_available(self) -> bool:
        return await PostgresAvailabilityProbe(self._components.engine).probe()

    async def _schema_ready(self) -> bool:
        return await schema_is_ready(
            self._components.engine,
            EXPECTED_SCHEMA_REVISION,
            policy=DatabaseReadinessPolicy.for_production_process("control"),
        )

    async def _restore_gate_open(self) -> bool:
        try:
            async with self._components.sessions() as session:
                gate = await RestoreGateRepository(session).get(
                    self._components.settings.deployment.deployment_id
                )
        except Exception:
            return False
        else:
            return gate is not None and gate.state is RestoreGateState.OPEN

    async def _redis_available(self) -> bool:
        if not self._components.redis.started:
            return False
        return await self._components.redis.probe()

    async def _persist_status(self, state: HealthState) -> bool:
        heartbeat = _service_heartbeat(self._components, state)
        try:
            async with self._components.sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.CONTROL).heartbeat(heartbeat)
        except Exception:
            return False
        return True

    async def _record_stopped(self) -> None:
        now = datetime.now(UTC)
        heartbeat = ServiceHeartbeat(
            instance_id=self._components.instance_id,
            service_name=ServiceName.CONTROL,
            started_at=self._components.started_at,
            heartbeat_at=now,
            readiness=ServiceReadiness.STOPPED,
            status_code=ServiceStatusCode.STOPPED,
            metadata=_status_metadata(self._components.settings),
        )
        with suppress(Exception):
            async with self._components.sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.CONTROL).heartbeat(heartbeat)


def build_control_application(  # noqa: PLR0913 - production dependencies are injectable
    *,
    settings: ProductionSettings,
    secrets: SecretBundle,
    capability_probe: ModelCapabilityProbe | None = None,
    engine: AsyncEngine | None = None,
    redis: RedisRuntime | None = None,
    api: TelegramBotAPI | None = None,
    web_server_factory: Callable[[Starlette], ControlWebServer] = create_control_web_server,
    now: datetime | None = None,
) -> ProductionControlApplication:
    if settings.process is not ProductionProcess.CONTROL:
        raise ControlProcessError("CONTROL_PROCESS_SETTINGS_INVALID")
    started_at = now or datetime.now(UTC)
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ControlProcessError("CONTROL_PROCESS_CLOCK_INVALID")
    deployment = settings.deployment
    identity_config = deployment.runtime_identity
    database_settings = _database_settings(settings, secrets)
    resolved_engine = engine or create_postgres_engine(database_settings)
    sessions = async_sessionmaker(resolved_engine, expire_on_commit=False)
    resolved_redis = redis or RedisRuntime(
        _redis_settings(settings, secrets), deployment_id=deployment.deployment_id
    )
    token = _text_secret(secrets, "control_bot_token")
    identity = TelegramBotIdentity(
        identity_config.control_bot_user_id,
        identity_config.control_bot_username,
    )
    resolved_api = api or TelegramBotAPI(
        token,
        identity,
        HttpxTelegramBotSender(),
    )
    if resolved_api.identity != identity:
        raise ControlProcessError("CONTROL_BOT_IDENTITY_INVALID")
    keyring_source = secrets.get("credential_master_keyring")
    keyring = parse_credential_keyring(
        keyring_source,
        expected_deployment_id=deployment.deployment_id,
    )
    resolved_capability_probe = capability_probe or build_production_capability_probe(
        sessions,
        keyring=keyring,
    )
    status_provider = ContentFreeServerStatusProvider(
        projections=PostgresServiceProjectionSource(sessions),
        redis=resolved_redis,
        database=PostgresAvailabilityProbe(resolved_engine),
        now=lambda: datetime.now(UTC),
    )
    dispatcher_factory = ControlDispatcherFactory(
        api=resolved_api,
        identity=identity,
        admin_ids=frozenset(identity_config.control_admin_user_ids),
        account_id=identity_config.account_id,
        deployment_id=deployment.deployment_id,
        public_origin=f"https://{deployment.public_host}",
        token_key=keyring.derive_runtime_key(b"control-tokens"),
        capability_probe=resolved_capability_probe,
        status_provider=status_provider,
    )
    instance_id = uuid7()
    executor = DurableControlUpdateExecutor(
        sessions=sessions,
        dispatcher_factory=dispatcher_factory,
        deployment_id=deployment.deployment_id,
        bot_user_id=identity.user_id,
        owner_instance_id=instance_id,
    )
    poller = ControlBotPoller(
        api=resolved_api,
        dispatcher=executor,
        offsets=PostgresBotOffsetStore(
            sessions=sessions,
            deployment_id=deployment.deployment_id,
            bot_user_id=identity.user_id,
        ),
    )
    admission = _AdmissionGate(settings.bootstrap_maintenance)
    web_app = create_key_web_app(
        verifier=TelegramInitDataVerifier(
            bot_token=token,
            allowed_admin_ids=frozenset(identity_config.control_admin_user_ids),
        ),
        mutation_port=TransactionalModelKeyMutationPort(
            sessions=sessions,
            launch_tokens=dispatcher_factory.launch_tokens,
            keyring=keyring,
            deployment_id=deployment.deployment_id,
            process_accepting=admission,
        ),
        public_origin=f"https://{deployment.public_host}",
        client_identity=DirectPeerClientResolver(),
        network_rate_limit=FixedWindowClientRateLimiter(),
    )
    return ProductionControlApplication(
        ControlComponents(
            settings=settings,
            engine=resolved_engine,
            sessions=sessions,
            redis=resolved_redis,
            api=resolved_api,
            poller=poller,
            web_server=web_server_factory(web_app),
            preview_maintenance=DurablePreviewDeletionMaintenance(
                sessions,
                dispatcher_factory,
            ),
            account_id=identity_config.account_id,
            admission=admission,
            instance_id=instance_id,
            started_at=started_at.astimezone(UTC),
        )
    )


def _database_settings(
    settings: ProductionSettings, secrets: SecretBundle
) -> PostgresConnectionSettings:
    database = settings.database
    return PostgresConnectionSettings(
        host=database.host,
        port=database.port,
        database=database.database,
        login_role=database.login_role,
        runtime_role=database.runtime_role,
        password=_text_secret(secrets, database.password_secret_id),
        sslmode=database.sslmode,
        application_name="telegram_userbot_control",
    )


def _redis_settings(settings: ProductionSettings, secrets: SecretBundle) -> RedisConnectionSettings:
    endpoint = settings.redis
    if endpoint is None:
        raise ControlProcessError("CONTROL_REDIS_SETTINGS_MISSING")
    return RedisConnectionSettings(
        host=endpoint.host,
        port=endpoint.port,
        database=0,
        password=_text_secret(secrets, endpoint.password_secret_id),
        max_connections=8,
    )


def _text_secret(secrets: SecretBundle, secret_id: str) -> SensitiveValue[str]:
    try:
        raw = secrets.get(secret_id).reveal_for_use()
        decoded = raw.decode("utf-8")
    except KeyError, UnicodeDecodeError:
        raise ControlProcessError("CONTROL_SECRET_INVALID") from None
    if not decoded or "\x00" in decoded or "\r" in decoded or "\n" in decoded:
        raise ControlProcessError("CONTROL_SECRET_INVALID")
    return SensitiveValue(decoded)


def _disk_safe() -> bool:
    try:
        usage = shutil.disk_usage(Path("/"))
        return disk_safety_ok(total_bytes=usage.total, available_bytes=usage.free)
    except OSError, ValueError:
        return False


def _status_metadata(settings: ProductionSettings) -> ServiceStatusMetadata:
    return ServiceStatusMetadata(
        deployment_id=settings.deployment.deployment_id,
        source_commit_prefix=settings.deployment.source_commit[:12],
        resource_profile=RESOURCE_PROFILE,
    )


def _service_heartbeat(components: ControlComponents, state: HealthState) -> ServiceHeartbeat:
    decision = ReadinessPolicy().readiness(state, now=state.observed_at)
    readiness = ServiceReadiness.READY if decision.healthy else ServiceReadiness.NOT_READY
    status_code = (
        ServiceStatusCode.READY if decision.healthy else ServiceStatusCode(decision.reason.value)
    )
    return ServiceHeartbeat(
        instance_id=components.instance_id,
        service_name=ServiceName.CONTROL,
        started_at=components.started_at,
        heartbeat_at=state.observed_at.value,
        readiness=readiness,
        status_code=status_code,
        metadata=_status_metadata(components.settings),
        last_successful_operation_at=(state.observed_at.value if decision.healthy else None),
    )


async def run_control_application(application: ProductionControlApplication) -> None:
    process = ManagedProcess(
        service=ServiceName.CONTROL,
        snapshot_path=DEFAULT_HEALTH_SNAPSHOT_PATH,
        health_provider=application.health,
        drain_hooks=(application.drain,),
    )
    await process.run(application.serve)


def load_control_settings(
    values: Mapping[str, str] | None = None,
) -> tuple[ProductionSettings, SecretBundle]:
    settings = ProductionSettings.load(
        ProductionProcess.CONTROL,
        os.environ if values is None else values,
    )
    return settings, settings.load_secrets()


def run(
    argv: Sequence[str],
    values: Mapping[str, str],
    *,
    stderr: TextIO = sys.stderr,
) -> int:
    """Build the concrete Control process and run it under managed signals."""

    if argv:
        stderr.write("CONTROL_ARGUMENT_INVALID\n")
        return 2
    try:
        settings, secrets = load_control_settings(values)
        application = build_control_application(
            settings=settings,
            secrets=secrets,
        )
        asyncio.run(run_control_application(application))
    except ControlProcessError, ProductionConfigurationError, CredentialCryptoError:
        stderr.write("CONTROL_CONFIGURATION_REJECTED\n")
        return 2
    except Exception:
        stderr.write("CONTROL_RUNTIME_FAILED\n")
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:], os.environ)


__all__ = [
    "ControlComponents",
    "ControlProcessError",
    "ControlWebServer",
    "DurablePreviewDeletionMaintenance",
    "ManagedUvicornServer",
    "PreviewDeletionMaintenance",
    "ProductionControlApplication",
    "build_control_application",
    "create_control_web_server",
    "load_control_settings",
    "main",
    "run",
    "run_control_application",
]


if __name__ == "__main__":
    raise SystemExit(main())
