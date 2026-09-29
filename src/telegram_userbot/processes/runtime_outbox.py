"""Typed Redis wake markers for non-worker transactional outbox topics.

The worker process is the sole relay owner.  Markers are metadata-only broadcast
hints; every observer must re-read its canonical PostgreSQL snapshot, and normal
database polling remains the compensation path if Redis coalesces or loses a
marker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.repositories import OutboxRepository
from telegram_userbot.adapters.persistence.schema import (
    control_commands,
    model_credentials,
    model_profiles,
)
from telegram_userbot.adapters.queue.redis import (
    RUNTIME_MARKER_TOPICS,
    RedisRuntime,
    RedisRuntimeError,
    RuntimeGenerationMarker,
)

RUNTIME_OUTBOX_POLL_SECONDS = 1.0
RUNTIME_OBSERVER_COMPENSATION_SECONDS = 30.0
RUNTIME_OUTBOX_BATCH = 100
RUNTIME_OUTBOX_MAX_RETRY_STATE = 4096
APP_RUNTIME_MARKER_TOPICS = frozenset(
    {
        "model.credential.changed",
        "model.config.activated",
        "control.command.requested",
    }
)
CONTROL_RUNTIME_MARKER_TOPICS = frozenset({"control.command.completed"})
MODEL_RUNTIME_MARKER_TOPICS = frozenset({"model.credential.changed", "model.config.activated"})

type RuntimeMarkerHandler = Callable[[RuntimeGenerationMarker], Awaitable[None]]
type RuntimeCompensationHandler = Callable[[], Awaitable[None]]
type RuntimeWakeHandler = Callable[[UUID], None]


class ModelProfileInvalidator(Protocol):
    def invalidate_profile(
        self,
        profile_id: UUID,
        *,
        minimum_version: int | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class _CanonicalModelSnapshot:
    """One row per stable profile; never fan out through credential versions."""

    profile_id: UUID
    profile_state: str
    profile_version: int
    active_config_version_no: int | None
    credential_id: UUID
    credential_status: str
    credential_active_version_no: int | None
    credential_latest_version_no: int
    credential_version: int


class RuntimeOutboxMarkerPublisher:
    """Relay four fixed topics as coalescing generation markers.

    PostgreSQL locks are never held across Redis I/O.  Duplicate SET operations
    are harmless, and the unpublished row remains the durable compensation fact
    until its CAS mark succeeds.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        redis: RedisRuntime,
        now: Callable[[], datetime] | None = None,
        poll_seconds: float = RUNTIME_OUTBOX_POLL_SECONDS,
        owns_relay: Callable[[], bool] | None = None,
    ) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("runtime outbox poll interval is invalid")
        self._sessions = sessions
        self._redis = redis
        self._now = now or (lambda: datetime.now(UTC))
        self._poll_seconds = poll_seconds
        self._owns_relay = owns_relay or (lambda: True)
        self._stop = asyncio.Event()
        self._failure_counts: dict[int, int] = {}
        self._retry_after: dict[int, float] = {}

    async def publish_once(self, *, limit: int = RUNTIME_OUTBOX_BATCH) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("runtime outbox batch is invalid")
        try:
            owns_relay = self._owns_relay() is True
        except Exception:
            owns_relay = False
        if not owns_relay:
            return 0
        async with self._sessions() as session, session.begin():
            records = await OutboxRepository(session).claim_batch(
                limit=limit,
                topics=RUNTIME_MARKER_TOPICS,
            )
        loop_time = asyncio.get_running_loop().time()
        published = 0
        for record in records:
            if loop_time < self._retry_after.get(record.id, 0.0):
                continue
            try:
                await self._redis.publish_generation_marker(record)
            except TypeError, ValueError:
                await self._record_failure(record.id, "RUNTIME_MARKER_INVALID")
                self._delay(record.id, loop_time, permanent=True)
                continue
            except RedisRuntimeError:
                await self._record_failure(record.id, "REDIS_RUNTIME_MARKER_WRITE_FAILED")
                self._delay(record.id, loop_time, permanent=False)
                continue
            async with self._sessions() as session, session.begin():
                marked = await OutboxRepository(session).mark_published(
                    outbox_id=record.id,
                    now=self._now(),
                )
            self._failure_counts.pop(record.id, None)
            self._retry_after.pop(record.id, None)
            published += int(marked)
        return published

    async def _record_failure(self, outbox_id: int, code: str) -> None:
        async with self._sessions() as session, session.begin():
            await OutboxRepository(session).record_failure(
                outbox_id=outbox_id,
                error_code=code,
            )

    def _delay(self, outbox_id: int, loop_time: float, *, permanent: bool) -> None:
        if (
            outbox_id not in self._retry_after
            and len(self._retry_after) >= RUNTIME_OUTBOX_MAX_RETRY_STATE
        ):
            # A malformed durable row can never reach the success path. Keep
            # retry bookkeeping bounded if an operator leaves many such rows
            # pending instead of allowing process memory to grow with the DB.
            oldest_id = next(iter(self._retry_after))
            self._retry_after.pop(oldest_id, None)
            self._failure_counts.pop(oldest_id, None)
        count = self._failure_counts.get(outbox_id, 0) + 1
        self._failure_counts[outbox_id] = count
        delay = 60.0 if permanent else min(60.0, float(2 ** min(count - 1, 6)))
        self._retry_after[outbox_id] = loop_time + delay

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.publish_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: S110 - durable PostgreSQL rows retain retry state
                # The unpublished PostgreSQL rows are the compensation queue.
                # A process-level transient must not permanently stop the relay.
                pass
            await _wait_or_stop(self._stop, self._poll_seconds)

    def stop(self) -> None:
        self._stop.set()


class RuntimeMarkerObserver:
    """Observe broadcast markers and periodically verify canonical snapshots."""

    def __init__(  # noqa: PLR0913 - timing and handlers are explicit test seams
        self,
        *,
        redis: RedisRuntime,
        topics: Iterable[str],
        handler: RuntimeMarkerHandler,
        compensate: RuntimeCompensationHandler,
        poll_seconds: float = RUNTIME_OUTBOX_POLL_SECONDS,
        compensation_seconds: float = RUNTIME_OBSERVER_COMPENSATION_SECONDS,
    ) -> None:
        selected = frozenset(topics)
        if (
            not selected
            or not selected <= RUNTIME_MARKER_TOPICS
            or not callable(handler)
            or not callable(compensate)
            or not 0.1 <= poll_seconds <= 60
            or not 1 <= compensation_seconds <= 3600
        ):
            raise ValueError("runtime marker observer configuration is invalid")
        self._redis = redis
        self._topics = tuple(sorted(selected))
        self._handler = handler
        self._compensate = compensate
        self._poll_seconds = poll_seconds
        self._compensation_seconds = compensation_seconds
        self._last_seen: dict[str, int] = {}
        self._last_compensation_at: float | None = None
        self._last_compensation_attempt_at: float | None = None
        self._initial_compensation_complete = False
        self._stop = asyncio.Event()
        self._running = False

    @property
    def ready(self) -> bool:
        return self._running and self._initial_compensation_complete and not self._stop.is_set()

    async def observe_once(self) -> int:
        observed = 0
        for topic in self._topics:
            try:
                marker = await self._redis.read_generation_marker(topic)
                if marker is None or marker.outbox_id <= self._last_seen.get(topic, 0):
                    continue
                await self._handler(marker)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: S112 - other topics and PG compensation remain live
                # One corrupt hint or unavailable canonical aggregate must not
                # starve other topics or the independent PostgreSQL scan.
                continue
            self._last_seen[topic] = marker.outbox_id
            observed += 1
        return observed

    async def compensate_once(self) -> None:
        await self._compensate()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._running = True
        try:
            while not self._stop.is_set():
                try:
                    await self.observe_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: S110 - next hint/poll retries
                    pass
                current = loop.time()
                if (
                    self._last_compensation_attempt_at is None
                    or current >= self._last_compensation_attempt_at + self._compensation_seconds
                ):
                    self._last_compensation_attempt_at = current
                    try:
                        await self.compensate_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: S110 - retry at the bounded interval
                        pass
                    else:
                        self._last_compensation_at = current
                        self._initial_compensation_complete = True
                await _wait_or_stop(self._stop, self._poll_seconds)
        finally:
            self._running = False

    def stop(self) -> None:
        self._stop.set()


class CanonicalRuntimeMarkerConsumer:
    """Re-read PostgreSQL before turning a Redis hint into a local action.

    The marker is never trusted as configuration or command truth.  Stale but
    structurally valid markers still cause safe cache eviction/wakeup after the
    current row proves the aggregate relationship.  A 30-second scan performs
    the same actions when Redis loses or coalesces notifications.
    """

    def __init__(  # noqa: PLR0913 - each optional action is an explicit process seam
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        topics: Iterable[str],
        account_id: UUID,
        model_invalidator: ModelProfileInvalidator | None = None,
        control_requested: RuntimeWakeHandler | None = None,
        control_completed: RuntimeWakeHandler | None = None,
    ) -> None:
        selected = frozenset(topics)
        expected: set[str] = set()
        if model_invalidator is not None:
            expected.update(MODEL_RUNTIME_MARKER_TOPICS)
        if control_requested is not None:
            expected.add("control.command.requested")
        if control_completed is not None:
            expected.add("control.command.completed")
        if (
            not isinstance(account_id, UUID)
            or account_id.int == 0
            or selected != frozenset(expected)
        ):
            raise ValueError("canonical runtime marker consumer is invalid")
        self._sessions = sessions
        self._topics = selected
        self._account_id = account_id
        self._model_invalidator = model_invalidator
        self._control_requested = control_requested
        self._control_completed = control_completed
        self._model_snapshots: dict[UUID, _CanonicalModelSnapshot] = {}
        self._latest_completed: tuple[datetime, UUID] | None = None

    async def handle(self, marker: RuntimeGenerationMarker) -> None:
        if marker.topic not in self._topics:
            raise ValueError("runtime marker routed to the wrong consumer")
        if marker.topic in MODEL_RUNTIME_MARKER_TOPICS:
            snapshot = await self._canonical_model_profile(marker)
            invalidator = self._model_invalidator
            if invalidator is None:
                raise RuntimeError("runtime model invalidator disappeared")
            if self._model_snapshots.get(snapshot.profile_id) != snapshot:
                invalidator.invalidate_profile(
                    snapshot.profile_id,
                    minimum_version=snapshot.profile_version,
                )
                self._model_snapshots[snapshot.profile_id] = snapshot
            return
        command_id, state, completed_at = await self._canonical_control_command(marker)
        if marker.topic == "control.command.requested":
            callback = self._control_requested
        else:
            if state == "pending" or completed_at is None:
                raise RuntimeError("control completion marker precedes canonical completion")
            callback = self._control_completed
        if callback is None:
            raise RuntimeError("runtime command wake handler disappeared")
        callback(command_id)
        if marker.topic == "control.command.completed":
            self._latest_completed = (cast(datetime, completed_at), command_id)

    async def compensate(self) -> None:
        operations = []
        if self._model_invalidator is not None:
            operations.append(self._compensate_models)
        if self._control_requested is not None:
            operations.append(self._compensate_requested)
        if self._control_completed is not None:
            operations.append(self._compensate_completed)
        failure: Exception | None = None
        for operation in operations:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # App observes model and command topics together.  A failure in
                # one canonical query must not suppress the other wake path.
                failure = failure or error
        if failure is not None:
            raise RuntimeError("runtime marker compensation incomplete") from failure

    async def _canonical_model_profile(
        self, marker: RuntimeGenerationMarker
    ) -> _CanonicalModelSnapshot:
        async with self._sessions() as session:
            fields = (
                model_profiles.c.id,
                model_profiles.c.state,
                model_profiles.c.version,
                model_profiles.c.active_config_version_no,
                model_credentials.c.id.label("credential_id"),
                model_credentials.c.status.label("credential_status"),
                model_credentials.c.active_version_no.label("credential_active_version_no"),
                model_credentials.c.latest_version_no.label("credential_latest_version_no"),
                model_credentials.c.version.label("credential_version"),
            )
            if marker.topic == "model.credential.changed":
                row = (
                    (
                        await session.execute(
                            select(*fields)
                            .join(
                                model_credentials,
                                model_credentials.c.profile_id == model_profiles.c.id,
                            )
                            .where(model_credentials.c.id == marker.aggregate_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    row is None
                    or row["id"] != UUID(cast(str, marker.payload["profile_id"]))
                    or row["credential_version"] < marker.aggregate_version
                ):
                    raise RuntimeError("credential marker canonical relationship mismatch")
                if row["credential_version"] == marker.aggregate_version and (
                    row["credential_status"] != marker.payload["status"]
                    or row["credential_active_version_no"]
                    != marker.payload["credential_version_no"]
                ):
                    raise RuntimeError("credential marker canonical value mismatch")
                return self._model_snapshot(row)
            row = (
                (
                    await session.execute(
                        select(*fields)
                        .join(
                            model_credentials,
                            model_credentials.c.profile_id == model_profiles.c.id,
                        )
                        .where(model_profiles.c.id == marker.aggregate_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["version"] < marker.aggregate_version:
                raise RuntimeError("profile marker canonical relationship mismatch")
            if row["version"] == marker.aggregate_version and (
                row["state"] != marker.payload["state"]
                or row["active_config_version_no"] != marker.payload["config_version_no"]
            ):
                raise RuntimeError("profile marker canonical value mismatch")
            return self._model_snapshot(row)

    @staticmethod
    def _model_snapshot(row: object) -> _CanonicalModelSnapshot:
        values = cast(dict[str, object], row)
        try:
            return _CanonicalModelSnapshot(
                profile_id=cast(UUID, values["id"]),
                profile_state=cast(str, values["state"]),
                profile_version=cast(int, values["version"]),
                active_config_version_no=cast(int | None, values["active_config_version_no"]),
                credential_id=cast(UUID, values["credential_id"]),
                credential_status=cast(str, values["credential_status"]),
                credential_active_version_no=cast(
                    int | None, values["credential_active_version_no"]
                ),
                credential_latest_version_no=cast(int, values["credential_latest_version_no"]),
                credential_version=cast(int, values["credential_version"]),
            )
        except KeyError:
            raise RuntimeError("canonical model snapshot is incomplete") from None

    async def _canonical_control_command(
        self, marker: RuntimeGenerationMarker
    ) -> tuple[UUID, str, datetime | None]:
        if marker.account_id != self._account_id:
            raise RuntimeError("control marker account mismatch")
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        select(
                            control_commands.c.id,
                            control_commands.c.state,
                            control_commands.c.completed_at,
                        ).where(
                            control_commands.c.id == marker.aggregate_id,
                            control_commands.c.account_id == self._account_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RuntimeError("control marker canonical command missing")
        return (
            cast(UUID, row["id"]),
            cast(str, row["state"]),
            cast(datetime | None, row["completed_at"]),
        )

    async def _compensate_models(self) -> None:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        model_profiles.c.id,
                        model_profiles.c.state,
                        model_profiles.c.version,
                        model_profiles.c.active_config_version_no,
                        model_credentials.c.id.label("credential_id"),
                        model_credentials.c.status.label("credential_status"),
                        model_credentials.c.active_version_no.label("credential_active_version_no"),
                        model_credentials.c.latest_version_no.label("credential_latest_version_no"),
                        model_credentials.c.version.label("credential_version"),
                    )
                    .join(
                        model_credentials,
                        model_credentials.c.profile_id == model_profiles.c.id,
                    )
                    .order_by(model_profiles.c.id)
                )
            ).mappings()
            current: dict[UUID, _CanonicalModelSnapshot] = {}
            for row in rows:
                snapshot = self._model_snapshot(row)
                if snapshot.profile_id in current:
                    # Never let a future accidental one-to-many join silently
                    # overwrite which credential/config snapshot is canonical.
                    raise RuntimeError("canonical model profile is duplicated")
                current[snapshot.profile_id] = snapshot
        invalidator = self._model_invalidator
        if invalidator is None:
            raise RuntimeError("runtime model invalidator disappeared")
        for profile_id, snapshot in current.items():
            if self._model_snapshots.get(profile_id) != snapshot:
                invalidator.invalidate_profile(profile_id, minimum_version=snapshot.profile_version)
        for missing_profile_id in self._model_snapshots.keys() - current.keys():
            invalidator.invalidate_profile(missing_profile_id)
        self._model_snapshots = current

    async def _compensate_requested(self) -> None:
        async with self._sessions() as session:
            command_id = await session.scalar(
                select(control_commands.c.id)
                .where(
                    control_commands.c.account_id == self._account_id,
                    control_commands.c.state == "pending",
                )
                .order_by(control_commands.c.created_at, control_commands.c.id)
                .limit(1)
            )
        if command_id is not None:
            callback = self._control_requested
            if callback is None:
                raise RuntimeError("runtime command wake handler disappeared")
            callback(cast(UUID, command_id))

    async def _compensate_completed(self) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(control_commands.c.completed_at, control_commands.c.id)
                    .where(
                        control_commands.c.account_id == self._account_id,
                        control_commands.c.state.in_(("applied", "rejected")),
                        control_commands.c.completed_at.is_not(None),
                    )
                    .order_by(control_commands.c.completed_at.desc(), control_commands.c.id.desc())
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return
        current = (cast(datetime, row[0]), cast(UUID, row[1]))
        if self._latest_completed == current:
            return
        callback = self._control_completed
        if callback is None:
            raise RuntimeError("runtime command wake handler disappeared")
        callback(current[1])
        self._latest_completed = current


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass


__all__ = [
    "APP_RUNTIME_MARKER_TOPICS",
    "CONTROL_RUNTIME_MARKER_TOPICS",
    "MODEL_RUNTIME_MARKER_TOPICS",
    "RUNTIME_OBSERVER_COMPENSATION_SECONDS",
    "RUNTIME_OUTBOX_BATCH",
    "RUNTIME_OUTBOX_POLL_SECONDS",
    "CanonicalRuntimeMarkerConsumer",
    "ModelProfileInvalidator",
    "RuntimeMarkerObserver",
    "RuntimeOutboxMarkerPublisher",
]
