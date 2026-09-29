from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.queue.redis import RuntimeGenerationMarker
from telegram_userbot.processes import runtime_outbox
from telegram_userbot.processes.runtime_outbox import CanonicalRuntimeMarkerConsumer

ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000100")
PROFILE_ID = UUID("01900000-0000-7000-8000-000000000101")
CREDENTIAL_ID = UUID("01900000-0000-7000-8000-000000000102")
COMMAND_ID = UUID("01900000-0000-7000-8000-000000000103")
NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass(slots=True)
class _Invalidator:
    calls: list[tuple[UUID, int | None]] = field(default_factory=list)

    def invalidate_profile(self, profile_id: UUID, *, minimum_version: int | None = None) -> int:
        self.calls.append((profile_id, minimum_version))
        return len(self.calls)


class _Result:
    def __init__(
        self,
        rows: Sequence[Mapping[str, object]] | None = None,
        row: object | None = None,
    ) -> None:
        self._rows = tuple(rows or ())
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Mapping[str, object] | object | None:
        if self._row is not None:
            return self._row
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self._rows)


class _Session:
    def __init__(
        self,
        *,
        rows: Sequence[Mapping[str, object]] = (),
        row: object | None = None,
        scalar: UUID | None = None,
    ) -> None:
        self._rows = rows
        self._row = row
        self._scalar = scalar

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _: object) -> _Result:
        return _Result(self._rows, self._row)

    async def scalar(self, _: object) -> UUID | None:
        return self._scalar


class _Sessions:
    def __init__(self, *sessions: _Session) -> None:
        self._sessions = list(sessions)

    def __call__(self) -> _Session:
        return self._sessions.pop(0)


def _snapshot(*, version: int = 1) -> runtime_outbox._CanonicalModelSnapshot:
    return runtime_outbox._CanonicalModelSnapshot(
        profile_id=PROFILE_ID,
        profile_state="active",
        profile_version=version,
        active_config_version_no=version,
        credential_id=CREDENTIAL_ID,
        credential_status="active",
        credential_active_version_no=version,
        credential_latest_version_no=version,
        credential_version=version,
    )


def _model_row(*, version: int = 1) -> dict[str, object]:
    return {
        "id": PROFILE_ID,
        "state": "active",
        "version": version,
        "active_config_version_no": version,
        "credential_id": CREDENTIAL_ID,
        "credential_status": "active",
        "credential_active_version_no": version,
        "credential_latest_version_no": version,
        "credential_version": version,
    }


def _marker(topic: str, *, version: int = 1) -> RuntimeGenerationMarker:
    aggregate_id = CREDENTIAL_ID if topic == "model.credential.changed" else PROFILE_ID
    if topic.startswith("control."):
        aggregate_id = COMMAND_ID
    payload: dict[str, str | int | None] = {
        "profile_id": str(PROFILE_ID),
        "status": "active",
        "credential_version_no": version,
        "state": "active",
        "config_version_no": version,
        "command_id": str(COMMAND_ID),
    }
    return RuntimeGenerationMarker(
        outbox_id=version,
        topic=topic,
        aggregate_type="control_command" if topic.startswith("control.") else "model_profile",
        aggregate_id=aggregate_id,
        aggregate_version=version,
        payload=payload,
        payload_schema_version=1,
        account_id=ACCOUNT_ID,
    )


def _consumer(
    *,
    invalidator: _Invalidator | None = None,
    requested: list[UUID] | None = None,
    completed: list[UUID] | None = None,
    sessions: object | None = None,
) -> CanonicalRuntimeMarkerConsumer:
    topics: set[str] = set()
    if invalidator is not None:
        topics.update(runtime_outbox.MODEL_RUNTIME_MARKER_TOPICS)
    if requested is not None:
        topics.add("control.command.requested")
    if completed is not None:
        topics.add("control.command.completed")
    return CanonicalRuntimeMarkerConsumer(
        sessions=cast(async_sessionmaker[AsyncSession], sessions or _Sessions()),
        topics=topics,
        account_id=ACCOUNT_ID,
        model_invalidator=invalidator,
        control_requested=None if requested is None else requested.append,
        control_completed=None if completed is None else completed.append,
    )


@pytest.mark.unit
async def test_model_marker_only_invalidates_when_canonical_snapshot_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidator = _Invalidator()
    consumer = _consumer(invalidator=invalidator)
    snapshots = iter((_snapshot(version=1), _snapshot(version=1), _snapshot(version=2)))

    async def canonical(_: RuntimeGenerationMarker) -> runtime_outbox._CanonicalModelSnapshot:
        return next(snapshots)

    monkeypatch.setattr(consumer, "_canonical_model_profile", canonical)
    marker = _marker("model.config.activated")
    await consumer.handle(marker)
    await consumer.handle(marker)
    await consumer.handle(_marker("model.config.activated", version=2))

    assert invalidator.calls == [(PROFILE_ID, 1), (PROFILE_ID, 2)]


@pytest.mark.unit
async def test_control_markers_require_canonical_state_before_waking_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[UUID] = []
    completed: list[UUID] = []
    consumer = _consumer(requested=requested, completed=completed)

    async def requested_state(_: RuntimeGenerationMarker) -> tuple[UUID, str, datetime | None]:
        return (COMMAND_ID, "pending", None)

    monkeypatch.setattr(consumer, "_canonical_control_command", requested_state)
    await consumer.handle(_marker("control.command.requested"))
    assert requested == [COMMAND_ID]

    with pytest.raises(RuntimeError, match="precedes canonical completion"):
        await consumer.handle(_marker("control.command.completed"))

    async def completed_state(_: RuntimeGenerationMarker) -> tuple[UUID, str, datetime | None]:
        return (COMMAND_ID, "applied", NOW)

    monkeypatch.setattr(consumer, "_canonical_control_command", completed_state)
    await consumer.handle(_marker("control.command.completed"))
    assert completed == [COMMAND_ID]
    assert consumer._latest_completed == (NOW, COMMAND_ID)


@pytest.mark.unit
async def test_compensation_runs_independent_paths_before_reporting_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    consumer = _consumer(invalidator=_Invalidator(), requested=[], completed=[])

    async def model_failure() -> None:
        calls.append("models")
        raise RuntimeError("synthetic")

    async def requested() -> None:
        calls.append("requested")

    async def completed() -> None:
        calls.append("completed")

    monkeypatch.setattr(consumer, "_compensate_models", model_failure)
    monkeypatch.setattr(consumer, "_compensate_requested", requested)
    monkeypatch.setattr(consumer, "_compensate_completed", completed)

    with pytest.raises(RuntimeError, match="runtime marker compensation incomplete"):
        await consumer.compensate()
    assert calls == ["models", "requested", "completed"]


@pytest.mark.unit
def test_consumer_rejects_incomplete_configuration_and_malformed_model_rows() -> None:
    with pytest.raises(ValueError, match="canonical runtime marker consumer is invalid"):
        CanonicalRuntimeMarkerConsumer(
            sessions=cast(async_sessionmaker[AsyncSession], _Sessions()),
            topics=("control.command.requested",),
            account_id=ACCOUNT_ID,
        )
    with pytest.raises(ValueError, match="canonical runtime marker consumer is invalid"):
        CanonicalRuntimeMarkerConsumer(
            sessions=cast(async_sessionmaker[AsyncSession], _Sessions()),
            topics=("model.config.activated",),
            account_id=ACCOUNT_ID,
            model_invalidator=_Invalidator(),
        )

    snapshot = CanonicalRuntimeMarkerConsumer._model_snapshot(_model_row())
    assert snapshot.profile_id == PROFILE_ID
    with pytest.raises(RuntimeError, match="canonical model snapshot is incomplete"):
        CanonicalRuntimeMarkerConsumer._model_snapshot({})


@pytest.mark.unit
async def test_canonical_model_profile_checks_marker_relationship_and_values() -> None:
    credential_consumer = _consumer(
        invalidator=_Invalidator(), sessions=_Sessions(_Session(rows=(_model_row(),)))
    )
    credential = await credential_consumer._canonical_model_profile(
        _marker("model.credential.changed")
    )
    assert credential.credential_id == CREDENTIAL_ID

    config_consumer = _consumer(
        invalidator=_Invalidator(), sessions=_Sessions(_Session(rows=(_model_row(),)))
    )
    config = await config_consumer._canonical_model_profile(_marker("model.config.activated"))
    assert config.active_config_version_no == 1

    stale_consumer = _consumer(
        invalidator=_Invalidator(), sessions=_Sessions(_Session(rows=(_model_row(version=1),)))
    )
    with pytest.raises(RuntimeError, match="profile marker canonical relationship mismatch"):
        await stale_consumer._canonical_model_profile(_marker("model.config.activated", version=2))


@pytest.mark.unit
async def test_control_command_lookup_enforces_account_scope_and_missing_rows() -> None:
    row = {"id": COMMAND_ID, "state": "applied", "completed_at": NOW}
    consumer = _consumer(requested=[], sessions=_Sessions(_Session(rows=(row,))))
    assert await consumer._canonical_control_command(_marker("control.command.requested")) == (
        COMMAND_ID,
        "applied",
        NOW,
    )

    foreign = _marker("control.command.requested")
    foreign = RuntimeGenerationMarker(
        foreign.outbox_id,
        foreign.topic,
        foreign.aggregate_type,
        foreign.aggregate_id,
        foreign.aggregate_version,
        foreign.payload,
        foreign.payload_schema_version,
        UUID("01900000-0000-7000-8000-000000000199"),
    )
    with pytest.raises(RuntimeError, match="control marker account mismatch"):
        await consumer._canonical_control_command(foreign)

    missing = _consumer(requested=[], sessions=_Sessions(_Session()))
    with pytest.raises(RuntimeError, match="control marker canonical command missing"):
        await missing._canonical_control_command(_marker("control.command.requested"))


@pytest.mark.unit
async def test_compensation_scans_models_requested_and_completed_canonical_rows() -> None:
    invalidator = _Invalidator()
    requested: list[UUID] = []
    completed: list[UUID] = []
    consumer = _consumer(
        invalidator=invalidator,
        requested=requested,
        completed=completed,
        sessions=_Sessions(
            _Session(rows=(_model_row(),)),
            _Session(scalar=COMMAND_ID),
            _Session(row=(NOW, COMMAND_ID)),
        ),
    )

    await consumer.compensate()
    assert invalidator.calls == [(PROFILE_ID, 1)]
    assert requested == [COMMAND_ID]
    assert completed == [COMMAND_ID]


@pytest.mark.unit
async def test_wait_or_stop_handles_timeout_and_pre_signalled_stop() -> None:
    stop = asyncio.Event()
    await runtime_outbox._wait_or_stop(stop, 0.01)
    stop.set()
    await runtime_outbox._wait_or_stop(stop, 60)
