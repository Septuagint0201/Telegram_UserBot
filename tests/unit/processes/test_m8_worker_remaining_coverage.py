"""Additional fake-first coverage for worker fail-closed and lifecycle edges."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.processes.worker as worker_module
from telegram_userbot.adapters.persistence.records import JobRecord, JobState, OutboxRecord
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.config.production import (
    DatabaseEndpoint,
    ProductionProcess,
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
)
from telegram_userbot.platform.health.status import ServiceStatusMetadata
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.worker import (
    DurableCompensationPublisher,
    DurableJobConsumer,
    DurableOutboxPublisher,
    ProductionWorkerApplication,
    WorkerComponents,
    WorkerProcessError,
    WorkerSchedulerLeader,
)
from telegram_userbot.processes.worker_executors import JobExecutionContext, JobExecutionError

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
OWNER = UUID("01900000-0000-7000-8000-000000000001")
JOB_ID = UUID("01900000-0000-7000-8000-000000000002")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000003")


def _job(*, attempt_count: int = 1) -> JobRecord:
    return JobRecord(
        id=JOB_ID,
        account_id=ACCOUNT_ID,
        queue_name="worker",
        job_type="memory.refresh_completed_turn",
        state=JobState.LEASED,
        priority=0,
        payload={},
        attempt_count=attempt_count,
        max_attempts=5,
        available_at=NOW,
        lease_owner=OWNER,
        lease_expires_at=NOW + timedelta(seconds=60),
        version=2,
        fencing_token=1,
        dispatch_generation=1,
    )


def _empty_session() -> object:
    return object()


def _empty_arq_settings() -> object:
    return object()


class _Session:
    def __init__(self, state: object) -> None:
        self.state = state

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Session:
        return self


class _Sessions:
    def __init__(self, state: object) -> None:
        self.state = state

    def __call__(self) -> _Session:
        return _Session(self.state)


class _ConsumerState:
    def __init__(self, claimed: JobRecord | None) -> None:
        self.claimed = claimed
        self.claim_calls: list[dict[str, object]] = []
        self.succeed_calls = 0
        self.fail_calls: list[dict[str, object]] = []
        self.renew_results: list[object] = []
        self.fail_result: JobState | None = JobState.RETRY_WAIT


class _ConsumerRepository:
    def __init__(self, session: _Session) -> None:
        self.state = cast(_ConsumerState, session.state)

    async def claim_notification(self, **kwargs: object) -> JobRecord | None:
        self.state.claim_calls.append(kwargs)
        return self.state.claimed

    async def succeed(self, **_kwargs: object) -> bool:
        self.state.succeed_calls += 1
        return True

    async def fail_or_retry(self, **kwargs: object) -> JobState | None:
        self.state.fail_calls.append(kwargs)
        return self.state.fail_result

    async def renew(self, **_kwargs: object) -> bool:
        result = self.state.renew_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return cast(bool, result)


class _Registry:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    async def execute(self, _context: JobExecutionContext) -> None:
        if self.error is not None:
            raise self.error


def _consumer(
    state: _ConsumerState,
    registry: _Registry,
    *,
    admission: Any = None,
    jitter: Any = None,
) -> DurableJobConsumer:
    return DurableJobConsumer(
        sessions=cast(async_sessionmaker[AsyncSession], _Sessions(state)),
        registry=cast(Any, registry),
        owner=OWNER,
        cpu_heavy=asyncio.Semaphore(1),
        now=lambda: NOW,
        admission=cast(
            Any,
            admission or (lambda: SimpleNamespace(operational=True, allow_proactive_work=True)),
        ),
        jitter=jitter or (lambda: 0.25),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consumer_falls_back_to_closed_admission_and_ignores_missing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(None)

    def broken_admission() -> Any:
        raise OSError("synthetic disk probe failure")

    monkeypatch.setattr(worker_module, "WorkerJobRepository", _ConsumerRepository)
    await _consumer(state, _Registry(), admission=broken_admission).consume(
        job_id=str(JOB_ID), dispatch_generation=1
    )

    assert state.claim_calls[0]["allow_work"] is False
    assert state.claim_calls[0]["allow_proactive"] is False
    assert state.succeed_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consumer_does_not_finalize_a_cancelled_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(_job())
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _ConsumerRepository)

    with pytest.raises(asyncio.CancelledError):
        await _consumer(state, _Registry(asyncio.CancelledError())).consume(
            job_id=str(JOB_ID), dispatch_generation=1
        )

    assert state.succeed_calls == 0
    assert state.fail_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consumer_reports_lost_fence_when_failure_transition_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(_job())
    state.fail_result = None
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _ConsumerRepository)

    with pytest.raises(JobExecutionError, match="WORKER_JOB_FENCE_LOST"):
        await _consumer(
            state,
            _Registry(JobExecutionError("SYNTHETIC", retryable=True)),
        ).consume(job_id=str(JOB_ID), dispatch_generation=1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consumer_renew_sets_lease_lost_after_repository_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(_job())
    state.renew_results = [RuntimeError("synthetic database failure")]
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _ConsumerRepository)
    monkeypatch.setattr(worker_module, "JOB_RENEW_SECONDS", 0)
    consumer = _consumer(state, _Registry())
    lease_lost = asyncio.Event()

    await consumer._renew(_job(), lease_lost)

    assert lease_lost.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consumer_renew_loops_after_success_then_stops_on_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(_job())
    state.renew_results = [True, False]
    monkeypatch.setattr(worker_module, "WorkerJobRepository", _ConsumerRepository)
    monkeypatch.setattr(worker_module, "JOB_RENEW_SECONDS", 0)
    consumer = _consumer(state, _Registry())
    lease_lost = asyncio.Event()

    await consumer._renew(_job(), lease_lost)

    assert lease_lost.is_set()
    assert state.renew_results == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attempt_count", "jitter", "seconds"),
    [(0, 0.5, 2.5), (2, 0.5, 15), (99, 0.5, 300)],
)
def test_consumer_retry_delay_uses_bounded_exponential_ceilings(
    attempt_count: int,
    jitter: float,
    seconds: float,
) -> None:
    consumer = _consumer(_ConsumerState(None), _Registry(), jitter=lambda: jitter)
    assert consumer._retry_delay(_job(attempt_count=attempt_count)) == timedelta(seconds=seconds)


@pytest.mark.unit
def test_consumer_rejects_out_of_range_retry_jitter() -> None:
    for jitter in (-0.01, 1.0):
        consumer = _consumer(_ConsumerState(None), _Registry(), jitter=lambda jitter=jitter: jitter)
        with pytest.raises(WorkerProcessError, match="WORKER_JITTER_INVALID"):
            consumer._retry_delay(_job())


class _OutboxState:
    def __init__(self) -> None:
        self.records: tuple[OutboxRecord, ...] = ()
        self.due_calls: list[dict[str, object]] = []


class _OutboxRepository:
    def __init__(self, session: _Session) -> None:
        self.state = cast(_OutboxState, session.state)

    async def due_wakeups(self, **kwargs: object) -> tuple[OutboxRecord, ...]:
        self.state.due_calls.append(kwargs)
        return self.state.records


class _Notifier:
    async def publish(self, _record: OutboxRecord) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_publisher_closes_admission_when_disk_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _OutboxState()

    def broken_admission() -> Any:
        raise OSError("synthetic disk probe failure")

    monkeypatch.setattr(worker_module, "WorkerOutboxRepository", _OutboxRepository)
    publisher = DurableOutboxPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _Sessions(state)),
        notifier=cast(Any, _Notifier()),
        now=lambda: NOW,
        admission=broken_admission,
    )

    assert await publisher.publish_once() == 0
    assert state.due_calls[0]["allow_work"] is False
    assert state.due_calls[0]["allow_proactive"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_run_propagates_cancellation() -> None:
    publisher = DurableOutboxPublisher(
        sessions=cast(async_sessionmaker[AsyncSession], _empty_session),
        notifier=cast(Any, _Notifier()),
    )
    cast(Any, publisher).publish_once = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await publisher.run()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compensation_publisher_closes_admission_when_disk_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ConsumerState(None)
    observed: dict[str, object] = {}

    class Repository:
        def __init__(self, _session: _Session) -> None:
            return None

        async def recover_expired(self, **kwargs: object) -> int:
            observed["recover"] = kwargs
            return 2

        async def rebuild_due_notifications(self, **kwargs: object) -> int:
            observed["rebuild"] = kwargs
            return 3

    def broken_admission() -> Any:
        raise OSError("synthetic disk probe failure")

    monkeypatch.setattr(worker_module, "WorkerJobRepository", Repository)
    publisher = DurableCompensationPublisher(
        cast(async_sessionmaker[AsyncSession], _Sessions(state)),
        admission=broken_admission,
    )

    assert await publisher.publish(now=NOW) == 5
    assert cast(dict[str, object], observed["rebuild"])["allow_work"] is False
    assert cast(dict[str, object], observed["rebuild"])["allow_proactive"] is False


@pytest.mark.unit
def test_compensation_publisher_rejects_invalid_batch_limits() -> None:
    for limit in (0, 1001):
        with pytest.raises(ValueError, match="worker compensation batch is invalid"):
            DurableCompensationPublisher(
                cast(Any, _Sessions(_ConsumerState(None))), batch_limit=limit
            )


@pytest.mark.unit
def test_worker_scheduler_rejects_empty_publishers_and_invalid_tick() -> None:
    lock = cast(Any, SimpleNamespace(acquired=False))
    with pytest.raises(ValueError, match="worker scheduler configuration is invalid"):
        WorkerSchedulerLeader(lock=lock, publishers=(), tick_seconds=1)
    with pytest.raises(ValueError, match="worker scheduler configuration is invalid"):
        WorkerSchedulerLeader(lock=lock, publishers=cast(Any, (object(),)), tick_seconds=0)
    with pytest.raises(ValueError, match="worker scheduler configuration is invalid"):
        WorkerSchedulerLeader(lock=lock, publishers=cast(Any, (object(),)), tick_seconds=3601)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_waits_when_not_leader() -> None:
    class Lock:
        acquired = False
        releases = 0

        async def try_acquire(self) -> bool:
            return False

        async def probe(self) -> bool:
            raise AssertionError("probe must not run without leadership")

        async def release(self) -> None:
            self.releases += 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(lock=cast(Any, lock), publishers=(cast(Any, object()),))
    waits: list[float] = []

    async def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        scheduler._stop.set()

    cast(Any, scheduler)._wait = stop_after_wait
    await scheduler.run()

    assert waits == [worker_module.SCHEDULER_RETRY_SECONDS]
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_retries_transient_leader_acquisition_failure() -> None:
    class Lock:
        acquired = False
        releases = 0

        async def try_acquire(self) -> bool:
            raise RuntimeError("synthetic PostgreSQL transient")

        async def probe(self) -> bool:
            raise AssertionError("probe must not run without leadership")

        async def release(self) -> None:
            self.releases += 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(lock=cast(Any, lock), publishers=(cast(Any, object()),))
    waits: list[float] = []

    async def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        scheduler._stop.set()

    cast(Any, scheduler)._wait = stop_after_wait
    await scheduler.run()

    assert waits == [worker_module.SCHEDULER_RETRY_SECONDS]
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_retries_after_lost_probe() -> None:
    class Lock:
        acquired = False
        releases = 0

        async def try_acquire(self) -> bool:
            self.acquired = True
            return True

        async def probe(self) -> bool:
            return False

        async def release(self) -> None:
            self.releases += 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(lock=cast(Any, lock), publishers=(cast(Any, object()),))
    waits: list[float] = []

    async def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        scheduler._stop.set()

    cast(Any, scheduler)._wait = stop_after_wait
    await scheduler.run()

    assert waits == [worker_module.SCHEDULER_RETRY_SECONDS]
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_isolates_publisher_failure_and_continues_tick() -> None:
    class Lock:
        acquired = False
        releases = 0

        async def try_acquire(self) -> bool:
            self.acquired = True
            return True

        async def probe(self) -> bool:
            return True

        async def release(self) -> None:
            self.acquired = False
            self.releases += 1

    calls: list[str] = []

    class BrokenPublisher:
        async def publish(self, *, now: datetime) -> int:
            assert now == NOW
            calls.append("broken")
            raise RuntimeError("synthetic transient publisher failure")

    class HealthyPublisher:
        async def publish(self, *, now: datetime) -> int:
            assert now == NOW
            calls.append("healthy")
            return 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(
        lock=cast(Any, lock),
        publishers=(BrokenPublisher(), HealthyPublisher()),
        now=lambda: NOW,
        tick_seconds=1.0,
    )
    waits: list[float] = []

    async def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        scheduler._stop.set()

    cast(Any, scheduler)._wait = stop_after_wait
    await scheduler.run()

    assert calls == ["broken", "healthy"]
    assert waits == [1.0]
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_holds_leadership_until_inflight_publish_finishes() -> None:
    class Lock:
        acquired: bool = False
        releases: int = 0

        async def try_acquire(self) -> bool:
            self.acquired = True
            return True

        async def probe(self) -> bool:
            return True

        async def release(self) -> None:
            self.acquired = False
            self.releases += 1

    publish_started = asyncio.Event()
    finish_publish = asyncio.Event()

    class Publisher:
        async def publish(self, *, now: datetime) -> int:
            assert now == NOW
            publish_started.set()
            await finish_publish.wait()
            assert lock.acquired
            return 1

    lock = Lock()

    def is_acquired() -> bool:
        # Keep the assertion dynamic across the scheduler's await boundary.
        return lock.acquired

    scheduler = WorkerSchedulerLeader(
        lock=cast(Any, lock),
        publishers=(Publisher(),),
        now=lambda: NOW,
        tick_seconds=1.0,
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(publish_started.wait(), timeout=1)

    await scheduler.stop()
    assert is_acquired()
    assert lock.releases == 0

    finish_publish.set()
    await asyncio.wait_for(task, timeout=1)
    assert not is_acquired()
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_skips_acquire_when_already_leader() -> None:
    class Lock:
        acquired: bool = True
        releases: int = 0

        async def try_acquire(self) -> bool:
            raise AssertionError("already-leader lock must not reacquire")

        async def probe(self) -> bool:
            return False

        async def release(self) -> None:
            self.releases += 1

    lock = Lock()
    scheduler = WorkerSchedulerLeader(lock=cast(Any, lock), publishers=cast(Any, (object(),)))

    async def stop_after_wait(_seconds: float) -> None:
        scheduler._stop.set()

    cast(Any, scheduler)._wait = stop_after_wait
    await scheduler.run()
    assert lock.releases == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_scheduler_wait_timeout_is_ignored() -> None:
    scheduler = WorkerSchedulerLeader(
        lock=cast(Any, SimpleNamespace(acquired=False)),
        publishers=(cast(Any, object()),),
    )
    await scheduler._wait(0)


def _settings(*, process: ProductionProcess = ProductionProcess.WORKER) -> Any:
    return SimpleNamespace(
        process=process,
        bootstrap_maintenance=False,
        worker_concurrency=2,
        image_stage_concurrency=1,
        deployment=SimpleNamespace(
            deployment_id="synthetic-deployment",
            source_commit="a" * 40,
            runtime_identity=SimpleNamespace(account_id=ACCOUNT_ID, telegram_user_id=42),
        ),
    )


def _runtime(*, process: ProductionProcess = ProductionProcess.WORKER) -> Any:
    components = SimpleNamespace(
        settings=_settings(process=process),
        engine=cast(AsyncEngine, SimpleNamespace(dispose=AsyncMock())),
        sessions=cast(async_sessionmaker[AsyncSession], _empty_session),
        redis=cast(Any, SimpleNamespace(started=False)),
        redis_settings=cast(Any, SimpleNamespace(arq_settings=_empty_arq_settings)),
        registry=SimpleNamespace(job_types={"memory.refresh_completed_turn"}),
        instance_id=uuid7(),
        started_at=NOW,
    )
    return ProductionWorkerApplication(cast(WorkerComponents, components))


@pytest.mark.unit
def test_worker_application_constructor_and_arq_builder_use_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    captured: dict[str, object] = {}

    class Arq:
        def __init__(self) -> None:
            self.allow_pick_jobs = True
            self.tasks: dict[str, asyncio.Task[Any]] = {}

    def worker_factory(*args: object, **kwargs: object) -> Arq:
        captured["args"] = args
        captured.update(kwargs)
        return Arq()

    monkeypatch.setattr(worker_module, "Worker", worker_factory)
    result = runtime._build_arq_worker(cast(Any, object()))

    assert isinstance(result, Arq)
    assert captured["queue_name"] == worker_module.ARQ_QUEUE_NAME
    assert captured["max_jobs"] == 2
    assert captured["handle_signals"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_serve_rejects_incomplete_start_and_preserves_prebound_process() -> None:
    runtime = _runtime()
    process = cast(Any, SimpleNamespace(accepting_new_work=True, draining=False))
    runtime._start = AsyncMock()
    runtime._request_stop = AsyncMock()
    runtime._close_dependencies = AsyncMock()

    with pytest.raises(WorkerProcessError, match="WORKER_START_INCOMPLETE"):
        await runtime.serve(cast(ManagedProcess, process))
    assert runtime._managed_process is None

    prebound = cast(Any, SimpleNamespace(accepting_new_work=True, draining=False))
    runtime = _runtime()
    runtime._managed_process = cast(ManagedProcess, prebound)
    runtime._start = AsyncMock(side_effect=RuntimeError("synthetic start failure"))
    runtime._request_stop = AsyncMock()
    runtime._close_dependencies = AsyncMock()

    with pytest.raises(RuntimeError, match="synthetic start failure"):
        await runtime.serve(cast(ManagedProcess, object()))
    assert runtime._managed_process is prebound


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_serve_propagates_child_exception() -> None:
    runtime = _runtime()

    class Child:
        def __init__(self) -> None:
            self.allow_pick_jobs = True
            self.tasks: dict[str, asyncio.Task[Any]] = {}

        async def async_run(self) -> None:
            raise RuntimeError("synthetic child failure")

        async def close(self) -> None:
            return None

    class Publisher:
        async def run(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class Scheduler:
        async def run(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def start() -> None:
        runtime._arq_worker = Child()
        runtime._outbox = Publisher()
        runtime._scheduler = Scheduler()
        runtime._runtime_outbox = Publisher()

    runtime._start = start
    runtime._request_stop = AsyncMock()
    runtime._close_dependencies = AsyncMock()

    async def wait_for_drain() -> None:
        await asyncio.Event().wait()

    process = cast(
        Any,
        SimpleNamespace(
            accepting_new_work=True,
            draining=False,
            wait_for_drain=wait_for_drain,
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic child failure"):
        await runtime.serve(cast(ManagedProcess, process))


@pytest.mark.unit
def test_worker_admission_and_rollback_handle_closed_process_and_stopped_redis() -> None:
    runtime = _runtime()
    runtime._managed_process = cast(
        ManagedProcess,
        SimpleNamespace(accepting_new_work=False),
    )
    with pytest.raises(WorkerProcessError, match="WORKER_DRAINING"):
        runtime._require_start_admission()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_start_rolls_back_when_admission_closes_after_redis_connect() -> None:
    runtime = _runtime()

    class Redis:
        started = False
        close_calls = 0

        async def connect(self, *, with_arq: bool) -> None:
            assert with_arq
            self.started = True
            runtime._managed_process.accepting_new_work = False

        async def close(self) -> None:
            self.close_calls += 1
            self.started = False

        def durable_job_notifier(self, *, queue_name: str) -> object:
            return object()

    redis = Redis()
    runtime._components.redis = redis
    runtime._managed_process = cast(Any, SimpleNamespace(accepting_new_work=True))
    runtime._schema_ready = AsyncMock(return_value=True)
    runtime._database_account_restore = AsyncMock(return_value=(True, True, True))

    with pytest.raises(WorkerProcessError, match="WORKER_DRAINING"):
        await runtime._start()
    assert redis.close_calls == 1
    assert runtime._arq_worker is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_rollback_and_request_stop_cover_already_stopped_resources() -> None:
    runtime = _runtime()
    await runtime._rollback_start()

    class RuntimeOutbox:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1

    runtime_outbox = RuntimeOutbox()
    runtime._runtime_outbox = runtime_outbox
    await runtime._request_stop(None)
    assert runtime_outbox.stop_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_request_stop_breaks_on_expired_deadline_and_skips_done_tasks() -> None:
    runtime = _runtime()

    class Worker:
        allow_pick_jobs = True
        tasks: dict[str, asyncio.Task[Any]]

        def __init__(self, task: asyncio.Task[Any]) -> None:
            self.tasks = {"job": task}
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    pending = asyncio.create_task(asyncio.Event().wait())
    worker = Worker(pending)
    runtime._arq_worker = worker
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    runtime._background_tasks = (done_task,)
    deadline = SimpleNamespace(expired=lambda _now: True)

    await runtime._request_stop(cast(Any, deadline))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert worker.close_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_request_stop_waits_once_before_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()

    class Worker:
        allow_pick_jobs = True
        tasks: dict[str, asyncio.Task[Any]]

        def __init__(self, task: asyncio.Task[Any]) -> None:
            self.tasks = {"job": task}

        async def close(self) -> None:
            return None

    pending = asyncio.create_task(asyncio.Event().wait())
    runtime._arq_worker = Worker(pending)

    class Deadline:
        calls = 0

        def expired(self, _now: object) -> bool:
            self.calls += 1
            return self.calls > 1

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cast(Any, worker_module).asyncio, "sleep", no_sleep)
    await runtime._request_stop(cast(Any, Deadline()))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_schema_wrapper_delegates_with_production_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    schema_check = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_module, "schema_is_ready", schema_check)

    assert await runtime._schema_ready()
    schema_check.assert_awaited_once()


@pytest.mark.unit
def test_worker_build_rejects_non_worker_process_before_secrets() -> None:
    with pytest.raises(WorkerProcessError, match="WORKER_PROCESS_SETTINGS_INVALID"):
        worker_module.build_worker_application(
            settings=_settings(process=ProductionProcess.CONTROL),
            secrets_bundle=SecretBundle(()),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_request_stop_handles_empty_children_and_drain_forwards_deadline() -> None:
    runtime = _runtime()
    deadline = cast(Any, SimpleNamespace(expired=lambda _now: True))
    await runtime.drain(deadline)
    await runtime._request_stop(None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_close_dependencies_is_idempotent_and_handles_unstarted_redis() -> None:
    runtime = _runtime()
    runtime._record_stopped = AsyncMock()
    await runtime._close_dependencies()
    await runtime._close_dependencies()
    runtime._record_stopped.assert_awaited_once()
    assert runtime._closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_health_skips_status_persistence_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._database_account_restore = AsyncMock(return_value=(False, False, False))
    runtime._schema_ready = AsyncMock(return_value=False)
    runtime._persist_status = AsyncMock()
    monkeypatch.setattr(worker_module, "_disk_admission", lambda: SimpleNamespace(operational=True))
    monkeypatch.setattr(worker_module, "worker_required_queues_composed", lambda: False)

    state = await runtime.health(UtcTimestamp(NOW))

    assert not state.database_ok
    assert not state.required_config_ok
    runtime._persist_status.assert_not_awaited()


class _MappingResult:
    def __init__(self, row: object) -> None:
        self.row = row

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> object:
        return self.row


class _DatabaseSession:
    def __init__(self, row: object, *, error: bool = False) -> None:
        self.row = row
        self.error = error

    async def __aenter__(self) -> _DatabaseSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _query: object) -> _MappingResult:
        if self.error:
            raise RuntimeError("synthetic database error")
        return _MappingResult(self.row)


class _GateRepository:
    gate: object = None

    def __init__(self, _session: _DatabaseSession) -> None:
        return None

    async def get(self, _deployment_id: str) -> object:
        return self.gate


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "gate", "account_ok", "restore_open"),
    [
        (
            {"telegram_user_id": 42, "status": "active"},
            SimpleNamespace(account_id=ACCOUNT_ID, state=RestoreGateState.OPEN),
            True,
            True,
        ),
        (
            {"telegram_user_id": 99, "status": "active"},
            SimpleNamespace(account_id=ACCOUNT_ID, state=RestoreGateState.OPEN),
            False,
            True,
        ),
        (None, None, False, False),
    ],
)
async def test_worker_database_account_restore_projects_account_and_gate(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
    gate: object,
    account_ok: bool,
    restore_open: bool,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(worker_module, "RestoreGateRepository", _GateRepository)
    _GateRepository.gate = gate
    runtime._components.sessions = cast(
        async_sessionmaker[AsyncSession], lambda: _DatabaseSession(row)
    )

    assert await runtime._database_account_restore() == (True, account_ok, restore_open)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_database_account_restore_fails_closed_on_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(worker_module, "RestoreGateRepository", _GateRepository)
    runtime._components.sessions = cast(
        async_sessionmaker[AsyncSession], lambda: _DatabaseSession(None, error=True)
    )
    assert await runtime._database_account_restore() == (False, False, False)


class _StatusSession:
    async def __aenter__(self) -> _StatusSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _StatusSession:
        return self


class _StatusRepository:
    heartbeats: ClassVar[list[ServiceHeartbeat]] = []
    error = False

    def __init__(self, _session: _StatusSession, _service: ServiceName) -> None:
        return None

    async def heartbeat(self, value: ServiceHeartbeat) -> None:
        if self.error:
            raise RuntimeError("synthetic status persistence failure")
        self.heartbeats.append(value)


def _health_state(*, healthy: bool) -> HealthState:
    return HealthState(
        version=HEALTH_SNAPSHOT_VERSION,
        service=ServiceName.WORKER,
        observed_at=UtcTimestamp(NOW),
        heartbeat_at=UtcTimestamp(NOW),
        process_loop_ok=True,
        maintenance=False,
        draining=False,
        required_config_ok=healthy,
        disk_safety_ok=True,
        database_ok=healthy,
        redis_ok=True,
        schema_ok=True,
        restore_gate_open=healthy,
        consumer_ready=True,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_persist_status_writes_ready_and_not_ready_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._components.sessions = cast(async_sessionmaker[AsyncSession], _StatusSession)
    _StatusRepository.heartbeats = []
    _StatusRepository.error = False
    monkeypatch.setattr(worker_module, "ServiceStatusRepository", _StatusRepository)

    await runtime._persist_status(_health_state(healthy=True), "normal")
    await runtime._persist_status(_health_state(healthy=False), "critical")

    assert [item.readiness for item in _StatusRepository.heartbeats] == [
        ServiceReadiness.READY,
        ServiceReadiness.NOT_READY,
    ]
    assert (
        _StatusRepository.heartbeats[1].status_code is ServiceStatusCode.REQUIRED_CONFIG_NOT_READY
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_persist_status_and_record_stopped_suppress_storage_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._components.started_at = datetime.now(UTC) - timedelta(seconds=1)
    runtime._components.sessions = cast(async_sessionmaker[AsyncSession], _StatusSession)
    _StatusRepository.error = True
    monkeypatch.setattr(worker_module, "ServiceStatusRepository", _StatusRepository)

    await runtime._persist_status(_health_state(healthy=True), "normal")
    await runtime._record_stopped()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_run_managed_cleans_up_after_managed_process_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime._request_stop = AsyncMock()
    runtime._close_dependencies = AsyncMock()

    class Process:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def run(self, _serve: object) -> None:
            raise RuntimeError("synthetic managed failure")

    monkeypatch.setattr(worker_module, "ManagedProcess", Process)
    with pytest.raises(RuntimeError, match="synthetic managed failure"):
        await runtime.run_managed()
    assert runtime._managed_process is None
    runtime._request_stop.assert_awaited_once_with(None)
    runtime._close_dependencies.assert_awaited_once()


@pytest.mark.unit
def test_worker_build_application_constructs_default_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.database = DatabaseEndpoint(
        "db", 5432, "app", "login", "runtime", "db_password", "require"
    )
    settings.redis = RedisEndpoint("redis", 6379, "redis_password")
    bundle = SecretBundle(
        (
            ("erasure_hmac_key", SensitiveValue(b"x" * 32)),
            ("db_password", SensitiveValue(b"db-secret")),
            ("redis_password", SensitiveValue(b"redis-secret")),
            (
                "credential_master_keyring",
                SensitiveValue(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "deployment_id": "synthetic-deployment",
                            "active_key_version": 1,
                            "keys": {"1": base64.b64encode(b"x" * 32).decode()},
                        }
                    ).encode()
                ),
            ),
        )
    )
    engine = cast(AsyncEngine, object())
    monkeypatch.setattr(worker_module, "create_postgres_engine", lambda _settings: engine)
    app = worker_module.build_worker_application(settings=settings, secrets_bundle=bundle, now=NOW)

    assert app._components.engine is engine
    assert "embedding.compute" in app._components.registry.job_types
    assert "memory.generate" in app._components.registry.job_types
    assert app._components.started_at == NOW


@pytest.mark.unit
def test_worker_settings_helpers_validate_database_redis_and_bytes_secret() -> None:
    settings = _settings()
    settings.database = DatabaseEndpoint("db", 5432, "app", "login", "runtime", "db", "require")
    settings.redis = RedisEndpoint("redis", 6379, "redis")
    bundle = SecretBundle(
        (("db", SensitiveValue(b"password")), ("redis", SensitiveValue(b"secret")))
    )

    database = worker_module._database_settings(settings, bundle)
    redis = worker_module._redis_settings(settings, bundle)
    assert database.application_name == "telegram_userbot_worker"
    assert redis.max_connections == 8

    settings.redis = None
    with pytest.raises(WorkerProcessError, match="WORKER_REDIS_SETTINGS_MISSING"):
        worker_module._redis_settings(settings, bundle)
    with pytest.raises(WorkerProcessError, match="WORKER_SECRET_INVALID"):
        worker_module._bytes_secret(bundle, "missing", minimum=1)


@pytest.mark.unit
def test_worker_uuid_and_status_helpers_cover_canonicality_checks() -> None:
    for raw in ("0" * 32, "01900000-0000-7000-8000-ABCDEFABCDEF"):
        with pytest.raises(JobExecutionError, match="WORKER_NOTIFICATION_INVALID"):
            worker_module._canonical_uuid(raw)
    with pytest.raises(JobExecutionError, match="WORKER_NOTIFICATION_INVALID"):
        worker_module._canonical_uuid(cast(Any, 1))

    settings = _settings()
    metadata = worker_module._status_metadata(settings, disk_band="normal")
    assert isinstance(metadata, ServiceStatusMetadata)
    assert metadata.source_commit_prefix == "a" * 12


@pytest.mark.unit
def test_worker_main_delegates_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "run", lambda argv, values: len(argv))
    monkeypatch.setattr(cast(Any, worker_module), "sys", SimpleNamespace(argv=["worker", "x"]))
    assert worker_module.main() == 1
