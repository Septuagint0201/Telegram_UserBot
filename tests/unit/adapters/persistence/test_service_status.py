from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from telegram_userbot.adapters.persistence.service_status import (
    RestoreGateRepository,
    ServiceStatusRepository,
)
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION
from telegram_userbot.platform.health.model import ServiceName
from telegram_userbot.platform.health.status import (
    RestoreGateRecord,
    RestoreGateState,
    RestoreVerification,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
INSTANCE_ID = UUID("01900000-0000-7000-8000-000000000010")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000001")


class _MappingsResult:
    def __init__(self, row: dict[str, object] | None = None, *, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def mappings(self) -> _MappingsResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _Session:
    def __init__(self, results: list[_MappingsResult]) -> None:
        self.results = results
        self.statements: list[ClauseElement] = []

    async def execute(self, statement: ClauseElement) -> _MappingsResult:
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return _MappingsResult()


def _heartbeat(
    *,
    heartbeat_at: datetime = NOW,
    readiness: ServiceReadiness = ServiceReadiness.STARTING,
    status_code: ServiceStatusCode = ServiceStatusCode.STARTING,
    metadata: ServiceStatusMetadata | None = None,
) -> ServiceHeartbeat:
    return ServiceHeartbeat(
        instance_id=INSTANCE_ID,
        service_name=ServiceName.APP,
        started_at=NOW,
        heartbeat_at=heartbeat_at,
        readiness=readiness,
        status_code=status_code,
        schema_revision=EXPECTED_SCHEMA_REVISION,
        metadata=metadata or ServiceStatusMetadata(deployment_id="prod-primary"),
    )


def _current(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instance_id": INSTANCE_ID,
        "service_name": "app",
        "started_at": NOW,
        "last_heartbeat_at": NOW,
        "readiness": "starting",
        "status_code": "STARTING",
        "schema_revision": EXPECTED_SCHEMA_REVISION,
        "last_successful_operation_at": None,
        "metadata": {"deployment_id": "prod-primary"},
        "version": 1,
    }
    row.update(changes)
    return row


def _restore_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "deployment_id": "prod-primary",
        "account_id": ACCOUNT_ID,
        "gate_state": "closed",
        "restore_generation": 1,
        "erasure_replay_verified": False,
        "unknown_send_reconciled": False,
        "credentials_verified": False,
        "session_verified": False,
        "verified_at": None,
        "version": 1,
        "updated_at": NOW,
    }
    row.update(changes)
    return row


@pytest.mark.unit
def test_status_contract_accepts_specific_degraded_reason_and_rejects_mismatch() -> None:
    degraded = _heartbeat(
        readiness=ServiceReadiness.DEGRADED,
        status_code=ServiceStatusCode.DATABASE_UNAVAILABLE,
    )
    assert degraded.status_code is ServiceStatusCode.DATABASE_UNAVAILABLE

    with pytest.raises(ValueError, match="readiness and status"):
        _heartbeat(readiness=ServiceReadiness.READY, status_code=ServiceStatusCode.DEGRADED)
    with pytest.raises(ValueError, match="not-ready"):
        _heartbeat(
            readiness=ServiceReadiness.NOT_READY,
            status_code=ServiceStatusCode.DEGRADED,
        )


@pytest.mark.unit
def test_status_metadata_is_allowlisted_bounded_and_immutable() -> None:
    metadata = ServiceStatusMetadata.from_mapping(
        {
            "deployment_id": "prod-primary",
            "source_commit_prefix": "a" * 12,
            "restart_count": 2,
            "disk_band": "warning",
        }
    )
    assert dict(metadata.as_mapping())["restart_count"] == 2
    assert list(metadata.as_mapping()) == [
        "deployment_id",
        "source_commit_prefix",
        "restart_count",
        "disk_band",
    ]

    with pytest.raises(ValueError, match="unknown field"):
        ServiceStatusMetadata.from_mapping(
            {"deployment_id": "prod-primary", "host_path": "/private"}
        )
    with pytest.raises(ValueError, match="band"):
        ServiceStatusMetadata(deployment_id="prod-primary", disk_band="full")
    with pytest.raises(ValueError, match="deployment id"):
        ServiceStatusMetadata.from_mapping({"deployment_id": 42})
    with pytest.raises(ValueError, match="digest prefix"):
        ServiceStatusMetadata(deployment_id="prod-primary", source_commit_prefix="wrong")
    with pytest.raises(ValueError, match="resource profile"):
        ServiceStatusMetadata(deployment_id="prod-primary", resource_profile="oversized")
    with pytest.raises(ValueError, match="restart count"):
        ServiceStatusMetadata(deployment_id="prod-primary", restart_count=-1)


@pytest.mark.unit
def test_status_contract_rejects_invalid_identity_time_and_schema() -> None:
    with pytest.raises(ValueError, match="nil"):
        ServiceHeartbeat(
            instance_id=UUID(int=0),
            service_name=ServiceName.APP,
            started_at=NOW,
            heartbeat_at=NOW,
            readiness=ServiceReadiness.STARTING,
            status_code=ServiceStatusCode.STARTING,
            metadata=ServiceStatusMetadata(deployment_id="prod-primary"),
        )
    with pytest.raises(ValueError, match="out of order"):
        _heartbeat(heartbeat_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="incompatible"):
        ServiceHeartbeat(
            instance_id=INSTANCE_ID,
            service_name=ServiceName.APP,
            started_at=NOW,
            heartbeat_at=NOW,
            readiness=ServiceReadiness.STARTING,
            status_code=ServiceStatusCode.STARTING,
            schema_revision="old",
            metadata=ServiceStatusMetadata(deployment_id="prod-primary"),
        )


@pytest.mark.unit
async def test_first_heartbeat_creates_projection_and_one_started_event() -> None:
    session = _Session([_MappingsResult(None)])
    repository = ServiceStatusRepository(cast(AsyncSession, session), ServiceName.APP)

    result = await repository.heartbeat(_heartbeat())

    assert result.transition_recorded is True
    assert result.version == 1
    assert len(session.statements) == 3
    assert session.statements[1].compile().params["service_name"] == "app"
    assert session.statements[2].compile().params["event_kind"] == "started"


@pytest.mark.unit
async def test_exact_heartbeat_replay_is_a_noop_without_version_or_event_growth() -> None:
    session = _Session([_MappingsResult(_current())])
    repository = ServiceStatusRepository(cast(AsyncSession, session), ServiceName.APP)

    result = await repository.heartbeat(_heartbeat())

    assert result.transition_recorded is False
    assert result.version == 1
    assert len(session.statements) == 1


@pytest.mark.unit
async def test_new_heartbeat_updates_projection_and_records_only_status_transition() -> None:
    session = _Session([_MappingsResult(_current()), _MappingsResult(rowcount=1)])
    repository = ServiceStatusRepository(cast(AsyncSession, session), ServiceName.APP)

    result = await repository.heartbeat(
        _heartbeat(
            heartbeat_at=NOW + timedelta(seconds=10),
            readiness=ServiceReadiness.NOT_READY,
            status_code=ServiceStatusCode.DATABASE_UNAVAILABLE,
        )
    )

    assert result.transition_recorded is True
    assert result.version == 2
    assert len(session.statements) == 3
    assert session.statements[2].compile().params["event_kind"] == "status_changed"


@pytest.mark.unit
async def test_ordinary_heartbeat_has_no_event_and_checks_cas_rowcount() -> None:
    later = NOW + timedelta(seconds=10)
    successful_session = _Session([_MappingsResult(_current()), _MappingsResult(rowcount=1)])
    successful_repository = ServiceStatusRepository(
        cast(AsyncSession, successful_session), ServiceName.APP
    )
    result = await successful_repository.heartbeat(_heartbeat(heartbeat_at=later))
    assert result.transition_recorded is False
    assert result.version == 2
    assert len(successful_session.statements) == 2

    repository = ServiceStatusRepository(
        cast(AsyncSession, _Session([_MappingsResult(_current()), _MappingsResult(rowcount=0)])),
        ServiceName.APP,
    )

    with pytest.raises(RuntimeError, match="compare-and-set"):
        await repository.heartbeat(_heartbeat(heartbeat_at=later))


@pytest.mark.unit
async def test_terminal_instance_cannot_restart_or_mutate_same_timestamp() -> None:
    stopped = _current(readiness="stopped", status_code="STOPPED")
    session = _Session([_MappingsResult(stopped)])
    repository = ServiceStatusRepository(cast(AsyncSession, session), ServiceName.APP)

    with pytest.raises(ValueError, match="terminal or replay"):
        await repository.heartbeat(_heartbeat(heartbeat_at=NOW + timedelta(seconds=10)))

    replay_session = _Session([_MappingsResult(_current())])
    replay_repository = ServiceStatusRepository(cast(AsyncSession, replay_session), ServiceName.APP)
    with pytest.raises(ValueError, match="terminal or replay"):
        await replay_repository.heartbeat(
            _heartbeat(
                readiness=ServiceReadiness.NOT_READY,
                status_code=ServiceStatusCode.DATABASE_UNAVAILABLE,
            )
        )


@pytest.mark.unit
async def test_repository_rejects_cross_service_and_immutable_identity_changes() -> None:
    repository = ServiceStatusRepository(cast(AsyncSession, _Session([])), ServiceName.CONTROL)
    with pytest.raises(ValueError, match="scope"):
        await repository.heartbeat(_heartbeat())

    changed = _current(schema_revision="wrong")
    app_repository = ServiceStatusRepository(
        cast(AsyncSession, _Session([_MappingsResult(changed)])), ServiceName.APP
    )
    with pytest.raises(ValueError, match="conflicts"):
        await app_repository.heartbeat(_heartbeat(heartbeat_at=NOW + timedelta(seconds=10)))


@pytest.mark.unit
async def test_status_repository_reads_typed_projection_and_missing_instance() -> None:
    repository = ServiceStatusRepository(
        cast(AsyncSession, _Session([_MappingsResult(_current()), _MappingsResult(None)])),
        ServiceName.APP,
    )
    loaded = await repository.get(INSTANCE_ID)
    assert loaded == _heartbeat()
    assert await repository.get(UUID("01900000-0000-7000-8000-000000000099")) is None


@pytest.mark.unit
async def test_status_repository_reads_latest_projection_for_service_dashboard() -> None:
    session = _Session([_MappingsResult(_current()), _MappingsResult(None)])
    repository = ServiceStatusRepository(cast(AsyncSession, session), ServiceName.APP)

    assert await repository.latest() == _heartbeat()
    assert await repository.latest() is None
    compiled = str(session.statements[0].compile())
    assert "service_instances.service_name" in compiled
    assert "ORDER BY service_instances.last_heartbeat_at DESC" in compiled
    assert "LIMIT" in compiled


@pytest.mark.unit
def test_restore_gate_contract_requires_typed_complete_verification() -> None:
    incomplete = RestoreVerification(False, True, True, True)
    assert incomplete.complete is False
    complete = RestoreVerification(True, True, True, True)
    assert complete.complete is True
    with pytest.raises(TypeError, match="booleans"):
        RestoreVerification(1, True, True, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="complete verification"):
        RestoreGateRecord(
            deployment_id="prod-primary",
            account_id=ACCOUNT_ID,
            state=RestoreGateState.OPEN,
            restore_generation=1,
            verification=incomplete,
            verified_at=None,
            version=1,
        )


@pytest.mark.unit
async def test_restore_gate_create_is_idempotent_and_identity_bound() -> None:
    session = _Session([_MappingsResult(), _MappingsResult(_restore_row())])
    repository = RestoreGateRepository(cast(AsyncSession, session))

    created = await repository.create_closed(
        deployment_id="prod-primary", account_id=ACCOUNT_ID, now=NOW
    )
    assert created.state is RestoreGateState.CLOSED
    assert created.version == 1
    assert len(session.statements) == 2
    assert "ON CONFLICT" in str(session.statements[0].compile())

    other_account = UUID("01900000-0000-7000-8000-000000000002")
    mismatch = RestoreGateRepository(
        cast(
            AsyncSession,
            _Session([_MappingsResult(), _MappingsResult(_restore_row(account_id=other_account))]),
        )
    )
    with pytest.raises(ValueError, match="identity conflicts"):
        await mismatch.create_closed(deployment_id="prod-primary", account_id=ACCOUNT_ID, now=NOW)


@pytest.mark.unit
async def test_restore_gate_cas_reset_verify_and_open_sequence() -> None:
    validating = _restore_row(gate_state="validating", restore_generation=2, version=2)
    verified = _restore_row(
        gate_state="validating",
        restore_generation=2,
        erasure_replay_verified=True,
        unknown_send_reconciled=True,
        credentials_verified=True,
        session_verified=True,
        verified_at=NOW,
        version=3,
    )
    opened = {**verified, "gate_state": "open", "version": 4}
    session = _Session(
        [
            _MappingsResult(validating),
            _MappingsResult(verified),
            _MappingsResult(opened),
            _MappingsResult(None),
        ]
    )
    repository = RestoreGateRepository(cast(AsyncSession, session))

    reset = await repository.reset_for_restore(
        deployment_id="prod-primary",
        account_id=ACCOUNT_ID,
        expected_version=1,
        now=NOW,
    )
    assert reset is not None
    assert reset.restore_generation == 2
    verification = RestoreVerification(True, True, True, True)
    recorded = await repository.record_verification(
        deployment_id="prod-primary",
        account_id=ACCOUNT_ID,
        expected_version=2,
        verification=verification,
        now=NOW,
    )
    assert recorded is not None
    assert recorded.verification.complete
    opened_record = await repository.open_if_verified(
        deployment_id="prod-primary",
        account_id=ACCOUNT_ID,
        expected_version=3,
        now=NOW,
    )
    assert opened_record is not None
    assert opened_record.state is RestoreGateState.OPEN
    assert (
        await repository.open_if_verified(
            deployment_id="prod-primary",
            account_id=ACCOUNT_ID,
            expected_version=4,
            now=NOW,
        )
        is None
    )
