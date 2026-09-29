from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)
from telegram_userbot.domain.messaging import OutboundIntentState

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID(int=1)
INTENT_ID = UUID(int=2)
GROUP_ID = UUID(int=3)


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        scalar_rows: tuple[object, ...] = (),
        rowcount: int = 1,
    ) -> None:
        self.row = row
        self.scalar_rows = scalar_rows
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row

    def scalars(self) -> tuple[object, ...]:
        return self.scalar_rows


class _Session:
    def __init__(self, results: tuple[_Result, ...]) -> None:
        self.results = deque(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return self.results.popleft()


def _session(*results: _Result) -> tuple[AsyncSession, _Session]:
    fake = _Session(tuple(results))
    return cast(AsyncSession, fake), fake


def _intent_row(*, state: str, message_id: int | None = None) -> dict[str, object]:
    return {
        "id": INTENT_ID,
        "delivery_group_id": GROUP_ID,
        "account_id": ACCOUNT_ID,
        "state": state,
        "telegram_message_id": message_id,
    }


def _refresh_results() -> tuple[_Result, _Result]:
    return (
        _Result(scalar_rows=(OutboundIntentState.SENT,)),
        _Result(rowcount=1),
    )


@pytest.mark.unit
async def test_reconcile_unmatched_and_idempotent_sent_mapping() -> None:
    sql_session, fake = _session(_Result())
    repository = TelegramLifecycleRepository(sql_session)
    assert (
        await repository.reconcile_outbound_message_id(
            account_id=ACCOUNT_ID,
            telegram_random_id=100,
            telegram_message_id=200,
            now=NOW,
        )
        == "unmatched"
    )
    assert len(fake.statements) == 1

    sql_session, _ = _session(_Result(_intent_row(state="sent", message_id=200)))
    assert (
        await TelegramLifecycleRepository(sql_session).reconcile_outbound_message_id(
            account_id=ACCOUNT_ID,
            telegram_random_id=100,
            telegram_message_id=200,
            now=NOW,
        )
        == "already_reconciled"
    )


@pytest.mark.unit
async def test_reconcile_conflicting_sent_mapping_fails_closed() -> None:
    sql_session, _ = _session(_Result(_intent_row(state="sent", message_id=201)))
    with pytest.raises(RuntimeError, match="mapping conflict"):
        await TelegramLifecycleRepository(sql_session).reconcile_outbound_message_id(
            account_id=ACCOUNT_ID,
            telegram_random_id=100,
            telegram_message_id=200,
            now=NOW,
        )


@pytest.mark.unit
@pytest.mark.parametrize("attempt_state", ["started", "unknown", "permanent"])
async def test_reconcile_marks_intent_and_group_sent_without_rewriting_terminal_history(
    attempt_state: str,
) -> None:
    results = [
        _Result(_intent_row(state="unknown")),
        _Result({"attempt_no": 2, "state": attempt_state}),
        _Result(rowcount=1),
    ]
    if attempt_state == "started":
        results.append(_Result(rowcount=1))
    results.extend(_refresh_results())
    sql_session, fake = _session(*results)

    status = await TelegramLifecycleRepository(sql_session).reconcile_outbound_message_id(
        account_id=ACCOUNT_ID,
        telegram_random_id=100,
        telegram_message_id=200,
        now=NOW,
    )
    assert status == "reconciled"
    assert len(fake.statements) == (6 if attempt_state == "started" else 5)
    intent_update = str(fake.statements[2])
    assert "telegram_message_id" in intent_update
    if attempt_state != "started":
        statements = " ".join(str(statement) for statement in fake.statements[2:])
        assert "outbound_attempts" not in statements


@pytest.mark.unit
async def test_reconcile_requires_an_attempt_and_valid_positive_ids() -> None:
    sql_session, _ = _session(_Result(_intent_row(state="retry_wait")), _Result())
    with pytest.raises(RuntimeError, match="no send attempt"):
        await TelegramLifecycleRepository(sql_session).reconcile_outbound_message_id(
            account_id=ACCOUNT_ID,
            telegram_random_id=100,
            telegram_message_id=200,
            now=NOW,
        )

    repository = TelegramLifecycleRepository(cast(AsyncSession, _Session(())))
    for random_id, message_id in ((0, 1), (1, 0), (True, 1), (1, False)):
        with pytest.raises(ValueError, match="message-id mapping"):
            await repository.reconcile_outbound_message_id(
                account_id=ACCOUNT_ID,
                telegram_random_id=random_id,
                telegram_message_id=message_id,
                now=NOW,
            )


@pytest.mark.unit
async def test_claim_does_not_blindly_resend_unknown_intent() -> None:
    # The real SQL predicate excludes UNKNOWN before the row reaches the mapper.
    sql_session, fake = _session(_Result())
    claimed = await TelegramLifecycleRepository(sql_session).claim_intent(
        account_id=ACCOUNT_ID,
        intent_id=INTENT_ID,
        now=NOW,
    )
    assert claimed is None
    assert len(fake.statements) == 1
    assert "unknown" not in str(cast(Any, fake.statements[0]).compile().params.values())
