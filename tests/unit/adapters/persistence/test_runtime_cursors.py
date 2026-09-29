from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.runtime_cursors import RuntimeCursorRepository
from telegram_userbot.platform.runtime.cursors import (
    ControlBotCursor,
    ControlUpdateClaim,
    ControlUpdateClaimOutcome,
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
    TelegramIngestWatermark,
)

NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)
OWNER = UUID("01900000-0000-7000-8000-000000000010")
ACCOUNT = UUID("01900000-0000-7000-8000-000000000001")


class _Result:
    def __init__(
        self,
        value: dict[str, object] | list[dict[str, object]] | None = None,
    ) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, object]:
        assert isinstance(self.value, dict)
        return self.value

    def one_or_none(self) -> dict[str, object] | None:
        assert self.value is None or isinstance(self.value, dict)
        return self.value

    def all(self) -> list[dict[str, object]]:
        assert isinstance(self.value, list)
        return self.value


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0)


def _cursor_row(**changes: object) -> dict[str, object]:
    return {
        "deployment_id": "prod-primary",
        "bot_user_id": 7000000002,
        "next_offset": 0,
        "version": 1,
        "updated_at": NOW,
        **changes,
    }


def _receipt_row(**changes: object) -> dict[str, object]:
    return {
        "deployment_id": "prod-primary",
        "bot_user_id": 7000000002,
        "update_id": 42,
        "state": "claimed",
        "disposition": None,
        "send_state": "not_required",
        "owner_instance_id": OWNER,
        "claimed_at": NOW,
        "lease_expires_at": NOW + timedelta(seconds=30),
        "completed_at": None,
        "attempt_count": 1,
        "version": 1,
        **changes,
    }


def _watermark_row(**changes: object) -> dict[str, object]:
    return {
        "account_id": ACCOUNT,
        "scope": "account",
        "pts": 10,
        "pts_count": 1,
        "update_identity": "UpdateNewMessage:10",
        "durable_ingested_at": NOW,
        "version": 1,
        "updated_at": NOW,
        **changes,
    }


@pytest.mark.unit
def test_cursor_contract_accepts_canonical_scope_terminal_send_and_late_completion() -> None:
    completed = ControlUpdateReceipt(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        state=ControlUpdateState.COMPLETED,
        disposition=ControlUpdateDisposition.HANDLED,
        send_state=ControlUpdateSendState.UNKNOWN,
        owner_instance_id=OWNER,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
        attempt_count=1,
        version=2,
    )
    assert completed.send_state is ControlUpdateSendState.UNKNOWN
    assert ControlUpdateSendState.NOT_SENT.value == "not_sent"
    assert (
        TelegramIngestWatermark(
            ACCOUNT,
            "channel:123456",
            10,
            1,
            "UpdateNewChannelMessage:10",
            NOW,
            1,
            NOW,
        ).scope
        == "channel:123456"
    )
    with pytest.raises(ValueError, match="scope"):
        TelegramIngestWatermark(
            ACCOUNT,
            "chat:-1000000000123",
            10,
            1,
            "UpdateNewChannelMessage:10",
            NOW,
            1,
            NOW,
        )


@pytest.mark.unit
async def test_control_cursor_create_and_new_claim_are_typed() -> None:
    session = _Session(
        [
            _Result(),
            _Result(_cursor_row()),
            _Result(_cursor_row()),
            _Result(),
            _Result(_receipt_row()),
        ]
    )
    repository = RuntimeCursorRepository(cast(AsyncSession, session))
    cursor = await repository.get_or_create_control_cursor(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        initial_offset=0,
        now=NOW,
    )
    claim = await repository.claim_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert cursor.next_offset == 0
    assert claim.outcome is ControlUpdateClaimOutcome.ACQUIRED
    assert claim.acquired
    assert claim.receipt is not None
    assert claim.receipt.version == 1


@pytest.mark.unit
async def test_control_claim_distinguishes_below_offset_completed_busy_and_reclaim() -> None:
    below_repository = RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(_cursor_row(next_offset=43))]))
    )
    below = await below_repository.claim_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert below.outcome is ControlUpdateClaimOutcome.BELOW_OFFSET

    completed_repository = RuntimeCursorRepository(
        cast(
            AsyncSession,
            _Session(
                [
                    _Result(_cursor_row()),
                    _Result(),
                    _Result(
                        _receipt_row(
                            state="completed",
                            disposition="ignored",
                            completed_at=NOW,
                            version=2,
                        )
                    ),
                ]
            ),
        )
    )
    completed = await completed_repository.claim_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert completed.outcome is ControlUpdateClaimOutcome.COMPLETED

    other_owner = UUID("01900000-0000-7000-8000-000000000011")
    busy_repository = RuntimeCursorRepository(
        cast(
            AsyncSession,
            _Session(
                [
                    _Result(_cursor_row()),
                    _Result(),
                    _Result(_receipt_row(owner_instance_id=other_owner)),
                ]
            ),
        )
    )
    busy = await busy_repository.claim_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert busy.outcome is ControlUpdateClaimOutcome.BUSY

    reclaimed_repository = RuntimeCursorRepository(
        cast(
            AsyncSession,
            _Session(
                [
                    _Result(_cursor_row()),
                    _Result(),
                    _Result(
                        _receipt_row(
                            owner_instance_id=other_owner,
                            claimed_at=NOW - timedelta(minutes=2),
                            lease_expires_at=NOW - timedelta(minutes=1),
                        )
                    ),
                    _Result(_receipt_row(attempt_count=2, version=2)),
                ]
            ),
        )
    )
    reclaimed = await reclaimed_repository.claim_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert reclaimed.acquired
    assert reclaimed.receipt is not None
    assert reclaimed.receipt.attempt_count == 2


@pytest.mark.unit
async def test_complete_late_then_mark_unknown_are_version_fenced() -> None:
    completed = _receipt_row(
        state="completed",
        disposition="rejected",
        send_state="pending",
        completed_at=NOW + timedelta(seconds=40),
        version=2,
    )
    not_sent = {**completed, "send_state": "not_sent", "version": 3}
    session = _Session([_Result(completed), _Result(not_sent), _Result(None)])
    repository = RuntimeCursorRepository(cast(AsyncSession, session))

    result = await repository.complete_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        expected_version=1,
        disposition=ControlUpdateDisposition.REJECTED,
        response_required=True,
        now=NOW + timedelta(seconds=40),
    )
    assert result is not None
    assert result.send_state is ControlUpdateSendState.PENDING
    statement = str(session.statements[0])
    assert "lease_expires_at" not in statement.partition(" RETURNING ")[0]

    marked = await repository.mark_control_response(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        expected_version=2,
        send_state=ControlUpdateSendState.NOT_SENT,
    )
    assert marked is not None
    assert marked.send_state is ControlUpdateSendState.NOT_SENT
    assert (
        await repository.mark_control_response(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            expected_version=2,
            send_state=ControlUpdateSendState.SENT,
        )
        is None
    )


@pytest.mark.unit
async def test_post_batch_offset_advances_once_and_replay_is_idempotent() -> None:
    session = _Session(
        [
            _Result(_cursor_row()),
            _Result(
                [
                    {"update_id": 40, "state": "completed"},
                    {"update_id": 42, "state": "completed"},
                ]
            ),
            _Result(_cursor_row(next_offset=43, version=2, updated_at=NOW + timedelta(seconds=1))),
            _Result(_cursor_row(next_offset=43, version=2, updated_at=NOW + timedelta(seconds=1))),
        ]
    )
    repository = RuntimeCursorRepository(cast(AsyncSession, session))
    advanced = await repository.advance_control_offset(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        through_update_id=42,
        expected_version=1,
        now=NOW + timedelta(seconds=1),
    )
    replayed = await repository.advance_control_offset(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        through_update_id=42,
        expected_version=1,
        now=NOW + timedelta(seconds=2),
    )
    assert advanced is not None
    assert advanced.next_offset == 43
    assert advanced.version == 2
    assert replayed == advanced
    assert len(session.statements) == 4

    blocked = RuntimeCursorRepository(
        cast(
            AsyncSession,
            _Session(
                [
                    _Result(_cursor_row()),
                    _Result(
                        [
                            {"update_id": 40, "state": "completed"},
                            {"update_id": 42, "state": "claimed"},
                        ]
                    ),
                ]
            ),
        )
    )
    assert (
        await blocked.advance_control_offset(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            through_update_id=42,
            expected_version=1,
            now=NOW,
        )
        is None
    )


@pytest.mark.unit
async def test_watermark_create_exact_replay_monotonic_update_and_cas() -> None:
    created_row = _watermark_row()
    created_repository = RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(None), _Result(created_row)]))
    )
    created = await created_repository.record_durable_ingest(
        account_id=ACCOUNT,
        scope="account",
        pts=10,
        pts_count=1,
        update_identity="UpdateNewMessage:10",
        durable_ingested_at=NOW,
        expected_version=None,
    )
    assert created is not None
    assert created.version == 1

    replay_repository = RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(created_row)]))
    )
    replay = await replay_repository.record_durable_ingest(
        account_id=ACCOUNT,
        scope="account",
        pts=10,
        pts_count=1,
        update_identity="UpdateNewMessage:10",
        durable_ingested_at=NOW + timedelta(seconds=1),
        expected_version=1,
    )
    assert replay == created

    updated_row = _watermark_row(
        pts=12,
        pts_count=2,
        update_identity="UpdatesCombined:12",
        durable_ingested_at=NOW + timedelta(seconds=2),
        updated_at=NOW + timedelta(seconds=2),
        version=2,
    )
    update_repository = RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(created_row), _Result(updated_row)]))
    )
    updated = await update_repository.record_durable_ingest(
        account_id=ACCOUNT,
        scope="account",
        pts=12,
        pts_count=2,
        update_identity="UpdatesCombined:12",
        durable_ingested_at=NOW + timedelta(seconds=2),
        expected_version=1,
    )
    assert updated is not None
    assert updated.pts == 12
    assert updated.version == 2

    stale_repository = RuntimeCursorRepository(cast(AsyncSession, _Session([_Result(created_row)])))
    assert (
        await stale_repository.record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=12,
            pts_count=2,
            update_identity="UpdatesCombined:12",
            durable_ingested_at=NOW + timedelta(seconds=2),
            expected_version=2,
        )
        is None
    )


@pytest.mark.unit
async def test_watermark_rejects_old_or_conflicting_same_pts() -> None:
    for pts, pts_count, identity, match in (
        (9, 1, "UpdateNewMessage:9", "backwards"),
        (10, 2, "UpdateNewMessage:10", "conflicts"),
        (10, 1, "UpdateEditMessage:10", "conflicts"),
    ):
        repository = RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(_watermark_row())]))
        )
        with pytest.raises(ValueError, match=match):
            await repository.record_durable_ingest(
                account_id=ACCOUNT,
                scope="account",
                pts=pts,
                pts_count=pts_count,
                update_identity=identity,
                durable_ingested_at=NOW + timedelta(seconds=1),
                expected_version=1,
            )


@pytest.mark.unit
def test_runtime_cursor_contract_rejects_invalid_identity_and_versions() -> None:
    invalid_cursors: tuple[tuple[dict[str, Any], str], ...] = (
        ({"deployment_id": cast(str, object())}, "deployment id"),
        ({"deployment_id": "AB"}, "deployment id"),
        ({"bot_user_id": cast(int, object())}, "bot user id"),
        ({"bot_user_id": 0}, "bot user id"),
        ({"bot_user_id": 2**63}, "bot user id"),
        ({"next_offset": cast(int, object())}, "cursor values"),
        ({"next_offset": -1}, "cursor values"),
        ({"version": 0}, "cursor values"),
    )
    for changes, match in invalid_cursors:
        values: dict[str, Any] = {
            "deployment_id": "prod-primary",
            "bot_user_id": 7000000002,
            "next_offset": 0,
            "version": 1,
            "updated_at": NOW,
        }
        values.update(changes)
        with pytest.raises(ValueError, match=match):
            ControlBotCursor(**values)

    valid_receipt = {
        **_receipt_row(),
        "state": ControlUpdateState.CLAIMED,
        "disposition": None,
        "send_state": ControlUpdateSendState.NOT_REQUIRED,
    }
    invalid_receipts: tuple[tuple[dict[str, Any], str], ...] = (
        ({"update_id": cast(int, object())}, "update id"),
        ({"update_id": -1}, "update id"),
        ({"state": "unknown"}, "stable vocabulary"),
        ({"disposition": "unknown"}, "stable vocabulary"),
        ({"send_state": "unknown"}, "stable vocabulary"),
        ({"owner_instance_id": cast(UUID, object())}, "owner instance"),
        ({"owner_instance_id": UUID(int=0)}, "owner instance"),
        ({"claimed_at": NOW + timedelta(seconds=1), "lease_expires_at": NOW}, "timestamps"),
        ({"disposition": ControlUpdateDisposition.HANDLED}, "terminal fields"),
        ({"completed_at": NOW}, "terminal fields"),
        ({"send_state": ControlUpdateSendState.PENDING}, "terminal fields"),
        (
            {
                "state": ControlUpdateState.COMPLETED,
                "disposition": None,
                "completed_at": NOW,
            },
            "missing terminal fields",
        ),
        (
            {
                "state": ControlUpdateState.COMPLETED,
                "disposition": ControlUpdateDisposition.HANDLED,
                "completed_at": None,
            },
            "missing terminal fields",
        ),
        (
            {
                "state": ControlUpdateState.COMPLETED,
                "disposition": ControlUpdateDisposition.HANDLED,
                "completed_at": NOW - timedelta(seconds=1),
            },
            "missing terminal fields",
        ),
        ({"attempt_count": 0}, "versions must be positive"),
        ({"version": 0}, "versions must be positive"),
    )
    for changes, match in invalid_receipts:
        row = {**valid_receipt, **changes}
        with pytest.raises((TypeError, ValueError), match=match):
            ControlUpdateReceipt(
                deployment_id=cast(str, row["deployment_id"]),
                bot_user_id=cast(int, row["bot_user_id"]),
                update_id=cast(int, row["update_id"]),
                state=cast(ControlUpdateState, row["state"]),
                disposition=cast(ControlUpdateDisposition | None, row["disposition"]),
                send_state=cast(ControlUpdateSendState, row["send_state"]),
                owner_instance_id=cast(UUID, row["owner_instance_id"]),
                claimed_at=cast(datetime, row["claimed_at"]),
                lease_expires_at=cast(datetime, row["lease_expires_at"]),
                completed_at=cast(datetime | None, row["completed_at"]),
                attempt_count=cast(int, row["attempt_count"]),
                version=cast(int, row["version"]),
            )


@pytest.mark.unit
def test_runtime_claim_and_watermark_contract_reject_invalid_shapes() -> None:
    valid_receipt = ControlUpdateReceipt(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        state=ControlUpdateState.CLAIMED,
        disposition=None,
        send_state=ControlUpdateSendState.NOT_REQUIRED,
        owner_instance_id=OWNER,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
        completed_at=None,
        attempt_count=1,
        version=1,
    )
    with pytest.raises(TypeError, match="stable vocabulary"):
        ControlUpdateClaim(cast(ControlUpdateClaimOutcome, object()), valid_receipt)
    with pytest.raises(ValueError, match="below-offset"):
        ControlUpdateClaim(ControlUpdateClaimOutcome.BELOW_OFFSET, valid_receipt)
    with pytest.raises(ValueError, match="requires a receipt"):
        ControlUpdateClaim(ControlUpdateClaimOutcome.ACQUIRED, None)
    assert not ControlUpdateClaim(ControlUpdateClaimOutcome.BUSY, valid_receipt).acquired

    invalid_watermarks: tuple[tuple[dict[str, Any], str], ...] = (
        ({"account_id": cast(UUID, object())}, "account id"),
        ({"account_id": UUID(int=0)}, "account id"),
        ({"scope": cast(str, object())}, "scope"),
        ({"scope": "channel:0"}, "scope"),
        ({"pts": cast(int, object())}, "pts is invalid"),
        ({"pts": -1}, "pts is invalid"),
        ({"pts_count": cast(int, object())}, "pts count"),
        ({"pts_count": -1}, "pts count"),
        ({"update_identity": cast(str, object())}, "update identity"),
        ({"update_identity": "1-invalid"}, "update identity"),
        ({"version": 0}, "version must be positive"),
    )
    for changes, match in invalid_watermarks:
        values: dict[str, Any] = {
            "account_id": ACCOUNT,
            "scope": "account",
            "pts": 10,
            "pts_count": 1,
            "update_identity": "UpdateNewMessage:10",
            "durable_ingested_at": NOW,
            "version": 1,
            "updated_at": NOW,
        }
        values.update(changes)
        with pytest.raises(ValueError, match=match):
            TelegramIngestWatermark(**values)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_repository_cursor_and_claim_fail_closed_edges() -> None:
    empty = RuntimeCursorRepository(cast(AsyncSession, _Session([_Result(None)])))
    assert (
        await empty.load_control_cursor(deployment_id="prod-primary", bot_user_id=7000000002)
        is None
    )

    with pytest.raises(RuntimeError, match="not observable"):
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(), _Result(None)]))
        ).get_or_create_control_cursor(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            initial_offset=0,
            now=NOW,
        )

    for update_id in (cast(int, object()), -1):
        with pytest.raises(ValueError, match="control update id"):
            await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).claim_control_update(
                deployment_id="prod-primary",
                bot_user_id=7000000002,
                update_id=update_id,
                owner_instance_id=OWNER,
                now=NOW,
                lease_expires_at=NOW + timedelta(seconds=30),
            )

    for owner in (cast(UUID, object()), UUID(int=0)):
        with pytest.raises(ValueError, match="owner instance"):
            await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).claim_control_update(
                deployment_id="prod-primary",
                bot_user_id=7000000002,
                update_id=42,
                owner_instance_id=owner,
                now=NOW,
                lease_expires_at=NOW + timedelta(seconds=30),
            )
    with pytest.raises(ValueError, match="lease"):
        await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).claim_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            now=NOW,
            lease_expires_at=NOW,
        )

    with pytest.raises(RuntimeError, match="initialized"):
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(None)]))
        ).claim_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=30),
        )

    with pytest.raises(RuntimeError, match="compare-and-set"):
        await RuntimeCursorRepository(
            cast(
                AsyncSession,
                _Session(
                    [
                        _Result(_cursor_row()),
                        _Result(),
                        _Result(
                            _receipt_row(
                                owner_instance_id=UUID(int=11),
                                claimed_at=NOW - timedelta(minutes=2),
                                lease_expires_at=NOW - timedelta(seconds=1),
                            )
                        ),
                        _Result(None),
                    ]
                ),
            )
        ).claim_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=30),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_repository_completion_and_offset_edges() -> None:
    repository = RuntimeCursorRepository(cast(AsyncSession, _Session([])))
    with pytest.raises(TypeError, match="stable vocabulary"):
        await repository.complete_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            expected_version=1,
            disposition=cast(ControlUpdateDisposition, object()),
            response_required=True,
            now=NOW,
        )
    with pytest.raises(TypeError, match="boolean"):
        await repository.complete_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            expected_version=1,
            disposition=ControlUpdateDisposition.HANDLED,
            response_required=cast(bool, object()),
            now=NOW,
        )

    completed_not_required = _receipt_row(
        state="completed",
        disposition="handled",
        send_state="not_required",
        completed_at=NOW,
        version=2,
    )
    result = await RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(completed_not_required)]))
    ).complete_control_update(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        update_id=42,
        owner_instance_id=OWNER,
        expected_version=1,
        disposition=ControlUpdateDisposition.HANDLED,
        response_required=False,
        now=NOW,
    )
    assert result is not None
    assert result.send_state is ControlUpdateSendState.NOT_REQUIRED
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(None)]))
        ).complete_control_update(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            owner_instance_id=OWNER,
            expected_version=1,
            disposition=ControlUpdateDisposition.HANDLED,
            response_required=False,
            now=NOW,
        )
    ) is None

    with pytest.raises(ValueError, match="terminal state"):
        await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).mark_control_response(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            update_id=42,
            expected_version=1,
            send_state=ControlUpdateSendState.PENDING,
        )
    with pytest.raises(ValueError, match="control update id"):
        await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).advance_control_offset(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            through_update_id=-1,
            expected_version=1,
            now=NOW,
        )
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(None)]))
        ).advance_control_offset(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            through_update_id=42,
            expected_version=1,
            now=NOW,
        )
    ) is None
    current = _cursor_row(next_offset=43)
    replay = await RuntimeCursorRepository(
        cast(AsyncSession, _Session([_Result(current)]))
    ).advance_control_offset(
        deployment_id="prod-primary",
        bot_user_id=7000000002,
        through_update_id=42,
        expected_version=1,
        now=NOW,
    )
    assert replay is not None
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(_cursor_row())]))
        ).advance_control_offset(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            through_update_id=42,
            expected_version=2,
            now=NOW,
        )
    ) is None
    for receipt_rows in ([], [{"update_id": 41, "state": "completed"}]):
        assert (
            await RuntimeCursorRepository(
                cast(AsyncSession, _Session([_Result(_cursor_row()), _Result(receipt_rows)]))
            ).advance_control_offset(
                deployment_id="prod-primary",
                bot_user_id=7000000002,
                through_update_id=42,
                expected_version=1,
                now=NOW,
            )
        ) is None
    assert (
        await RuntimeCursorRepository(
            cast(
                AsyncSession,
                _Session(
                    [
                        _Result(_cursor_row()),
                        _Result([{"update_id": 42, "state": "completed"}]),
                        _Result(None),
                    ]
                ),
            )
        ).advance_control_offset(
            deployment_id="prod-primary",
            bot_user_id=7000000002,
            through_update_id=42,
            expected_version=1,
            now=NOW,
        )
    ) is None
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(None)]))
        ).load_ingest_watermark(
            account_id=ACCOUNT,
            scope="account",
        )
    ) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_repository_ingest_race_and_cas_edges() -> None:
    for expected in (0, cast(int, object())):
        with pytest.raises(ValueError, match="expected version"):
            await RuntimeCursorRepository(cast(AsyncSession, _Session([]))).record_durable_ingest(
                account_id=ACCOUNT,
                scope="account",
                pts=10,
                pts_count=1,
                update_identity="UpdateNewMessage:10",
                durable_ingested_at=NOW,
                expected_version=expected,
            )
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(None)]))
        ).record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=10,
            pts_count=1,
            update_identity="UpdateNewMessage:10",
            durable_ingested_at=NOW,
            expected_version=1,
        )
    ) is None

    raced = _watermark_row()
    for raced_row, should_match in (
        (raced, True),
        (_watermark_row(pts=11, update_identity="UpdateNewMessage:11"), False),
    ):
        session = _Session([_Result(None), _Result(None), _Result(raced_row)])
        result = await RuntimeCursorRepository(cast(AsyncSession, session)).record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=10,
            pts_count=1,
            update_identity="UpdateNewMessage:10",
            durable_ingested_at=NOW,
            expected_version=None,
        )
        assert (result is not None) is should_match

    with pytest.raises(ValueError, match="time cannot move backwards"):
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(_watermark_row(pts=9))]))
        ).record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=10,
            pts_count=1,
            update_identity="UpdateNewMessage:10",
            durable_ingested_at=NOW - timedelta(seconds=1),
            expected_version=1,
        )
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(_watermark_row())]))
        ).record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=12,
            pts_count=1,
            update_identity="UpdateNewMessage:12",
            durable_ingested_at=NOW,
            expected_version=2,
        )
    ) is None
    assert (
        await RuntimeCursorRepository(
            cast(AsyncSession, _Session([_Result(_watermark_row()), _Result(None)]))
        ).record_durable_ingest(
            account_id=ACCOUNT,
            scope="account",
            pts=12,
            pts_count=1,
            update_identity="UpdateNewMessage:12",
            durable_ingested_at=NOW,
            expected_version=1,
        )
    ) is None
