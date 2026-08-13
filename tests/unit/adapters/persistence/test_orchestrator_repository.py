from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

import telegram_userbot.adapters.persistence.orchestrator_repository as repository_module
from telegram_userbot.adapters.persistence.orchestrator_records import RunResult
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
    OrchestratorConflictError,
    _run,
    _turn,
)
from telegram_userbot.domain.conversation import (
    AccountControl,
    BaseMode,
    ConversationControl,
    MaintenanceState,
    WorkSnapshot,
    resolve_mode,
)
from telegram_userbot.domain.messaging import EventKind

NOW = datetime(2030, 2, 3, 4, 5, 6, tzinfo=UTC)
ACCOUNT = UUID(int=1)
CONVERSATION = UUID(int=2)
TURN = UUID(int=3)
RUN = UUID(int=4)
OWNER = UUID(int=5)


class FakeResult:
    def __init__(
        self,
        row: Any = None,
        *,
        rowcount: int = 0,
        scalar_rows: tuple[object, ...] = (),
        rows: tuple[object, ...] = (),
    ) -> None:
        self.row = row
        self.rowcount = rowcount
        self.scalar_rows = scalar_rows
        self.rows = rows

    def mappings(self) -> Self:
        return self

    def one(self) -> Any:
        assert self.row is not None
        return self.row

    def one_or_none(self) -> Any:
        return self.row

    def scalars(self) -> tuple[object, ...]:
        return self.scalar_rows

    def all(self) -> tuple[object, ...]:
        return self.rows

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeSession:
    def __init__(
        self,
        *,
        scalars: tuple[object, ...] = (),
        results: tuple[FakeResult, ...] = (),
    ) -> None:
        self.scalars = deque(scalars)
        self.results = deque(results)
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalars.popleft() if self.scalars else None

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.popleft() if self.results else FakeResult()


def session(fake: FakeSession) -> AsyncSession:
    return cast(AsyncSession, fake)


def resolution(
    *,
    mode: BaseMode = BaseMode.AUTO,
    revision: int = 7,
    floor: int | None = None,
    coverage: int | None = None,
) -> object:
    return resolve_mode(
        account=AccountControl(mode, False, MaintenanceState.INACTIVE, 5),
        conversation=ConversationControl(
            None,
            False,
            None,
            6,
            revision,
            floor,
            coverage,
        ),
        now=NOW,
    )


def scope(
    *,
    mode: BaseMode = BaseMode.AUTO,
    revision: int = 7,
    floor: int | None = None,
    coverage: int | None = None,
) -> object:
    return SimpleNamespace(
        account={
            "default_base_mode": mode,
            "global_paused": False,
            "maintenance_state": "inactive",
            "control_version": 5,
            "temporary_takeover_enabled": False,
            "temporary_takeover_seconds": 600,
        },
        conversation={
            "id": CONVERSATION,
            "account_id": ACCOUNT,
            "mode_version": 6,
            "content_revision": revision,
            "base_mode_override": None,
            "contact_paused": False,
            "temporary_human_until": None,
            "automation_resume_floor_event_id": floor,
            "last_response_covered_event_id": coverage,
        },
        contact={},
        resolution=resolution(
            mode=mode,
            revision=revision,
            floor=floor,
            coverage=coverage,
        ),
    )


def turn_row(*, state: str = "collecting", trigger: str = "incoming") -> dict[str, object]:
    return {
        "id": TURN,
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "state": state,
        "trigger_kind": trigger,
        "collection_sequence": 1,
        "active_generation_no": 1 if state in {"generating", "output_ready"} else 0,
        "account_control_version_snapshot": 5,
        "mode_version_snapshot": 6,
        "content_revision_snapshot": 7,
        "quiet_deadline_at": NOW,
        "hard_deadline_at": NOW + timedelta(seconds=7),
        "collect_started_at": NOW - timedelta(seconds=3),
        "created_at": NOW - timedelta(seconds=3),
        "lease_owner": OWNER if state in {"generating", "output_ready"} else None,
        "lease_expires_at": NOW + timedelta(seconds=30)
        if state in {"generating", "output_ready"}
        else None,
        "fencing_token": 1,
    }


def run_row(*, state: str = "running", trigger: str = "incoming") -> dict[str, object]:
    return {
        "id": RUN,
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "turn_id": TURN,
        "state": state,
        "generation_no": 1,
        "model_profile_id": UUID(int=10),
        "config_version_id": UUID(int=11),
        "credential_version_id": UUID(int=12),
        "input_fingerprint": b"i" * 32,
        "account_control_version_snapshot": 5,
        "mode_version_snapshot": 6,
        "content_revision_snapshot": 7,
        "started_at": NOW,
        "logical_role": "main_ai",
        "purpose": ("copilot_reactive_draft" if trigger == "copilot" else "conversation_reply"),
        "trigger_kind": trigger,
    }


@pytest.mark.unit
def test_record_converters_reject_missing_start_and_normalize_generation() -> None:
    converted_turn = _turn(cast(RowMapping, turn_row()))
    assert converted_turn.snapshot == WorkSnapshot(5, 6, 7, 1)
    converted_run = _run(cast(RowMapping, run_row()), "incoming")
    assert converted_run.trigger_kind == "incoming"
    missing = run_row()
    missing["started_at"] = None
    with pytest.raises(RuntimeError, match="no start time"):
        _run(cast(RowMapping, missing), "incoming")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversation_activity_orders_drafts_by_requested_time() -> None:
    fake = FakeSession(scalars=(0, None, "ready"))
    repository = ConversationOrchestratorRepository(session(fake))
    repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]

    activity = await repository.conversation_activity(CONVERSATION, NOW)

    assert activity.unanswered_count == 0
    assert activity.active_turn_state is None
    assert activity.active_draft_state == "ready"
    draft_query = str(fake.statements[-1])
    assert "copilot_drafts.requested_at DESC" in draft_query


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_resolution_bootstrap_and_missing_conversation() -> None:
    account_row = {
        "account_id": ACCOUNT,
        "default_base_mode": "AUTO",
        "global_paused": False,
        "maintenance_state": "inactive",
        "control_version": 5,
        "resume_floor_event_id": 10,
    }
    conversation = {
        "id": CONVERSATION,
        "account_id": ACCOUNT,
        "account_status": "active",
        "automation_status": "allowed",
        "base_mode_override": None,
        "contact_paused": False,
        "temporary_human_until": None,
        "mode_version": 6,
        "content_revision": 7,
        "automation_resume_floor_event_id": 12,
        "last_response_covered_event_id": 11,
    }
    fake = FakeSession(
        scalars=(ACCOUNT, "MAIN_AI_UNAVAILABLE"),
        results=(FakeResult(), FakeResult(account_row), FakeResult(conversation)),
    )
    resolved = await ConversationOrchestratorRepository(session(fake)).resolve(CONVERSATION, NOW)
    assert resolved.block_reason == "MAIN_AI_UNAVAILABLE"
    assert resolved.automation_resume_floor_event_id == 12
    assert len(fake.statements) == 5

    with pytest.raises(OrchestratorConflictError, match="CONVERSATION_NOT_FOUND"):
        await ConversationOrchestratorRepository(session(FakeSession(scalars=(None,)))).resolve(
            CONVERSATION, NOW
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalidation_cancels_only_pre_send_work() -> None:
    fake = FakeSession(
        results=(
            FakeResult(scalar_rows=(RUN,)),
            FakeResult(),
            FakeResult(scalar_rows=(UUID(int=30),)),
            FakeResult(),
            FakeResult(),
        )
    )
    changed = await ConversationOrchestratorRepository(session(fake))._invalidate_pre_send(
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        now=NOW,
        reason="MODE_CHANGED",
    )
    assert changed
    assert len(fake.statements) == 5

    no_work = FakeSession(results=(FakeResult(), FakeResult(), FakeResult(), FakeResult()))
    assert not await ConversationOrchestratorRepository(session(no_work))._invalidate_pre_send(
        account_id=ACCOUNT, conversation_id=None, now=NOW, reason="GLOBAL_PAUSE"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_and_conversation_control_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    account_row = {
        "default_base_mode": "HUMAN",
        "global_paused": False,
        "maintenance_state": "inactive",
        "control_version": 2,
        "resume_floor_event_id": None,
    }
    fake = FakeSession(results=(FakeResult(account_row), FakeResult(), FakeResult(), FakeResult()))
    repository = ConversationOrchestratorRepository(session(fake))
    repository._ensure_account_state = AsyncMock()  # type: ignore[method-assign]
    repository._latest_event_id = AsyncMock(return_value=44)  # type: ignore[method-assign]
    repository._invalidate_pre_send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    changed, version = await repository.set_account_control(
        account_id=ACCOUNT,
        actor_ref="admin:1",
        now=NOW,
        expected_version=2,
        default_base_mode=BaseMode.AUTO,
    )
    assert changed
    assert version == 3
    assert len(fake.statements) == 3

    conflict = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(account_row),)))
    )
    conflict._ensure_account_state = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="CONTROL_VERSION_CONFLICT"):
        await conflict.set_account_control(
            account_id=ACCOUNT,
            actor_ref="admin:1",
            now=NOW,
            expected_version=1,
        )

    conversation_fake = FakeSession(results=(FakeResult(), FakeResult()))
    conversation_repository = ConversationOrchestratorRepository(session(conversation_fake))
    conversation_repository._locked_scope = AsyncMock(  # type: ignore[method-assign]
        side_effect=(scope(mode=BaseMode.HUMAN), scope())
    )
    conversation_repository._latest_event_id = AsyncMock(return_value=45)  # type: ignore[method-assign]
    conversation_repository._invalidate_pre_send = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    result = await conversation_repository.set_conversation_control(
        conversation_id=CONVERSATION,
        actor_ref="admin:1",
        now=NOW,
        expected_version=6,
        base_mode_override=BaseMode.AUTO,
    )
    assert result.changed
    assert result.cancelled_work

    no_change = ConversationOrchestratorRepository(session(FakeSession()))
    no_change._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    unchanged = await no_change.set_conversation_control(
        conversation_id=CONVERSATION,
        actor_ref="admin:1",
        now=NOW,
        expected_version=6,
    )
    assert unchanged.result_code == "NO_CHANGE"

    unchanged_account = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(account_row),)))
    )
    unchanged_account._ensure_account_state = AsyncMock()  # type: ignore[method-assign]
    assert await unchanged_account.set_account_control(
        account_id=ACCOUNT,
        actor_ref="admin:1",
        now=NOW,
        expected_version=2,
    ) == (False, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_create_pending_and_seal_turn() -> None:
    message = {
        "id": UUID(int=20),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "current_revision_no": 1,
        "telegram_message_id": 42,
        "source_event_id": 13,
    }
    created = turn_row()
    fake = FakeSession(
        scalars=(0, None, 0),
        results=(
            FakeResult(None),
            FakeResult(),
            FakeResult(created),
            FakeResult(),
            FakeResult(created),
        ),
    )
    repository = ConversationOrchestratorRepository(session(fake))
    repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    repository._eligible_message = AsyncMock(return_value=message)  # type: ignore[method-assign]
    collected = await repository.collect_message(
        conversation_id=CONVERSATION,
        message_id=cast(UUID, message["id"]),
        observed_at=NOW,
    )
    assert collected is not None
    assert collected.state == "collecting"

    pending_fake = FakeSession(
        scalars=(None, 0),
        results=(FakeResult(), FakeResult(), FakeResult(turn_row())),
    )
    pending_repository = ConversationOrchestratorRepository(session(pending_fake))
    pending_repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    pending_repository._pending_messages = AsyncMock(return_value=(message,))  # type: ignore[method-assign]
    pending = await pending_repository.create_pending_turn(
        conversation_id=CONVERSATION,
        trigger_kind="manual_pending_reply",
        now=NOW,
        ignore_resume_floor=True,
    )
    assert pending.trigger_kind == "incoming" or pending.id == TURN

    ready = turn_row(state="ready")
    seal_fake = FakeSession(
        scalars=(CONVERSATION,),
        results=(FakeResult(turn_row()), FakeResult(ready)),
    )
    seal_repository = ConversationOrchestratorRepository(session(seal_fake))
    seal_repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    sealed = await seal_repository.seal_turn(turn_id=TURN, now=NOW)
    assert sealed.state == "ready"

    early = turn_row()
    early["quiet_deadline_at"] = NOW + timedelta(seconds=1)
    early_repository = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(CONVERSATION,), results=(FakeResult(early),)))
    )
    early_repository._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope()
    )
    with pytest.raises(OrchestratorConflictError, match="DEBOUNCE_NOT_DUE"):
        await early_repository.seal_turn(turn_id=TURN, now=NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_message_mode_source_floor_and_existing_membership_gates() -> None:
    message = {
        "id": UUID(int=20),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "current_revision_no": 1,
        "telegram_message_id": 42,
        "source_event_id": 13,
    }
    human = ConversationOrchestratorRepository(session(FakeSession()))
    human._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(mode=BaseMode.HUMAN)
    )
    human._eligible_message = AsyncMock()  # type: ignore[method-assign]
    assert (
        await human.collect_message(
            conversation_id=CONVERSATION,
            message_id=UUID(int=20),
            observed_at=NOW,
        )
        is None
    )
    human._eligible_message.assert_not_awaited()

    unsupported = ConversationOrchestratorRepository(session(FakeSession()))
    unsupported._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    unsupported._eligible_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert (
        await unsupported.collect_message(
            conversation_id=CONVERSATION,
            message_id=UUID(int=20),
            observed_at=NOW,
        )
        is None
    )

    floor = ConversationOrchestratorRepository(session(FakeSession()))
    floor._locked_scope = AsyncMock(return_value=scope(floor=13))  # type: ignore[method-assign]
    floor._eligible_message = AsyncMock(return_value=message)  # type: ignore[method-assign]
    assert (
        await floor.collect_message(
            conversation_id=CONVERSATION,
            message_id=UUID(int=20),
            observed_at=NOW,
        )
        is None
    )

    active = turn_row()
    active["collect_started_at"] = None
    existing_fake = FakeSession(
        scalars=(UUID(int=20),),
        results=(FakeResult(active), FakeResult(active)),
    )
    existing = ConversationOrchestratorRepository(session(existing_fake))
    existing._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    existing._eligible_message = AsyncMock(return_value=message)  # type: ignore[method-assign]
    collected = await existing.collect_message(
        conversation_id=CONVERSATION,
        message_id=UUID(int=20),
        observed_at=NOW,
    )
    assert collected is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_incoming_routes_collect_ready_and_generation_grace() -> None:
    collected_turn = _turn(cast(RowMapping, turn_row()))
    empty = ConversationOrchestratorRepository(session(FakeSession(results=(FakeResult(None),))))
    empty._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    empty.collect_message = AsyncMock(return_value=collected_turn)  # type: ignore[method-assign]
    assert (
        await empty.handle_new_incoming(
            conversation_id=CONVERSATION,
            message_id=UUID(int=20),
            observed_at=NOW,
        )
        == collected_turn
    )

    ready = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(turn_row(state="ready")), FakeResult())))
    )
    ready._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    ready.create_pending_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=collected_turn
    )
    assert (
        await ready.handle_new_incoming(
            conversation_id=CONVERSATION,
            message_id=UUID(int=21),
            observed_at=NOW,
        )
        == collected_turn
    )

    missing_run = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(turn_row(state="generating")), FakeResult(None))))
    )
    missing_run._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    assert (
        await missing_run.handle_new_incoming(
            conversation_id=CONVERSATION,
            message_id=UUID(int=22),
            observed_at=NOW,
        )
        is None
    )

    within = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(turn_row(state="generating")),
                    FakeResult(run_row()),
                )
            )
        )
    )
    within._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    assert (
        await within.handle_new_incoming(
            conversation_id=CONVERSATION,
            message_id=UUID(int=23),
            observed_at=NOW + timedelta(seconds=2),
        )
        is None
    )

    outside = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(turn_row(state="generating")),
                    FakeResult(run_row()),
                )
            )
        )
    )
    outside._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    outside._supersede = AsyncMock(  # type: ignore[method-assign]
        return_value=RunResult(RUN, "superseded", "GENERATION_GRACE_EXPIRED")
    )
    outside.create_pending_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=collected_turn
    )
    assert (
        await outside.handle_new_incoming(
            conversation_id=CONVERSATION,
            message_id=UUID(int=24),
            observed_at=NOW + timedelta(seconds=4),
        )
        == collected_turn
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_grace_compensation_skips_stale_and_supersedes_exact_incoming() -> None:
    collected_turn = _turn(cast(RowMapping, turn_row()))
    assert (
        await ConversationOrchestratorRepository(
            session(FakeSession(results=(FakeResult(rows=()),)))
        ).expire_generation_grace(now=NOW)
        == 0
    )

    candidate = run_row()
    candidate["started_at"] = NOW - timedelta(seconds=5)
    stale_run = dict(candidate)
    stale_run["state"] = "superseded"
    stale = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(rows=(candidate,)),
                    FakeResult(turn_row(state="generating")),
                    FakeResult(stale_run),
                )
            )
        )
    )
    stale._locked_scope = AsyncMock(return_value=scope(revision=8))  # type: ignore[method-assign]
    assert await stale.expire_generation_grace(now=NOW) == 0

    exact = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(rows=(candidate,)),
                    FakeResult(turn_row(state="generating")),
                    FakeResult(candidate),
                )
            )
        )
    )
    exact._locked_scope = AsyncMock(return_value=scope(revision=8))  # type: ignore[method-assign]
    exact._new_revision_events = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "event_kind": EventKind.MESSAGE_CREATED.value,
                "direction": "incoming",
                "source": "telegram_user",
            },
        )
    )
    exact._supersede = AsyncMock(  # type: ignore[method-assign]
        return_value=RunResult(RUN, "superseded", "GENERATION_GRACE_EXPIRED")
    )
    exact.create_pending_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=collected_turn
    )
    assert await exact.expire_generation_grace(now=NOW) == 1

    non_incoming = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(rows=(candidate,)),
                    FakeResult(turn_row(state="generating")),
                    FakeResult(candidate),
                )
            )
        )
    )
    non_incoming._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(revision=8)
    )
    non_incoming._new_revision_events = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "event_kind": EventKind.MESSAGE_EDITED.value,
                "direction": "incoming",
                "source": "telegram_user",
            },
        )
    )
    assert await non_incoming.expire_generation_grace(now=NOW) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_turn_and_seal_precondition_failures() -> None:
    message = {
        "id": UUID(int=20),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "current_revision_no": 1,
        "telegram_message_id": 42,
        "source_event_id": 13,
    }

    wrong_mode = ConversationOrchestratorRepository(session(FakeSession()))
    wrong_mode._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(mode=BaseMode.HUMAN)
    )
    with pytest.raises(OrchestratorConflictError, match="MODE_OR_OPERATIONAL_GATE"):
        await wrong_mode.create_pending_turn(
            conversation_id=CONVERSATION,
            trigger_kind="manual_pending_reply",
            now=NOW,
        )

    no_pending = ConversationOrchestratorRepository(session(FakeSession()))
    no_pending._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    no_pending._pending_messages = AsyncMock(return_value=())  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="NO_PENDING_SEGMENT"):
        await no_pending.create_pending_turn(
            conversation_id=CONVERSATION,
            trigger_kind="manual_pending_reply",
            now=NOW,
        )

    active = ConversationOrchestratorRepository(session(FakeSession(scalars=(TURN,))))
    active._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    active._pending_messages = AsyncMock(return_value=(message,))  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="ACTIVE_TURN_EXISTS"):
        await active.create_pending_turn(
            conversation_id=CONVERSATION,
            trigger_kind="manual_pending_reply",
            now=NOW,
        )

    with pytest.raises(OrchestratorConflictError, match="TURN_NOT_COLLECTING"):
        await ConversationOrchestratorRepository(session(FakeSession(scalars=(None,)))).seal_turn(
            turn_id=TURN, now=NOW
        )

    missing_turn = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(CONVERSATION,), results=(FakeResult(None),)))
    )
    missing_turn._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="TURN_NOT_COLLECTING"):
        await missing_turn.seal_turn(turn_id=TURN, now=NOW)

    moved = turn_row()
    moved["conversation_id"] = UUID(int=99)
    moved_turn = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(CONVERSATION,), results=(FakeResult(moved),)))
    )
    moved_turn._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="TURN_SCOPE_CHANGED"):
        await moved_turn.seal_turn(turn_id=TURN, now=NOW)

    stale_turn = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(CONVERSATION,),
                results=(FakeResult(turn_row()), FakeResult()),
            )
        )
    )
    stale_turn._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(revision=8)
    )
    with pytest.raises(OrchestratorConflictError, match="SEAL_GATE_FAILED"):
        await stale_turn.seal_turn(turn_id=TURN, now=NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_and_lease_precondition_failures() -> None:
    with pytest.raises(OrchestratorConflictError, match="TURN_NOT_READY"):
        await ConversationOrchestratorRepository(
            session(FakeSession(scalars=(None,)))
        ).start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    missing_ready = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(CONVERSATION,), results=(FakeResult(None),)))
    )
    missing_ready._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="TURN_NOT_READY"):
        await missing_ready.start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    profile = SimpleNamespace(
        profile_id=UUID(int=10),
        config_version_id=UUID(int=11),
        credential_version_id=UUID(int=12),
        config_sha256=b"c" * 32,
    )
    unavailable = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(CONVERSATION,),
                results=(FakeResult(turn_row(state="ready")),),
            )
        )
    )
    unavailable._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    unavailable._main_profile = AsyncMock(return_value=None)  # type: ignore[method-assign]
    unavailable._set_main_ai_block = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="MAIN_AI_UNAVAILABLE"):
        await unavailable.start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    moved_ready = turn_row(state="ready")
    moved_ready["conversation_id"] = UUID(int=99)
    moved_generation = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(CONVERSATION,),
                results=(FakeResult(moved_ready),),
            )
        )
    )
    moved_generation._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope()
    )
    moved_generation._main_profile = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    moved_generation._set_main_ai_block = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="TURN_SCOPE_CHANGED"):
        await moved_generation.start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    stale_generation = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(CONVERSATION,),
                results=(FakeResult(turn_row(state="ready")),),
            )
        )
    )
    stale_generation._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(revision=8)
    )
    stale_generation._main_profile = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    stale_generation._set_main_ai_block = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="GENERATION_GATE_FAILED"):
        await stale_generation.start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    empty_generation = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(CONVERSATION,),
                results=(
                    FakeResult(turn_row(state="ready")),
                    FakeResult(rows=()),
                ),
            )
        )
    )
    empty_generation._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    empty_generation._main_profile = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    empty_generation._set_main_ai_block = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(OrchestratorConflictError, match="TURN_HAS_NO_MESSAGES"):
        await empty_generation.start_generation(turn_id=TURN, owner=OWNER, now=NOW)

    lease = ConversationOrchestratorRepository(session(FakeSession()))
    with pytest.raises(ValueError, match="positive"):
        await lease.renew_generation_lease(
            run_id=RUN,
            owner=OWNER,
            now=NOW,
            lease_seconds=0,
        )
    assert not await ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(None),)))
    ).renew_generation_lease(run_id=RUN, owner=OWNER, now=NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_and_complete_auto_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SimpleNamespace(
        profile_id=UUID(int=10),
        config_version_id=UUID(int=11),
        credential_version_id=UUID(int=12),
        config_sha256=b"c" * 32,
    )
    ready = turn_row(state="ready")
    ready["active_generation_no"] = 0
    running = run_row()
    fake = FakeSession(
        scalars=(CONVERSATION,),
        results=(
            FakeResult(ready),
            FakeResult(
                rows=(
                    SimpleNamespace(
                        message_id=UUID(int=20),
                        message_revision_no=1,
                        telegram_message_id=42,
                    ),
                )
            ),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(running),
        ),
    )
    repository = ConversationOrchestratorRepository(session(fake), new_uuid=lambda: RUN)
    repository._main_profile = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    repository._set_main_ai_block = AsyncMock()  # type: ignore[method-assign]
    repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    claim = await repository.start_generation(turn_id=TURN, owner=OWNER, now=NOW)
    assert claim.run.id == RUN
    assert claim.max_telegram_message_id == 42
    assert claim.typing_lease_token == RUN

    complete_fake = FakeSession(
        results=(
            FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
            FakeResult(turn_row(state="generating")),
            FakeResult(running),
            FakeResult(),
            FakeResult(),
        )
    )
    complete = ConversationOrchestratorRepository(session(complete_fake))
    complete._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    complete._new_revision_events = AsyncMock(return_value=())  # type: ignore[method-assign]
    complete._create_group = AsyncMock(return_value=UUID(int=30))  # type: ignore[method-assign]
    complete._finish_run_row = AsyncMock()  # type: ignore[method-assign]
    result = await complete.complete_generation(
        run_id=RUN,
        owner=OWNER,
        text_output="  synthetic output\r\n  ",
        completed_at=NOW + timedelta(seconds=1),
        entropy=b"e" * 32,
    )
    assert result.delivery_group_id == UUID(int=30)
    assert result.reason == "DELIVERY_PLANNED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_lease_renewal_obeys_owner_mode_and_run_state() -> None:
    active = turn_row(state="generating")
    fake = FakeSession(
        results=(
            FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
            FakeResult(active),
            FakeResult(run_row()),
            FakeResult(),
        )
    )
    repository = ConversationOrchestratorRepository(session(fake))
    repository._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    assert await repository.renew_generation_lease(
        run_id=RUN,
        owner=OWNER,
        now=NOW + timedelta(seconds=4),
    )

    denied = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
                    FakeResult(active),
                    FakeResult(run_row()),
                )
            )
        )
    )
    denied._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(mode=BaseMode.HUMAN)
    )
    assert not await denied.renew_generation_lease(
        run_id=RUN,
        owner=OWNER,
        now=NOW + timedelta(seconds=4),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_complete_copilot_and_stale_generation() -> None:
    copilot_run = run_row(trigger="copilot")
    copilot_turn = turn_row(state="generating", trigger="copilot")
    draft = {
        "id": UUID(int=40),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "turn_id": TURN,
    }
    fake = FakeSession(
        results=(
            FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
            FakeResult(copilot_turn),
            FakeResult(copilot_run),
            FakeResult(),
            FakeResult(draft),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        )
    )
    repository = ConversationOrchestratorRepository(session(fake))
    repository._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(mode=BaseMode.COPILOT)
    )
    repository._new_revision_events = AsyncMock(return_value=())  # type: ignore[method-assign]
    repository._finish_run_row = AsyncMock()  # type: ignore[method-assign]
    result = await repository.complete_generation(
        run_id=RUN,
        owner=OWNER,
        text_output="draft",
        completed_at=NOW + timedelta(seconds=1),
        entropy=b"e" * 32,
    )
    assert result.draft_id == UUID(int=40)

    stale_fake = FakeSession(
        results=(
            FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
            FakeResult(turn_row(state="generating")),
            FakeResult(run_row()),
        )
    )
    stale = ConversationOrchestratorRepository(session(stale_fake))
    stale._locked_scope = AsyncMock(return_value=scope(revision=8))  # type: ignore[method-assign]
    stale._new_revision_events = AsyncMock(return_value=())  # type: ignore[method-assign]
    stale._supersede = AsyncMock(  # type: ignore[method-assign]
        return_value=RunResult(RUN, "superseded", "stale")
    )
    result = await stale.complete_generation(
        run_id=RUN,
        owner=OWNER,
        text_output="late",
        completed_at=NOW + timedelta(seconds=4),
        entropy=b"e" * 32,
    )
    assert result.state == "superseded"

    terminal_run = run_row(state="superseded")
    terminal_run["error_code"] = "GENERATION_GRACE_EXPIRED"
    terminal = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
                    FakeResult(turn_row(state="superseded")),
                    FakeResult(terminal_run),
                )
            )
        )
    )
    terminal._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    discarded = await terminal.complete_generation(
        run_id=RUN,
        owner=OWNER,
        text_output="late result must be discarded",
        completed_at=NOW + timedelta(seconds=5),
        entropy=b"e" * 32,
    )
    assert discarded.reason == "GENERATION_GRACE_EXPIRED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_preflight_and_operational_block(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSession(
        results=(
            FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
            FakeResult(),
            FakeResult(run_row()),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        )
    )
    failure = ConversationOrchestratorRepository(session(fake))
    failure._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    await failure.fail_generation(run_id=RUN, now=NOW, error_code="PROVIDER_ERROR")
    assert len(fake.statements) == 7

    block = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(None,), results=(FakeResult(),)))
    )
    await block._set_main_ai_block(ACCOUNT, NOW, True)
    clear = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(UUID(int=50),), results=(FakeResult(),)))
    )
    await clear._set_main_ai_block(ACCOUNT, NOW, False)

    joined = {
        "id": UUID(int=60),
        "delivery_group_id": UUID(int=30),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "model_run_id": RUN,
        "turn_id": TURN,
        "source": "ai",
        "generation_no": 1,
        "account_control_version": 5,
        "mode_version": 6,
        "content_revision": 7,
        "sequence_no": 0,
        "state": "pending",
        "group_state": "planned",
        "first_side_effect_at": None,
        "sent_count": 0,
        "turn_state": "output_ready",
        "lease_owner": OWNER,
        "lease_expires_at": NOW + timedelta(seconds=1),
        "active_generation_no": 1,
    }

    class Lifecycle:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def claim_intent(self, **kwargs: object) -> str:
            return "claimed"

    monkeypatch.setattr(repository_module, "TelegramLifecycleRepository", Lifecycle)
    preflight_fake = FakeSession(
        scalars=(None,),
        results=(
            FakeResult(
                {
                    "conversation_id": CONVERSATION,
                    "turn_id": TURN,
                    "delivery_group_id": UUID(int=30),
                }
            ),
            FakeResult(),
            FakeResult(),
            FakeResult(joined),
            FakeResult(),
        ),
    )
    preflight = ConversationOrchestratorRepository(session(preflight_fake))
    preflight._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    preflight._continuation_source_valid = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert (
        await preflight.preflight_intent(intent_id=cast(UUID, joined["id"]), owner=OWNER, now=NOW)
        == "claimed"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_remainder_cancellation_and_ordinal_wait_are_distinct() -> None:
    joined = {
        "id": UUID(int=60),
        "delivery_group_id": UUID(int=30),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "model_run_id": RUN,
        "turn_id": TURN,
        "source": "ai",
        "generation_no": 1,
        "account_control_version": 5,
        "mode_version": 6,
        "content_revision": 7,
        "sequence_no": 1,
        "state": "pending",
        "group_state": "partial",
        "first_side_effect_at": NOW - timedelta(seconds=1),
        "sent_count": 1,
        "turn_state": "output_ready",
        "lease_owner": OWNER,
        "lease_expires_at": NOW + timedelta(seconds=1),
        "active_generation_no": 1,
    }
    stale = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(
                        {
                            "conversation_id": CONVERSATION,
                            "turn_id": TURN,
                            "delivery_group_id": UUID(int=30),
                        }
                    ),
                    FakeResult(),
                    FakeResult(),
                    FakeResult(joined),
                    FakeResult(),
                    FakeResult(),
                )
            )
        )
    )
    stale._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(mode=BaseMode.HUMAN)
    )
    stale._continuation_source_valid = AsyncMock(return_value=True)  # type: ignore[method-assign]
    stale._continuation_ordinal_ready = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await stale.preflight_intent(intent_id=UUID(int=60), owner=OWNER, now=NOW) is None
    assert len(cast(FakeSession, stale._session).statements) == 6

    waiting = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(
                        {
                            "conversation_id": CONVERSATION,
                            "turn_id": TURN,
                            "delivery_group_id": UUID(int=30),
                        }
                    ),
                    FakeResult(),
                    FakeResult(),
                    FakeResult(joined),
                )
            )
        )
    )
    waiting._locked_scope = AsyncMock(return_value=scope())  # type: ignore[method-assign]
    waiting._continuation_source_valid = AsyncMock(return_value=True)  # type: ignore[method-assign]
    waiting._continuation_ordinal_ready = AsyncMock(return_value=False)  # type: ignore[method-assign]
    assert await waiting.preflight_intent(intent_id=UUID(int=60), owner=OWNER, now=NOW) is None
    assert len(cast(FakeSession, waiting._session).statements) == 4

    # first_side_effect_at is conservative: a claimed first RPC may still have
    # ended in FloodWait/transient failure. Without a durable sent chunk, the
    # retry must retain the original content-revision gate.
    unsent_retry = {**joined, "sequence_no": 0, "sent_count": 0}
    retry = ConversationOrchestratorRepository(
        session(
            FakeSession(
                results=(
                    FakeResult(
                        {
                            "conversation_id": CONVERSATION,
                            "turn_id": TURN,
                            "delivery_group_id": UUID(int=30),
                        }
                    ),
                    FakeResult(),
                    FakeResult(),
                    FakeResult(unsent_retry),
                    FakeResult(),
                    FakeResult(),
                )
            )
        )
    )
    retry._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope(revision=8)
    )
    retry._continuation_source_valid = AsyncMock(return_value=True)  # type: ignore[method-assign]
    retry._continuation_ordinal_ready = AsyncMock(return_value=True)  # type: ignore[method-assign]
    retry._exact_grace_authorized = AsyncMock(return_value=False)  # type: ignore[method-assign]
    assert await retry.preflight_intent(intent_id=UUID(int=60), owner=OWNER, now=NOW) is None
    assert len(cast(FakeSession, retry._session).statements) == 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continuation_source_and_ordinal_helpers_fail_closed() -> None:
    row = cast(
        RowMapping,
        {
            "account_id": ACCOUNT,
            "conversation_id": CONVERSATION,
            "turn_id": TURN,
            "delivery_group_id": UUID(int=30),
            "sequence_no": 2,
        },
    )
    invalid = ConversationOrchestratorRepository(session(FakeSession(scalars=(UUID(int=20),))))
    assert not await invalid._continuation_source_valid(row)

    valid = ConversationOrchestratorRepository(session(FakeSession(scalars=(None, 8, None))))
    assert await valid._continuation_source_valid(row)
    human = ConversationOrchestratorRepository(session(FakeSession(scalars=(None, 8, 9))))
    assert not await human._continuation_source_valid(row)

    ready = ConversationOrchestratorRepository(session(FakeSession(scalars=(None,))))
    blocked = ConversationOrchestratorRepository(session(FakeSession(scalars=(UUID(int=60),))))
    assert await ready._continuation_ordinal_ready(row)
    assert not await blocked._continuation_ordinal_ready(row)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_identity_memory_and_reconciliation() -> None:
    command = UUID(int=80)
    command_row = {
        "id": command,
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "bot_identity": "control-bot",
        "telegram_update_id": 9,
        "admin_telegram_user_id": 10,
        "bot_chat_id": 10,
        "command_kind": "ai",
        "idempotency_key": b"k" * 32,
        "expected_control_version": None,
        "expected_mode_version": None,
        "result_control_version": None,
        "result_mode_version": None,
        "state": "pending",
        "result_code": None,
        "result_changed": None,
        "result_payload": None,
    }
    inserted = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(command_row),)))
    )
    assert (
        await inserted.record_control_command(
            command_id=command,
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            bot_identity="control-bot",
            telegram_update_id=9,
            admin_telegram_user_id=10,
            bot_chat_id=10,
            command_kind="ai",
            idempotency_key=b"k" * 32,
            now=NOW,
        )
    ).id == command
    existing = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(None), FakeResult(command_row))))
    )
    assert (
        await existing.record_control_command(
            command_id=UUID(int=81),
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            bot_identity="control-bot",
            telegram_update_id=9,
            admin_telegram_user_id=10,
            bot_chat_id=10,
            command_kind="ai",
            idempotency_key=b"k" * 32,
            now=NOW,
        )
    ).id == command
    invalidations = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(),)))
    )
    await invalidations.add_invalidation_outbox(
        account_id=ACCOUNT,
        aggregate_id=str(CONVERSATION),
        version=7,
        now=NOW,
    )
    control_queue = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(command_row), FakeResult(rowcount=1))))
    )
    claimed = await control_queue.claim_pending_control_command(
        account_id=ACCOUNT,
        bot_identity="control-bot",
        command_id=command,
    )
    assert claimed is not None
    assert claimed.id == command
    await control_queue.bind_control_command_versions(
        command_id=command,
        expected_control_version=2,
        expected_mode_version=3,
    )

    terminal_session = FakeSession(results=(FakeResult(rowcount=1), FakeResult()))
    terminal = ConversationOrchestratorRepository(session(terminal_session))
    await terminal.finish_control_command(
        command_id=command,
        result_code="CHANGED",
        accepted=True,
        result_changed=True,
        result_control_version=2,
        result_mode_version=4,
        now=NOW,
    )
    terminal_statement = cast(Update, terminal_session.statements[0])
    assert "result_payload" not in terminal_statement.compile().params

    status_session = FakeSession(results=(FakeResult(rowcount=1),))
    status_terminal = ConversationOrchestratorRepository(session(status_session))
    await status_terminal.finish_control_command(
        command_id=command,
        result_code="STATUS",
        accepted=True,
        result_changed=False,
        result_control_version=2,
        result_mode_version=4,
        result_payload={"target_label": "synthetic"},
        now=NOW,
    )
    status_statement = cast(Update, status_session.statements[0])
    assert status_statement.compile().params["result_payload"] == {"target_label": "synthetic"}

    await terminal.add_control_command_outbox(
        command_id=command,
        account_id=ACCOUNT,
        topic="control.command.completed",
        now=NOW,
    )
    with pytest.raises(ValueError, match="unsupported"):
        await terminal.add_control_command_outbox(
            command_id=command,
            account_id=ACCOUNT,
            topic="control.command.secret",
            now=NOW,
        )

    missing_terminal = ConversationOrchestratorRepository(
        session(FakeSession(results=(FakeResult(rowcount=0),)))
    )
    with pytest.raises(OrchestratorConflictError, match="NOT_PENDING"):
        await missing_terminal.finish_control_command(
            command_id=command,
            result_code="CHANGED",
            accepted=True,
            result_changed=True,
            now=NOW,
        )

    none = ConversationOrchestratorRepository(session(FakeSession(scalars=(None,))))
    await none.queue_memory_refresh(turn_id=TURN, now=NOW)
    queued = ConversationOrchestratorRepository(
        session(FakeSession(scalars=(ACCOUNT, UUID(int=90)), results=(FakeResult(),)))
    )
    await queued.queue_memory_refresh(turn_id=TURN, now=NOW)

    group = {
        "id": UUID(int=30),
        "turn_id": TURN,
        "source": "ai",
        "copilot_draft_id": None,
    }
    reconciliation = ConversationOrchestratorRepository(
        session(
            FakeSession(
                scalars=(UUID(int=30), 13, ACCOUNT, UUID(int=90)),
                results=(
                    FakeResult({"conversation_id": CONVERSATION, "turn_id": TURN}),
                    FakeResult(),
                    FakeResult(group),
                    FakeResult((1, 1)),
                    FakeResult(),
                    FakeResult(),
                    FakeResult(),
                ),
            )
        )
    )
    reconciliation._locked_scope = AsyncMock(  # type: ignore[method-assign]
        return_value=scope()
    )
    reconciliation.create_pending_turn = AsyncMock(  # type: ignore[method-assign]
        side_effect=OrchestratorConflictError("NO_PENDING_SEGMENT")
    )
    assert await reconciliation.reconcile_completed_delivery(
        conversation_id=CONVERSATION, telegram_message_id=77, now=NOW
    )
