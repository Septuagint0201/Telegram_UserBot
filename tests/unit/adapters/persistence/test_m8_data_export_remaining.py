from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.data_export_repository import DataExportRepository
from telegram_userbot.domain.data_export import (
    EXPORT_FORMAT_VERSION,
    DataExportRequest,
    DataExportState,
    pseudonymous_export_actor,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
REQUEST_ID = UUID("01900000-0000-7000-8000-000000000211")
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000212")
CONTACT_ID = UUID("01900000-0000-7000-8000-000000000213")
OWNER_ID = UUID("01900000-0000-7000-8000-000000000214")
ACTOR = "actor:hmac-sha256:" + "a" * 64


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        rowcount: int = 0,
    ) -> None:
        self.row = row
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row


def _row(
    state: DataExportState,
    *,
    version: int = 1,
    attempt_count: int = 0,
    artifact_deleted_at: datetime | None = None,
) -> dict[str, object]:
    completed_at: datetime | None = None
    artifact_sha256: bytes | None = None
    last_error_code: str | None = None
    owner_instance_id: UUID | None = None
    lease_expires_at: datetime | None = None
    if state is DataExportState.CLAIMED:
        owner_instance_id = OWNER_ID
        lease_expires_at = NOW + timedelta(hours=1)
    elif state is DataExportState.COMPLETED:
        completed_at = NOW + timedelta(minutes=1)
        artifact_sha256 = b"d" * 32
    elif state in {DataExportState.FAILED, DataExportState.EXPIRED}:
        completed_at = NOW + timedelta(minutes=1)
        last_error_code = "REQUEST_EXPIRED" if state is DataExportState.EXPIRED else "EXPORT_FAILED"
    return {
        "id": REQUEST_ID,
        "account_id": ACCOUNT_ID,
        "contact_id": CONTACT_ID,
        "state": state.value,
        "requested_by": ACTOR,
        "format_version": EXPORT_FORMAT_VERSION,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "owner_instance_id": owner_instance_id,
        "lease_expires_at": lease_expires_at,
        "completed_at": completed_at,
        "artifact_sha256": artifact_sha256,
        "artifact_deleted_at": artifact_deleted_at,
        "last_error_code": last_error_code,
        "attempt_count": attempt_count,
        "version": version,
    }


def _request() -> DataExportRequest:
    return DataExportRequest(
        id=REQUEST_ID,
        account_id=ACCOUNT_ID,
        contact_id=CONTACT_ID,
        state=DataExportState.REQUESTED,
        requested_by=ACTOR,
        format_version=EXPORT_FORMAT_VERSION,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        owner_instance_id=None,
        lease_expires_at=None,
        completed_at=None,
        artifact_sha256=None,
        artifact_deleted_at=None,
        last_error_code=None,
        attempt_count=0,
        version=1,
    )


def _repository(session: AsyncMock) -> DataExportRepository:
    return DataExportRepository(cast(AsyncSession, session))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_and_create_round_trip_record_without_persisting_secret_material() -> None:
    request = _request()
    session = AsyncMock()
    session.execute.side_effect = [
        object(),
        _Result(_row(DataExportState.REQUESTED)),
        _Result(),
    ]
    repository = _repository(session)

    loaded = await repository.create(request)

    assert loaded == request
    assert await repository.get(REQUEST_ID) is None
    insert = (
        session.execute.await_args_list[0]
        .args[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert insert.params["requested_by"] == ACTOR
    assert "artifact" not in str(insert).lower()
    assert "path" not in str(insert).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_rejects_non_requested_and_unobservable_or_conflicting_rows() -> None:
    session = AsyncMock()
    repository = _repository(session)
    # A non-requested state is rejected before any SQL is issued.
    invalid = cast(Any, SimpleNamespace(state=DataExportState.CLAIMED))
    with pytest.raises(ValueError, match="must be requested"):
        await repository.create(invalid)
    session.execute.assert_not_awaited()

    session.execute.side_effect = [object(), _Result()]
    with pytest.raises(RuntimeError, match="not observable"):
        await repository.create(_request())

    session.execute.side_effect = [object(), _Result(_row(DataExportState.REQUESTED, version=9))]
    with pytest.raises(ValueError, match="conflicts"):
        await repository.create(_request())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claim_complete_fail_expire_and_delete_follow_fenced_state_transitions() -> None:
    claimed_row = _row(DataExportState.CLAIMED, version=2, attempt_count=1)
    completed_row = _row(DataExportState.COMPLETED, version=3, attempt_count=1)
    failed_row = _row(DataExportState.FAILED, version=4, attempt_count=1)
    deleted_row = _row(
        DataExportState.COMPLETED,
        version=4,
        attempt_count=1,
        artifact_deleted_at=NOW + timedelta(minutes=2),
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _Result({"id": REQUEST_ID, "version": 1}),
        _Result(claimed_row),
        _Result(completed_row),
        _Result(failed_row),
        _Result(rowcount=2),
        _Result(deleted_row),
    ]
    repository = _repository(session)

    claimed = await repository.claim_next(
        owner_instance_id=OWNER_ID,
        now=NOW,
        lease_expires_at=NOW + timedelta(hours=1),
    )
    assert claimed is not None
    assert claimed.state is DataExportState.CLAIMED
    assert claimed.owner_instance_id == OWNER_ID

    completed = await repository.complete(
        request_id=REQUEST_ID,
        owner_instance_id=OWNER_ID,
        expected_version=2,
        artifact_sha256=b"x" * 32,
        now=NOW + timedelta(minutes=1),
    )
    assert completed is not None
    assert completed.state is DataExportState.COMPLETED
    assert completed.artifact_sha256 == b"d" * 32

    failed = await repository.fail(
        request_id=REQUEST_ID,
        owner_instance_id=OWNER_ID,
        expected_version=3,
        error_code="EXPORT_FAILED",
        now=NOW + timedelta(minutes=1),
    )
    assert failed is not None
    assert failed.state is DataExportState.FAILED
    assert failed.last_error_code == "EXPORT_FAILED"

    assert await repository.expire_due(now=NOW + timedelta(days=2)) == 2
    deleted = await repository.mark_artifact_deleted(
        request_id=REQUEST_ID,
        expected_version=4,
        now=NOW + timedelta(minutes=2),
    )
    assert deleted is not None
    assert deleted.artifact_deleted_at == NOW + timedelta(minutes=2)

    claim_sql = str(
        session.execute.await_args_list[0]
        .args[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    finalize_sql = str(
        session.execute.await_args_list[2]
        .args[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "owner_instance_id" in finalize_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claim_and_terminal_methods_fail_closed_on_invalid_inputs_or_cas_misses() -> None:
    repository = _repository(AsyncMock())
    with pytest.raises(ValueError, match="owner id"):
        await repository.claim_next(
            owner_instance_id=UUID(int=0), now=NOW, lease_expires_at=NOW + timedelta(minutes=1)
        )
    with pytest.raises(ValueError, match="lease"):
        await repository.claim_next(owner_instance_id=OWNER_ID, now=NOW, lease_expires_at=NOW)
    with pytest.raises(ValueError, match="SHA-256"):
        await repository.complete(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=1,
            artifact_sha256=b"short",
            now=NOW,
        )
    with pytest.raises(ValueError, match="error code"):
        await repository.fail(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=1,
            error_code="bad code",
            now=NOW,
        )
    with pytest.raises(ValueError, match="expected version"):
        await repository.renew(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=0,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )

    session = AsyncMock()
    session.execute.side_effect = [
        _Result(),
        _Result(),
        _Result(),
        _Result(rowcount=0),
        _Result(),
    ]
    repository = _repository(session)
    assert (
        await repository.claim_next(
            owner_instance_id=OWNER_ID,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        is None
    )
    assert (
        await repository.renew(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=1,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        is None
    )
    assert (
        await repository.complete(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=1,
            artifact_sha256=b"x" * 32,
            now=NOW,
        )
        is None
    )
    assert (
        await repository.fail(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=1,
            error_code="EXPORT_FAILED",
            now=NOW,
        )
        is None
    )
    assert (
        await repository.mark_artifact_deleted(
            request_id=REQUEST_ID,
            expected_version=1,
            now=NOW,
        )
        is None
    )


@pytest.mark.unit
def test_pseudonymous_actor_reference_is_stable_for_export_rows() -> None:
    first = pseudonymous_export_actor(actor_identity=b"operator", hmac_key=b"k" * 32)
    second = pseudonymous_export_actor(actor_identity=b"operator", hmac_key=b"k" * 32)
    assert first == second
    assert first.startswith("actor:hmac-sha256:")
