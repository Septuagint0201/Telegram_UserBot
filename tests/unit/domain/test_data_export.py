from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

import pytest

from telegram_userbot.domain.data_export import (
    DataExportRequest,
    DataExportState,
    pseudonymous_export_actor,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT = UUID("01900000-0000-7000-8000-000000000001")
ACTOR = "actor:hmac-sha256:" + "a" * 64


def _request(
    *, state: DataExportState = DataExportState.REQUESTED, **changes: object
) -> DataExportRequest:
    values: dict[str, object] = {
        "id": uuid7(),
        "account_id": ACCOUNT,
        "contact_id": None,
        "state": state,
        "requested_by": ACTOR,
        "format_version": 1,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "owner_instance_id": None,
        "lease_expires_at": None,
        "completed_at": None,
        "artifact_sha256": None,
        "artifact_deleted_at": None,
        "last_error_code": None,
        "attempt_count": 0,
        "version": 1,
    }
    values.update(changes)
    return DataExportRequest(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_actor_pseudonym_is_deterministic_and_validates_key_material() -> None:
    assert pseudonymous_export_actor(
        actor_identity=b"telegram:123", hmac_key=b"k" * 32
    ) == pseudonymous_export_actor(actor_identity=b"telegram:123", hmac_key=b"k" * 32)
    with pytest.raises(ValueError, match="identity"):
        pseudonymous_export_actor(actor_identity=b"", hmac_key=b"k" * 32)
    with pytest.raises(ValueError, match="identity"):
        pseudonymous_export_actor(actor_identity=b"x" * 257, hmac_key=b"k" * 32)
    with pytest.raises(ValueError, match="HMAC"):
        pseudonymous_export_actor(actor_identity=b"x", hmac_key=b"k")


@pytest.mark.unit
def test_request_validates_each_durable_state_shape() -> None:
    requested = _request()
    assert requested.state is DataExportState.REQUESTED

    claimed = _request(
        state=DataExportState.CLAIMED,
        owner_instance_id=UUID("01900000-0000-7000-8000-000000000002"),
        lease_expires_at=NOW + timedelta(minutes=10),
        attempt_count=1,
        version=2,
    )
    assert claimed.state is DataExportState.CLAIMED

    completed = _request(
        state=DataExportState.COMPLETED,
        completed_at=NOW + timedelta(minutes=5),
        artifact_sha256=b"x" * 32,
        attempt_count=1,
        version=2,
    )
    assert completed.state is DataExportState.COMPLETED
    assert (
        _request(
            state=DataExportState.COMPLETED,
            completed_at=NOW + timedelta(minutes=5),
            artifact_sha256=b"x" * 32,
            artifact_deleted_at=NOW + timedelta(minutes=6),
            attempt_count=1,
            version=2,
        ).artifact_deleted_at
        is not None
    )

    for state, error in (
        (DataExportState.FAILED, "FAILED"),
        (DataExportState.EXPIRED, "EXPIRED"),
    ):
        assert _request(state=state, completed_at=NOW, last_error_code=error).state is state


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [
        {"id": UUID(int=0)},
        {"account_id": UUID(int=0)},
        {"requested_by": "actor:bad"},
        {"format_version": 2},
        {"expires_at": NOW},
        {"created_at": NOW.replace(tzinfo=None)},
        {"artifact_sha256": b"short"},
        {"last_error_code": "bad-code"},
        {"attempt_count": -1},
        {"version": 0},
        {"owner_instance_id": UUID(int=0)},
    ],
)
def test_request_rejects_invalid_common_fields(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _request(**cast(dict[str, Any], changes))


@pytest.mark.unit
def test_request_rejects_inconsistent_state_fields() -> None:
    with pytest.raises(ValueError, match="fields"):
        _request(owner_instance_id=UUID("01900000-0000-7000-8000-000000000002"))
    with pytest.raises(ValueError, match="fields"):
        _request(
            state=DataExportState.CLAIMED,
            owner_instance_id=UUID("01900000-0000-7000-8000-000000000002"),
            lease_expires_at=NOW - timedelta(seconds=1),
            attempt_count=1,
        )
    with pytest.raises(ValueError, match="fields"):
        _request(
            state=DataExportState.COMPLETED,
            completed_at=NOW,
            artifact_sha256=b"x" * 32,
            artifact_deleted_at=NOW - timedelta(seconds=1),
            attempt_count=1,
        )
