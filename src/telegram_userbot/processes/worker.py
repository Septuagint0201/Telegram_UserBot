"""Production arq worker with PostgreSQL-fenced durable execution."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO, cast
from uuid import UUID, uuid7

from arq.worker import Worker
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.embedding_rebuild import EmbeddingRebuildRepository
from telegram_userbot.adapters.persistence.embedding_runtime import EmbeddingRuntimeRepository
from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    PostgresConnectionSettings,
    create_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.memory_pipeline import MemoryPipelineRepository
from telegram_userbot.adapters.persistence.records import JobRecord
from telegram_userbot.adapters.persistence.scheduler_leader import SchedulerLeaderLock
from telegram_userbot.adapters.persistence.schema import (
    accounts,
    background_jobs,
    data_erasure_requests,
)
from telegram_userbot.adapters.persistence.service_status import (
    RestoreGateRepository,
    ServiceStatusRepository,
)
from telegram_userbot.adapters.persistence.worker_runtime import (
    WorkerJobRepository,
    WorkerOutboxRepository,
)
from telegram_userbot.adapters.queue.redis import (
    DurableJobNotifier,
    RedisConnectionSettings,
    RedisRuntime,
    RedisRuntimeError,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION, RESOURCE_PROFILE
from telegram_userbot.platform.config.production import (
    ProductionConfigurationError,
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.crypto import parse_credential_keyring
from telegram_userbot.platform.health import (
    DEFAULT_HEALTH_SNAPSHOT_PATH,
    HEALTH_SNAPSHOT_VERSION,
    HealthState,
    ReadinessPolicy,
    ServiceName,
)
from telegram_userbot.platform.health.disk import DiskAdmission, disk_admission
from telegram_userbot.platform.health.status import (
    RestoreGateState,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
)
from telegram_userbot.platform.runtime import ManagedProcess, TerminationDeadline
from telegram_userbot.platform.time.system import SystemClock
from telegram_userbot.processes.durable_queue_inventory import (
    worker_required_queues_composed,
)
from telegram_userbot.processes.embedding_runtime import EmbeddingExecutor
from telegram_userbot.processes.memory_periods import MemoryPeriodPublisher
from telegram_userbot.processes.memory_pipeline import MemoryPipelineExecutor
from telegram_userbot.processes.model_gateway import PrivateMediaRuntimeImageLoader
from telegram_userbot.processes.proactive_pipeline import ProactivePublisher
from telegram_userbot.processes.runtime_outbox import RuntimeOutboxMarkerPublisher
from telegram_userbot.processes.worker_executors import (
    JobExecutionContext,
    JobExecutionError,
    WorkerExecutorRegistry,
    build_worker_executor_registry,
)

ARQ_QUEUE_NAME = "arq:durable"
JOB_LEASE_SECONDS = 60
JOB_RENEW_SECONDS = 20
OUTBOX_POLL_SECONDS = 0.5
SCHEDULER_RETRY_SECONDS = 5.0
SCHEDULER_TICK_SECONDS = 60.0
_RETRY_CEILINGS = (5, 30, 120, 600)


class WorkerProcessError(RuntimeError):
    """Stable, content-free production worker failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ArqWorkerRuntime(Protocol):
    allow_pick_jobs: bool
    tasks: dict[str, asyncio.Task[Any]]

    async def async_run(self) -> None: ...

    async def close(self) -> None: ...


class ScheduledPublisher(Protocol):
    async def publish(self, *, now: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class WorkerComponents:
    settings: ProductionSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: RedisRuntime
    redis_settings: RedisConnectionSettings
    registry: WorkerExecutorRegistry
    instance_id: UUID
    started_at: datetime
    proactive: ProactivePublisher | None = None


class DurableJobConsumer:
    """Claim exact durable IDs and fence completion against PostgreSQL leases."""

    def __init__(  # noqa: PLR0913 - durable execution seams are explicit
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        registry: WorkerExecutorRegistry,
        owner: UUID,
        cpu_heavy: asyncio.Semaphore,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
        admission: Callable[[], DiskAdmission] | None = None,
    ) -> None:
        self._sessions = sessions
        self._registry = registry
        self._owner = owner
        self._cpu_heavy = cpu_heavy
        self._now = now or (lambda: datetime.now(UTC))
        self._jitter = jitter or (lambda: secrets.randbelow(1_000_000) / 1_000_000)
        self._admission = admission or _disk_admission

    async def consume(self, *, job_id: str, dispatch_generation: int) -> None:
        durable_id = _canonical_uuid(job_id)
        if type(dispatch_generation) is not int or dispatch_generation < 1:
            raise JobExecutionError("WORKER_NOTIFICATION_INVALID", retryable=False)
        try:
            admission = self._admission()
        except Exception:
            admission = disk_admission(total_bytes=1, available_bytes=0)
        async with self._sessions() as session, session.begin():
            job = await WorkerJobRepository(session).claim_notification(
                job_id=durable_id,
                dispatch_generation=dispatch_generation,
                owner=self._owner,
                now=self._now(),
                allow_work=admission.operational,
                allow_proactive=admission.allow_proactive_work,
            )
        if job is None:
            return
        lease_lost = asyncio.Event()
        renew_task = asyncio.create_task(
            self._renew(job, lease_lost),
            name=f"worker-renew-{job.id}",
        )
        failure: JobExecutionError | None = None
        try:
            await self._registry.execute(
                JobExecutionContext(job, self._sessions, lease_lost, self._cpu_heavy)
            )
        except asyncio.CancelledError:
            # A cancelled external operation is not declared safe to replay.  Its
            # lease remains fenced until the recovery scan can classify it.
            raise
        except JobExecutionError as error:
            failure = error
        except Exception:
            failure = JobExecutionError("WORKER_EXECUTION_FAILED", retryable=True)
        finally:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task

        async with self._sessions() as session, session.begin():
            repository = WorkerJobRepository(session)
            if failure is None:
                completed = await repository.succeed(job=job, owner=self._owner, now=self._now())
                if not completed:
                    raise JobExecutionError("WORKER_JOB_FENCE_LOST", retryable=True)
                return
            transitioned = await repository.fail_or_retry(
                job=job,
                owner=self._owner,
                now=self._now(),
                error_code=failure.code,
                retryable=failure.retryable,
                retry_delay=self._retry_delay(job),
            )
        if transitioned is None:
            raise JobExecutionError("WORKER_JOB_FENCE_LOST", retryable=True)

    async def _renew(self, job: JobRecord, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(JOB_RENEW_SECONDS)
            try:
                async with self._sessions() as session, session.begin():
                    renewed = await WorkerJobRepository(session).renew(
                        job=job,
                        owner=self._owner,
                        now=self._now(),
                        lease_duration=timedelta(seconds=JOB_LEASE_SECONDS),
                    )
            except Exception:
                renewed = False
            if not renewed:
                lease_lost.set()
                return

    def _retry_delay(self, job: JobRecord) -> timedelta:
        unit = self._jitter()
        if not 0 <= unit < 1:
            raise WorkerProcessError("WORKER_JITTER_INVALID")
        ceiling = _RETRY_CEILINGS[min(max(job.attempt_count - 1, 0), len(_RETRY_CEILINGS) - 1)]
        return timedelta(seconds=unit * ceiling)


async def wake_durable_job(
    context: dict[str, Any],
    job_id: str,
    dispatch_generation: int,
) -> None:
    consumer = context.get("durable_consumer")
    if not isinstance(consumer, DurableJobConsumer):
        raise WorkerProcessError("WORKER_ARQ_CONTEXT_INVALID")
    await consumer.consume(job_id=job_id, dispatch_generation=dispatch_generation)


class DurableOutboxPublisher:
    """Publish due worker wakeups without owning unrelated outbox topics."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        notifier: DurableJobNotifier,
        now: Callable[[], datetime] | None = None,
        admission: Callable[[], DiskAdmission] | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._now = now or (lambda: datetime.now(UTC))
        self._admission = admission or _disk_admission
        self._stop = asyncio.Event()

    async def publish_once(self, *, limit: int = 100) -> int:
        # Locks are released before Redis I/O. Duplicate relays are safe because
        # DurableJobNotifier uses a generation-bound arq job ID.
        try:
            admission = self._admission()
        except Exception:
            admission = disk_admission(total_bytes=1, available_bytes=0)
        async with self._sessions() as session, session.begin():
            records = await WorkerOutboxRepository(session).due_wakeups(
                now=self._now(),
                limit=limit,
                allow_work=admission.operational,
                allow_proactive=admission.allow_proactive_work,
            )
        published = 0
        for record in records:
            try:
                await self._notifier.publish(record)
            except TypeError, ValueError:
                # Payload validation happens before the Redis adapter's error
                # boundary. Persist a stable terminal diagnosis and continue
                # relaying unrelated durable wakeups.
                async with self._sessions() as session, session.begin():
                    await WorkerOutboxRepository(session).record_failure(
                        outbox_id=record.id,
                        error_code="DURABLE_JOB_INVALID",
                    )
            except RedisRuntimeError:
                async with self._sessions() as session, session.begin():
                    await WorkerOutboxRepository(session).record_failure(
                        outbox_id=record.id,
                        error_code="REDIS_JOB_ENQUEUE_FAILED",
                    )
            else:
                async with self._sessions() as session, session.begin():
                    marked = await WorkerOutboxRepository(session).mark_published(
                        outbox_id=record.id,
                        now=self._now(),
                    )
                published += int(marked)
        return published

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.publish_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: S110 - PostgreSQL rows retain retry state
                # A transient database or adapter failure must not terminate the
                # worker's durable relay. The next poll retries the facts.
                pass
            try:
                async with asyncio.timeout(OUTBOX_POLL_SECONDS):
                    await self._stop.wait()
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


class DurableCompensationPublisher:
    """Publish replayable recovery work from PostgreSQL facts only."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        batch_limit: int = 100,
        admission: Callable[[], DiskAdmission] | None = None,
        timezone: str = "UTC",
    ) -> None:
        if not 1 <= batch_limit <= 1000:
            raise ValueError("worker compensation batch is invalid")
        self._sessions = sessions
        self._batch_limit = batch_limit
        self._admission = admission or _disk_admission
        self._periods = MemoryPeriodPublisher(sessions, timezone=timezone)

    async def publish(self, *, now: datetime) -> int:
        try:
            admission = self._admission()
        except Exception:
            admission = disk_admission(total_bytes=1, available_bytes=0)
        async with self._sessions() as session, session.begin():
            repository = WorkerJobRepository(session)
            recovered = await repository.recover_expired(now=now, limit=self._batch_limit)
            enqueued = await self._enqueue_erasure_requests(session, now=now)
            if admission.operational and hasattr(session, "execute"):
                enqueued += await EmbeddingRebuildRepository(session).ensure_spaces(now=now)
                enqueued += await EmbeddingRebuildRepository(session).advance(
                    now=now, limit=self._batch_limit
                )
                enqueued += await MemoryPipelineRepository(session).enqueue_pending(
                    now=now, limit=self._batch_limit
                )
                enqueued += await EmbeddingRuntimeRepository(session).enqueue_pending(
                    now=now, limit=self._batch_limit
                )
            rebuilt = await repository.rebuild_due_notifications(
                now=now,
                limit=self._batch_limit,
                allow_work=admission.operational,
                allow_proactive=admission.allow_proactive_work,
            )
        if admission.operational and hasattr(session, "execute"):
            enqueued += await self._periods.publish(now=now)
        return recovered + enqueued + rebuilt

    async def _enqueue_erasure_requests(self, session: AsyncSession, *, now: datetime) -> int:
        # Keep the publisher seam compatible with the narrow repository fakes used
        # by scheduler unit tests; real AsyncSession always exposes execute().
        if not hasattr(session, "execute"):
            return 0
        rows = (
            await session.execute(
                select(
                    data_erasure_requests.c.id,
                    data_erasure_requests.c.account_id,
                )
                .outerjoin(background_jobs, background_jobs.c.id == data_erasure_requests.c.id)
                .where(
                    or_(
                        background_jobs.c.id.is_(None),
                        and_(
                            background_jobs.c.state == "succeeded",
                            background_jobs.c.updated_at <= now - timedelta(minutes=1),
                        ),
                    ),
                    or_(
                        and_(
                            data_erasure_requests.c.state.in_(
                                (
                                    "requested",
                                    "quiescing",
                                    "redacting",
                                    "media_cleanup",
                                    "derived_cleanup",
                                )
                            ),
                        ),
                        and_(
                            data_erasure_requests.c.state == "failed",
                            data_erasure_requests.c.last_error_code
                            == "ERASURE_SCOPE_PIPELINE_UNAVAILABLE",
                        ),
                    ),
                )
                .order_by(data_erasure_requests.c.created_at, data_erasure_requests.c.id)
                .limit(self._batch_limit)
                .with_for_update(of=data_erasure_requests, skip_locked=True)
            )
        ).all()
        enqueued = 0
        for request_id, account_id in rows:
            key = hashlib.sha256(b"erasure-reconcile:" + request_id.bytes).digest()
            result = cast(
                CursorResult[Any],
                await session.execute(
                    postgresql_insert(background_jobs)
                    .values(
                        id=request_id,
                        account_id=account_id,
                        queue_name="worker",
                        job_type="memory.reconcile_erasure",
                        idempotency_key=key,
                        payload_schema_version=1,
                        payload={"request_id": str(request_id)},
                        available_at=now,
                        priority=100,
                        max_attempts=20,
                    )
                    .on_conflict_do_update(
                        constraint="uq_background_jobs_idempotency",
                        set_={
                            "state": "pending",
                            "attempt_count": 0,
                            "available_at": now,
                            "completed_at": None,
                            "last_error_code": None,
                            "version": background_jobs.c.version + 1,
                            "dispatch_generation": background_jobs.c.dispatch_generation + 1,
                            "updated_at": now,
                        },
                        where=and_(
                            background_jobs.c.state == "succeeded",
                            background_jobs.c.updated_at <= now - timedelta(minutes=1),
                        ),
                    )
                    .returning(background_jobs.c.id)
                ),
            )
            enqueued += int(result.scalar_one_or_none() is not None)
        return enqueued


class WorkerSchedulerLeader:
    """Only the session-lock owner publishes periodic durable ticks."""

    def __init__(
        self,
        *,
        lock: SchedulerLeaderLock,
        publishers: Sequence[ScheduledPublisher],
        now: Callable[[], datetime] | None = None,
        tick_seconds: float = SCHEDULER_TICK_SECONDS,
    ) -> None:
        if not publishers or not 1 <= tick_seconds <= 3600:
            raise ValueError("worker scheduler configuration is invalid")
        self._lock = lock
        self._publishers = tuple(publishers)
        self._now = now or (lambda: datetime.now(UTC))
        self._tick_seconds = tick_seconds
        self._stop = asyncio.Event()

    @property
    def leader(self) -> bool:
        return self._lock.acquired

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._lock.acquired:
                    try:
                        await self._lock.try_acquire()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # PostgreSQL leadership acquisition is retriable.  A
                        # database outage must not turn into a worker restart
                        # loop while durable jobs remain safely unclaimed.
                        await self._wait(SCHEDULER_RETRY_SECONDS)
                        continue
                if self._lock.acquired:
                    if not await self._lock.probe():
                        await self._wait(SCHEDULER_RETRY_SECONDS)
                        continue
                    now = self._now()
                    for publisher in self._publishers:
                        try:
                            await publisher.publish(now=now)
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: S112 - durable rows retry next tick
                            # A transient failure in one scheduled projection must
                            # not terminate the scheduler (or the whole worker).
                            # The durable rows remain the retry source for the next
                            # tick, and other publishers still get their turn.
                            continue
                    await self._wait(self._tick_seconds)
                else:
                    await self._wait(SCHEDULER_RETRY_SECONDS)
        finally:
            await self._lock.release()

    async def stop(self) -> None:
        self._stop.set()
        # ``run`` owns the leadership lifetime and releases the advisory lock
        # in its ``finally`` block.  Releasing it here can race a publisher
        # after a successful probe, allowing that tick to continue after a
        # different worker acquires leadership.  Waking ``_wait`` is enough to
        # stop an idle scheduler; an in-flight durable publish finishes while
        # the lock is still held and then exits through ``finally``.

    async def _wait(self, seconds: float) -> None:
        try:
            async with asyncio.timeout(seconds):
                await self._stop.wait()
        except TimeoutError:
            pass


class ProductionWorkerApplication:
    """Own arq, outbox recovery, scheduler leadership, and worker health."""

    def __init__(
        self,
        components: WorkerComponents,
        *,
        snapshot_path: Path = DEFAULT_HEALTH_SNAPSHOT_PATH,
        worker_factory: Callable[[DurableJobConsumer], ArqWorkerRuntime] | None = None,
        monotonic_clock: Callable[[], MonotonicInstant] | None = None,
    ) -> None:
        if components.settings.process is not ProductionProcess.WORKER:
            raise WorkerProcessError("WORKER_PROCESS_SETTINGS_INVALID")
        self._components = components
        self._snapshot_path = snapshot_path
        self._worker_factory = worker_factory or self._build_arq_worker
        self._monotonic_clock = monotonic_clock or SystemClock().monotonic_now
        self._arq_worker: ArqWorkerRuntime | None = None
        self._outbox: DurableOutboxPublisher | None = None
        self._scheduler: WorkerSchedulerLeader | None = None
        self._runtime_outbox: RuntimeOutboxMarkerPublisher | None = None
        self._serve_running = False
        self._consumer_running = False
        self._closed = False
        self._stop_lock = asyncio.Lock()
        self._background_tasks: tuple[asyncio.Task[Any], ...] = ()
        self._managed_process: ManagedProcess | None = None

    def _termination_deadline(
        self,
        process: object | None = None,
    ) -> TerminationDeadline | None:
        candidate = process if process is not None else getattr(self, "_managed_process", None)
        deadline = getattr(candidate, "termination_deadline", None)
        return deadline if isinstance(deadline, TerminationDeadline) else None

    def _force_termination_requested(self, process: object | None = None) -> bool:
        candidate = process if process is not None else getattr(self, "_managed_process", None)
        return bool(getattr(candidate, "force_termination_requested", False) is True)

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
        awaitable: Any,
        deadline: TerminationDeadline | None,
    ) -> Any:
        process = getattr(self, "_managed_process", None)
        await_before_deadline = getattr(process, "await_before_deadline", None)
        if deadline is not None and callable(await_before_deadline):
            return await await_before_deadline(awaitable, deadline)
        if deadline is None:
            return await awaitable
        remaining_method = getattr(deadline, "remaining_seconds", None)
        if not callable(remaining_method):
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        remaining = cast(float, remaining_method(self._monotonic_clock()))
        if remaining <= 0:
            if not task.done():
                task.cancel()
            self._detach_task(task)
            raise WorkerProcessError("WORKER_DRAIN_DEADLINE_EXCEEDED")
        timer = asyncio.create_task(asyncio.sleep(remaining), name="worker-drain-deadline")
        try:
            done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if not timer.done():
                timer.cancel()
            with suppress(BaseException):
                await timer
            if not task.done():
                task.cancel()
            self._detach_task(task)
            raise
        if task in done:
            if not timer.done():
                timer.cancel()
            with suppress(BaseException):
                await timer
            return await task
        if not task.done():
            task.cancel()
        self._detach_task(task)
        raise WorkerProcessError("WORKER_DRAIN_DEADLINE_EXCEEDED")

    async def _settle_task(
        self,
        task: asyncio.Task[Any] | None,
        deadline: TerminationDeadline | None,
        *,
        cancel: bool,
    ) -> None:
        if task is None or task.done():
            if task is not None and task.done():
                self._consume_detached_task(task)
            return
        process = getattr(self, "_managed_process", None)
        require_settled = getattr(process, "require_settled_before_deadline", None)
        if deadline is not None and callable(require_settled):
            await require_settled(task, deadline, cancel=cancel)
            return
        if cancel:
            task.cancel()
        if deadline is None:
            with suppress(BaseException):
                await task
            return
        await self._await_bounded(task, deadline)

    def _build_arq_worker(self, consumer: DurableJobConsumer) -> ArqWorkerRuntime:
        settings = self._components.settings
        return cast(
            ArqWorkerRuntime,
            Worker(
                cast(Sequence[Any], (wake_durable_job,)),
                queue_name=ARQ_QUEUE_NAME,
                redis_settings=self._components.redis_settings.arq_settings(),
                handle_signals=False,
                max_jobs=settings.worker_concurrency,
                max_tries=1,
                job_timeout=300,
                keep_result=0,
                poll_delay=0.5,
                health_check_interval=10,
                log_results=False,
                retry_jobs=False,
                ctx={"durable_consumer": consumer},
            ),
        )

    async def serve(self, process: ManagedProcess) -> None:  # noqa: PLR0912, PLR0915
        bound_process = self._managed_process is None
        if bound_process:
            self._managed_process = process
        self._serve_running = True
        tasks: list[asyncio.Task[Any]] = []
        drain_task: asyncio.Task[None] | None = None
        primary_error: BaseException | None = None
        try:
            await self._start()
            self._require_start_admission()
            if (
                self._arq_worker is None
                or self._outbox is None
                or self._scheduler is None
                or self._runtime_outbox is None
            ):
                raise WorkerProcessError("WORKER_START_INCOMPLETE")  # noqa: TRY301
            tasks = [
                asyncio.create_task(self._arq_worker.async_run(), name="worker-arq-consumer"),
                asyncio.create_task(self._outbox.run(), name="worker-outbox-publisher"),
                asyncio.create_task(self._scheduler.run(), name="worker-scheduler-leader"),
                asyncio.create_task(
                    self._runtime_outbox.run(),
                    name="worker-runtime-outbox-relay",
                ),
            ]
            self._background_tasks = tuple(tasks)
            self._consumer_running = True
            drain_task = asyncio.create_task(process.wait_for_drain(), name="worker-drain-wait")
            done, _pending = await asyncio.wait(
                (*tasks, drain_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain_task in done:
                return
            for task in tasks:
                if task in done and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        raise error  # noqa: TRY301
            raise WorkerProcessError("WORKER_CHILD_EXITED")  # noqa: TRY301
        except BaseException as error:
            primary_error = error
            request_drain = getattr(process, "request_drain", None)
            if callable(request_drain) and not getattr(process, "draining", False):
                request_drain()
        finally:
            self._consumer_running = False
            self._serve_running = False
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
            deadline = self._termination_deadline(process)
            if not self._force_termination_requested(process):
                try:
                    await self._request_stop(deadline)
                except BaseException as cleanup_error:
                    if self._force_termination_requested(process):
                        raise
                    if primary_error is None:
                        primary_error = cleanup_error
                try:
                    await self._close_dependencies(deadline)
                except BaseException as cleanup_error:
                    if self._force_termination_requested(process):
                        raise
                    if primary_error is None:
                        primary_error = cleanup_error
            if bound_process:
                self._managed_process = None
        if primary_error is not None:
            raise primary_error

    def _require_start_admission(self) -> None:
        """Reject startup continuation once ManagedProcess has closed admission."""

        process = self._managed_process
        if process is not None and not process.accepting_new_work:
            raise WorkerProcessError("WORKER_DRAINING")

    async def _rollback_start(self, deadline: TerminationDeadline | None = None) -> None:
        """Release only resources acquired by a startup that did not complete."""

        self._arq_worker = None
        self._outbox = None
        self._scheduler = None
        self._runtime_outbox = None
        if self._components.redis.started:
            with suppress(RedisRuntimeError):
                await self._await_bounded(self._components.redis.close(), deadline)

    async def _start(self) -> None:
        if self._components.settings.bootstrap_maintenance:
            raise WorkerProcessError("WORKER_MAINTENANCE_ACTIVE")
        self._require_start_admission()
        if not await self._schema_ready():
            raise WorkerProcessError("WORKER_SCHEMA_NOT_READY")
        self._require_start_admission()
        database_ok, account_ok, restore_open = await self._database_account_restore()
        if not database_ok:
            raise WorkerProcessError("WORKER_DATABASE_UNAVAILABLE")
        if not account_ok:
            raise WorkerProcessError("WORKER_ACCOUNT_NOT_READY")
        if not restore_open:
            raise WorkerProcessError("WORKER_RESTORE_GATE_CLOSED")
        self._require_start_admission()
        try:
            await self._components.redis.connect(with_arq=True)
            self._require_start_admission()
            consumer = DurableJobConsumer(
                sessions=self._components.sessions,
                registry=self._components.registry,
                owner=self._components.instance_id,
                cpu_heavy=asyncio.Semaphore(self._components.settings.image_stage_concurrency),
            )
            self._arq_worker = self._worker_factory(consumer)
            self._outbox = DurableOutboxPublisher(
                sessions=self._components.sessions,
                notifier=self._components.redis.durable_job_notifier(queue_name=ARQ_QUEUE_NAME),
            )
            compensation = DurableCompensationPublisher(
                self._components.sessions, timezone=self._components.settings.deployment.timezone
            )
            proactive = getattr(self._components, "proactive", None)
            self._scheduler = WorkerSchedulerLeader(
                lock=SchedulerLeaderLock(
                    self._components.engine,
                    deployment_id=self._components.settings.deployment.deployment_id,
                ),
                publishers=(compensation, proactive) if proactive is not None else (compensation,),
            )
            self._runtime_outbox = RuntimeOutboxMarkerPublisher(
                sessions=self._components.sessions,
                redis=self._components.redis,
                owns_relay=lambda: self._scheduler is not None and self._scheduler.leader,
            )
        except BaseException:
            try:
                await self._rollback_start(self._termination_deadline())
            except BaseException:
                # Keep the startup or cancellation error as the operation's
                # primary result.  A ManagedProcess deadline breach is the
                # one exception: its force-termination boundary must escape so
                # no dependency is released while a detached task is alive.
                if self._force_termination_requested():
                    raise
            raise

    async def drain(self, deadline: TerminationDeadline) -> None:
        await self._request_stop(deadline)

    async def _request_stop(  # noqa: PLR0912
        self, deadline: TerminationDeadline | None
    ) -> None:
        await self._await_bounded(self._stop_lock.acquire(), deadline)
        try:
            if self._outbox is not None:
                self._outbox.stop()
            runtime_outbox = getattr(self, "_runtime_outbox", None)
            if runtime_outbox is not None:
                runtime_outbox.stop()
            if self._scheduler is not None:
                await self._await_bounded(self._scheduler.stop(), deadline)
            worker = self._arq_worker
            if worker is not None:
                worker.allow_pick_jobs = False
                while any(not task.done() for task in worker.tasks.values()):
                    if deadline is None or deadline.expired(self._monotonic_clock()):
                        break
                    remaining_method = getattr(deadline, "remaining_seconds", None)
                    remaining = (
                        cast(float, remaining_method(self._monotonic_clock()))
                        if callable(remaining_method)
                        else 0.05
                    )
                    await asyncio.sleep(min(0.05, remaining))
                try:
                    await self._await_bounded(worker.close(), deadline)
                except RedisRuntimeError:
                    raise
                except WorkerProcessError:
                    raise
                except Exception:
                    if deadline is not None:
                        raise
                else:
                    self._arq_worker = None
            # Stop signals and the arq close above are issued before cancellation.
            # Awaiting every child here prevents a database/Redis operation from
            # surviving into dependency disposal.
            for task in self._background_tasks:
                try:
                    await self._settle_task(task, deadline, cancel=True)
                except BaseException:
                    if self._force_termination_requested():
                        raise
                    if deadline is not None:
                        raise
            self._background_tasks = ()
        finally:
            self._stop_lock.release()

    async def _close_dependencies(
        self,
        deadline: TerminationDeadline | None = None,
    ) -> None:
        if self._closed:
            return
        await self._await_bounded(self._record_stopped(), deadline)
        release_errors: list[BaseException] = []
        if self._components.redis.started:
            try:
                await self._await_bounded(
                    self._components.redis.clear_heartbeat(ServiceName.WORKER), deadline
                )
            except BaseException as error:
                if self._force_termination_requested():
                    raise
                if isinstance(error, WorkerProcessError) and deadline is not None:
                    raise
                # Heartbeat removal is advisory.  The persisted STOPPED row and
                # freshness window cover a stale Redis key, so it must not block
                # release of the actual Redis/engine resources.
            try:
                await self._await_bounded(self._components.redis.close(), deadline)
            except BaseException as error:
                if self._force_termination_requested():
                    raise
                if isinstance(error, WorkerProcessError) and deadline is not None:
                    raise
                release_errors.append(error)
        try:
            await self._await_bounded(self._components.engine.dispose(), deadline)
        except BaseException as error:
            if self._force_termination_requested():
                raise
            if isinstance(error, WorkerProcessError) and deadline is not None:
                raise
            release_errors.append(error)
        self._closed = not release_errors
        if deadline is not None and release_errors:
            raise release_errors[0]

    async def health(self, observed_at: UtcTimestamp) -> HealthState:
        database_ok, account_ok, restore_open = await self._database_account_restore()
        schema_ok = await self._schema_ready()
        redis_ok = await self._components.redis.probe() if self._components.redis.started else False
        if redis_ok:
            try:
                await self._components.redis.publish_heartbeat(ServiceName.WORKER)
            except RedisRuntimeError:
                redis_ok = False
        admission = _disk_admission()
        process = self._managed_process
        state = HealthState(
            version=HEALTH_SNAPSHOT_VERSION,
            service=ServiceName.WORKER,
            observed_at=observed_at,
            heartbeat_at=observed_at,
            process_loop_ok=self._serve_running,
            maintenance=self._components.settings.bootstrap_maintenance,
            # Disk blocking is an admission state, not a process-exit request.
            # Keeping the worker alive lets compensation and cleanup recover
            # durable facts without an ``unless-stopped`` restart loop.
            draining=process.draining if process is not None else False,
            required_config_ok=(
                bool(self._components.registry.job_types) and worker_required_queues_composed()
            ),
            disk_safety_ok=admission.operational,
            database_ok=database_ok and account_ok,
            redis_ok=redis_ok,
            schema_ok=schema_ok,
            restore_gate_open=restore_open,
            consumer_ready=self._consumer_running,
        )
        if database_ok:
            await self._persist_status(state, admission.status_metadata_band)
        return state

    async def _schema_ready(self) -> bool:
        return await schema_is_ready(
            self._components.engine,
            EXPECTED_SCHEMA_REVISION,
            policy=DatabaseReadinessPolicy.for_production_process("worker"),
        )

    async def _database_account_restore(self) -> tuple[bool, bool, bool]:
        identity = self._components.settings.deployment.runtime_identity
        try:
            async with self._components.sessions() as session:
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
                    self._components.settings.deployment.deployment_id
                )
        except Exception:
            return False, False, False
        account_ok = (
            account is not None
            and account["telegram_user_id"] == identity.telegram_user_id
            and account["status"] == "active"
        )
        restore_open = (
            gate is not None
            and gate.account_id == identity.account_id
            and gate.state is RestoreGateState.OPEN
        )
        return True, account_ok, restore_open

    async def _persist_status(self, state: HealthState, disk_band: str) -> None:
        decision = ReadinessPolicy().readiness(state, now=state.observed_at)
        heartbeat = ServiceHeartbeat(
            instance_id=self._components.instance_id,
            service_name=ServiceName.WORKER,
            started_at=self._components.started_at,
            heartbeat_at=state.observed_at.value,
            readiness=ServiceReadiness.READY if decision.healthy else ServiceReadiness.NOT_READY,
            status_code=(
                ServiceStatusCode.READY
                if decision.healthy
                else ServiceStatusCode(decision.reason.value)
            ),
            metadata=_status_metadata(self._components.settings, disk_band=disk_band),
            last_successful_operation_at=state.observed_at.value if decision.healthy else None,
        )
        try:
            async with self._components.sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.WORKER).heartbeat(heartbeat)
        except Exception:
            return

    async def _record_stopped(self) -> None:
        heartbeat = ServiceHeartbeat(
            instance_id=self._components.instance_id,
            service_name=ServiceName.WORKER,
            started_at=self._components.started_at,
            heartbeat_at=datetime.now(UTC),
            readiness=ServiceReadiness.STOPPED,
            status_code=ServiceStatusCode.STOPPED,
            metadata=_status_metadata(self._components.settings, disk_band="unknown"),
        )
        with suppress(Exception):
            async with self._components.sessions() as session, session.begin():
                await ServiceStatusRepository(session, ServiceName.WORKER).heartbeat(heartbeat)

    async def run_managed(self) -> None:
        process = ManagedProcess(
            service=ServiceName.WORKER,
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
            deadline = self._termination_deadline(process)
            if not self._force_termination_requested(process):
                try:
                    await self._request_stop(deadline)
                except BaseException as cleanup_error:
                    if self._force_termination_requested(process):
                        raise
                    if primary_error is None:
                        primary_error = cleanup_error
                try:
                    await self._close_dependencies(deadline)
                except BaseException as cleanup_error:
                    if self._force_termination_requested(process):
                        raise
                    if primary_error is None:
                        primary_error = cleanup_error
            self._managed_process = None
        if primary_error is not None:
            raise primary_error


def build_worker_application(  # noqa: PLR0913 - production seams are explicit
    *,
    settings: ProductionSettings,
    secrets_bundle: SecretBundle,
    registry: WorkerExecutorRegistry | None = None,
    engine: AsyncEngine | None = None,
    redis: RedisRuntime | None = None,
    now: datetime | None = None,
) -> ProductionWorkerApplication:
    if settings.process is not ProductionProcess.WORKER:
        raise WorkerProcessError("WORKER_PROCESS_SETTINGS_INVALID")
    started_at = now or datetime.now(UTC)
    database_settings = _database_settings(settings, secrets_bundle)
    resolved_engine = engine or create_postgres_engine(database_settings)
    sessions = async_sessionmaker(resolved_engine, expire_on_commit=False)
    redis_settings = _redis_settings(settings, secrets_bundle)
    resolved_redis = redis or RedisRuntime(
        redis_settings,
        deployment_id=settings.deployment.deployment_id,
    )
    erasure_secret = _bytes_secret(secrets_bundle, "erasure_hmac_key", minimum=32)
    resolved_registry = registry or build_worker_executor_registry(
        erasure_scope_secret=erasure_secret,
        embedding_executor=EmbeddingExecutor(
            keyring=parse_credential_keyring(
                secrets_bundle.get("credential_master_keyring"),
                expected_deployment_id=settings.deployment.deployment_id,
            )
        ),
        memory_executor=MemoryPipelineExecutor(
            keyring=parse_credential_keyring(
                secrets_bundle.get("credential_master_keyring"),
                expected_deployment_id=settings.deployment.deployment_id,
            ),
            fingerprint_secret=erasure_secret,
            image_loader=PrivateMediaRuntimeImageLoader(Path("/var/lib/telegram-userbot/media")),
        ),
    )
    return ProductionWorkerApplication(
        WorkerComponents(
            settings=settings,
            engine=resolved_engine,
            sessions=sessions,
            redis=resolved_redis,
            redis_settings=redis_settings,
            registry=resolved_registry,
            instance_id=uuid7(),
            started_at=started_at.astimezone(UTC),
            proactive=ProactivePublisher(
                sessions=sessions,
                keyring=parse_credential_keyring(
                    secrets_bundle.get("credential_master_keyring"),
                    expected_deployment_id=settings.deployment.deployment_id,
                ),
                secret=erasure_secret,
                admission=_disk_admission,
            ),
        )
    )


def _database_settings(
    settings: ProductionSettings,
    secrets_bundle: SecretBundle,
) -> PostgresConnectionSettings:
    endpoint = settings.database
    return PostgresConnectionSettings(
        host=endpoint.host,
        port=endpoint.port,
        database=endpoint.database,
        login_role=endpoint.login_role,
        runtime_role=endpoint.runtime_role,
        password=_text_secret(secrets_bundle, endpoint.password_secret_id),
        sslmode=endpoint.sslmode,
        application_name="telegram_userbot_worker",
    )


def _redis_settings(
    settings: ProductionSettings,
    secrets_bundle: SecretBundle,
) -> RedisConnectionSettings:
    endpoint = settings.redis
    if endpoint is None:
        raise WorkerProcessError("WORKER_REDIS_SETTINGS_MISSING")
    return RedisConnectionSettings(
        host=endpoint.host,
        port=endpoint.port,
        database=0,
        password=_text_secret(secrets_bundle, endpoint.password_secret_id),
        max_connections=8,
    )


def _text_secret(secrets_bundle: SecretBundle, secret_id: str) -> SensitiveValue[str]:
    try:
        decoded = secrets_bundle.get(secret_id).reveal_for_use().decode("utf-8")
    except KeyError, UnicodeDecodeError:
        raise WorkerProcessError("WORKER_SECRET_INVALID") from None
    if not decoded or any(character in decoded for character in "\x00\r\n"):
        raise WorkerProcessError("WORKER_SECRET_INVALID")
    return SensitiveValue(decoded)


def _bytes_secret(
    secrets_bundle: SecretBundle,
    secret_id: str,
    *,
    minimum: int,
) -> SensitiveValue[bytes]:
    try:
        value = secrets_bundle.get(secret_id).reveal_for_use()
    except KeyError:
        raise WorkerProcessError("WORKER_SECRET_INVALID") from None
    if not isinstance(value, bytes) or len(value) < minimum:
        raise WorkerProcessError("WORKER_SECRET_INVALID")
    return SensitiveValue(value)


def _canonical_uuid(raw: str) -> UUID:
    if not isinstance(raw, str):
        raise JobExecutionError("WORKER_NOTIFICATION_INVALID", retryable=False)
    try:
        value = UUID(raw)
    except ValueError:
        raise JobExecutionError("WORKER_NOTIFICATION_INVALID", retryable=False) from None
    if value.int == 0 or str(value) != raw:
        raise JobExecutionError("WORKER_NOTIFICATION_INVALID", retryable=False)
    return value


def _disk_admission() -> DiskAdmission:
    try:
        usage = shutil.disk_usage(Path("/"))
    except OSError:
        return disk_admission(total_bytes=1, available_bytes=0)
    return disk_admission(total_bytes=usage.total, available_bytes=usage.free)


def _status_metadata(settings: ProductionSettings, *, disk_band: str) -> ServiceStatusMetadata:
    return ServiceStatusMetadata(
        deployment_id=settings.deployment.deployment_id,
        source_commit_prefix=settings.deployment.source_commit[:12],
        resource_profile=RESOURCE_PROFILE,
        disk_band=disk_band,
    )


def run(
    argv: Sequence[str],
    values: Mapping[str, str],
    *,
    stderr: TextIO = sys.stderr,
) -> int:
    if argv:
        stderr.write("WORKER_ARGUMENT_INVALID\n")
        return 2
    try:
        settings = ProductionSettings.load(ProductionProcess.WORKER, values)
        application = build_worker_application(
            settings=settings,
            secrets_bundle=settings.load_secrets(),
        )
        asyncio.run(application.run_managed())
    except ProductionConfigurationError, WorkerProcessError, ValueError:
        stderr.write("WORKER_CONFIGURATION_REJECTED\n")
        return 2
    except Exception:
        stderr.write("WORKER_RUNTIME_FAILED\n")
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:], cast(Mapping[str, str], os.environ))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARQ_QUEUE_NAME",
    "DurableCompensationPublisher",
    "DurableJobConsumer",
    "DurableOutboxPublisher",
    "ProductionWorkerApplication",
    "WorkerComponents",
    "WorkerProcessError",
    "WorkerSchedulerLeader",
    "build_worker_application",
    "main",
    "run",
    "wake_durable_job",
]
