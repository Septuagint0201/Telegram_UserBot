from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.adapters.telegram_bot.durable_control as durable_module
from telegram_userbot.adapters.telegram_bot.dispatcher import (
    ControlBotDispatcher,
    DispatchOutcome,
    PreparedDispatch,
    UpdateDisposition,
)
from telegram_userbot.adapters.telegram_bot.http import BotAPIError, BotMutationState
from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.platform.health.status import RestoreGateState
from telegram_userbot.platform.runtime.cursors import (
    ControlBotCursor,
    ControlUpdateClaim,
    ControlUpdateClaimOutcome,
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
OWNER_ID = UUID("01900000-0000-7000-8000-000000000301")
OTHER_OWNER_ID = UUID("01900000-0000-7000-8000-000000000302")
DEPLOYMENT_ID = "synthetic-deployment"
BOT_USER_ID = 777


class _Transaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, repository: object, gate: object = None) -> None:
        self.repository = repository
        self.gate = gate
        self.commit_count = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def commit(self) -> None:
        self.commit_count += 1


class _SessionFactory:
    def __init__(self, sessions: Sequence[_Session]) -> None:
        self._sessions = list(sessions)

    def __call__(self) -> _Session:
        if not self._sessions:
            raise AssertionError("unexpected session allocation")
        return self._sessions.pop(0)


class _FakeRepository:
    def __init__(
        self,
        *,
        cursor: ControlBotCursor | None = None,
        claim: ControlUpdateClaim | None = None,
        completed: ControlUpdateReceipt | None = None,
        marked: ControlUpdateReceipt | None = None,
        advanced: ControlBotCursor | None = None,
    ) -> None:
        self.cursor = cursor
        self.claim = claim
        self.completed = completed
        self.marked = marked
        self.advanced = advanced
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_or_create_control_cursor(self, **kwargs: Any) -> ControlBotCursor:
        self.calls.append(("get_or_create", kwargs))
        if self.cursor is None:
            self.cursor = _cursor()
        return self.cursor

    async def load_control_cursor(self, **kwargs: Any) -> ControlBotCursor | None:
        self.calls.append(("load", kwargs))
        return self.cursor

    async def advance_control_offset(self, **kwargs: Any) -> ControlBotCursor | None:
        self.calls.append(("advance", kwargs))
        return self.advanced

    async def claim_control_update(self, **kwargs: Any) -> ControlUpdateClaim:
        self.calls.append(("claim", kwargs))
        if self.claim is None:
            raise AssertionError("claim result was not configured")
        return self.claim

    async def complete_control_update(self, **kwargs: Any) -> ControlUpdateReceipt | None:
        self.calls.append(("complete", kwargs))
        return self.completed

    async def mark_control_response(self, **kwargs: Any) -> ControlUpdateReceipt | None:
        self.calls.append(("mark", kwargs))
        return self.marked


class _FakeRuntimeCursorRepository:
    def __init__(self, session: _Session) -> None:
        self._repository = cast(_FakeRepository, session.repository)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)


class _FakeRestoreGateRepository:
    def __init__(self, session: _Session) -> None:
        self._gate = session.gate

    async def get(self, _deployment_id: str) -> Any:
        return self._gate


class _Dispatcher:
    def __init__(self, prepared: PreparedDispatch, outcome: DispatchOutcome) -> None:
        self.prepared = prepared
        self.outcome = outcome
        self.route_calls: list[tuple[int, Mapping[str, Any]]] = []
        self.deliver_calls: list[PreparedDispatch] = []

    async def route_update(self, *, update_id: int, update: Mapping[str, Any]) -> PreparedDispatch:
        self.route_calls.append((update_id, update))
        return self.prepared

    async def deliver(self, prepared: PreparedDispatch) -> DispatchOutcome:
        self.deliver_calls.append(prepared)
        return self.outcome


class _CancelDispatcher:
    async def deliver(self, _prepared: PreparedDispatch) -> DispatchOutcome:
        raise asyncio.CancelledError


class _RecordingMarkExecutor(durable_module.DurableControlUpdateExecutor):
    def __init__(self) -> None:
        super().__init__(
            sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory([])),
            dispatcher_factory=cast(Any, object()),
            deployment_id=DEPLOYMENT_ID,
            bot_user_id=BOT_USER_ID,
            owner_instance_id=OWNER_ID,
            now=lambda: NOW,
        )
        self.marked_states: list[ControlUpdateSendState] = []

    async def _mark_response(
        self,
        receipt: ControlUpdateReceipt,
        *,
        update_id: int,
        send_state: ControlUpdateSendState,
    ) -> None:
        assert receipt.update_id == update_id
        self.marked_states.append(send_state)


def _clock(value: object) -> Callable[[], datetime]:
    def source() -> datetime:
        return cast(datetime, value)

    return source


def _cursor(*, offset: int = 0, version: int = 1) -> ControlBotCursor:
    return ControlBotCursor(DEPLOYMENT_ID, BOT_USER_ID, offset, version, NOW)


def _claimed_receipt(*, update_id: int = 10, version: int = 1) -> ControlUpdateReceipt:
    return ControlUpdateReceipt(
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        update_id=update_id,
        state=ControlUpdateState.CLAIMED,
        disposition=None,
        send_state=ControlUpdateSendState.NOT_REQUIRED,
        owner_instance_id=OWNER_ID,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
        completed_at=None,
        attempt_count=1,
        version=version,
    )


def _completed_receipt(
    *,
    send_state: ControlUpdateSendState = ControlUpdateSendState.NOT_REQUIRED,
    update_id: int = 10,
    version: int = 2,
    owner: UUID = OWNER_ID,
) -> ControlUpdateReceipt:
    return ControlUpdateReceipt(
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        update_id=update_id,
        state=ControlUpdateState.COMPLETED,
        disposition=ControlUpdateDisposition.HANDLED,
        send_state=send_state,
        owner_instance_id=owner,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
        completed_at=NOW + timedelta(seconds=1),
        attempt_count=1,
        version=version,
    )


def _executor(
    factory: _SessionFactory,
    *,
    dispatcher_factory: object = None,
) -> durable_module.DurableControlUpdateExecutor:
    return durable_module.DurableControlUpdateExecutor(
        sessions=cast(async_sessionmaker[AsyncSession], factory),
        dispatcher_factory=cast(Any, dispatcher_factory or object()),
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        owner_instance_id=OWNER_ID,
        now=lambda: NOW,
    )


@pytest.mark.unit
def test_durable_control_constructor_and_small_helpers_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="owner instance"):
        durable_module.DurableControlUpdateExecutor(
            sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory([])),
            dispatcher_factory=cast(Any, object()),
            deployment_id=DEPLOYMENT_ID,
            bot_user_id=BOT_USER_ID,
            owner_instance_id=UUID(int=0),
        )
    for lease in (timedelta(seconds=10), timedelta(minutes=11)):
        with pytest.raises(ValueError, match="claim lease"):
            durable_module.DurableControlUpdateExecutor(
                sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory([])),
                dispatcher_factory=cast(Any, object()),
                deployment_id=DEPLOYMENT_ID,
                bot_user_id=BOT_USER_ID,
                owner_instance_id=OWNER_ID,
                claim_lease=lease,
            )

    assert durable_module._now(_clock(NOW)) == NOW
    for invalid in (None, NOW.replace(tzinfo=None), "not-a-time"):
        with pytest.raises(BotAPIError, match="BOT_CLOCK_INVALID"):
            durable_module._now(_clock(invalid))

    assert (
        durable_module._dispatcher_disposition(ControlUpdateDisposition.REJECTED)
        is UpdateDisposition.REJECTED
    )
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_INVALID"):
        durable_module._dispatcher_disposition(None)

    expected_states = {
        ControlUpdateSendState.NOT_REQUIRED: None,
        ControlUpdateSendState.SENT: BotMutationState.KNOWN,
        ControlUpdateSendState.NOT_SENT: BotMutationState.REJECTED,
        ControlUpdateSendState.UNKNOWN: BotMutationState.UNKNOWN,
        ControlUpdateSendState.PENDING: BotMutationState.REJECTED,
    }
    for state, expected in expected_states.items():
        assert durable_module._bot_send_state(state) is expected
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_INVALID"):
        durable_module._bot_send_state(cast(Any, "invalid"))


@pytest.mark.unit
def test_transaction_boundary_classifier_covers_non_message_and_command_shapes() -> None:
    cases: list[tuple[Mapping[str, Any], bool]] = [
        ({}, False),
        ({"message": "malformed"}, False),
        ({"message": {"text": 123}}, False),
        ({"message": {"text": "   "}}, False),
        ({"message": {"text": "/models"}}, False),
        ({"message": {"text": " /MODEL_VALIDATE@ControlBot payload"}}, True),
        ({"callback_query": {}, "message": {}}, False),
        ({"callback_query": {}}, True),
    ]
    for update, expected in cases:
        assert durable_module._requires_transaction_boundary(update) is expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_offset_store_loads_and_commits_only_matching_cas_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RuntimeCursorRepository", _FakeRuntimeCursorRepository)
    monkeypatch.setattr(durable_module, "RestoreGateRepository", _FakeRestoreGateRepository)
    gate = SimpleNamespace(state=RestoreGateState.OPEN)
    load_repo = _FakeRepository(cursor=_cursor(offset=4))
    store = durable_module.PostgresBotOffsetStore(
        sessions=cast(
            async_sessionmaker[AsyncSession], _SessionFactory([_Session(load_repo, gate)])
        ),
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        now=lambda: NOW,
    )
    assert await store.load_next_offset() == 4
    assert [name for name, _ in load_repo.calls] == ["get_or_create"]

    advanced = _cursor(offset=7, version=2)
    commit_repo = _FakeRepository(cursor=_cursor(offset=4, version=1), advanced=advanced)
    commit_store = durable_module.PostgresBotOffsetStore(
        sessions=cast(
            async_sessionmaker[AsyncSession], _SessionFactory([_Session(commit_repo, gate)])
        ),
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        now=lambda: NOW,
    )
    assert await commit_store.commit_next_offset(expected=4, replacement=7)
    advance_call = next(kwargs for name, kwargs in commit_repo.calls if name == "advance")
    assert advance_call["through_update_id"] == 6
    assert advance_call["expected_version"] == 1

    for cursor in (None, _cursor(offset=9)):
        repo = _FakeRepository(cursor=cursor)
        mismatch_store = durable_module.PostgresBotOffsetStore(
            sessions=cast(
                async_sessionmaker[AsyncSession], _SessionFactory([_Session(repo, gate)])
            ),
            deployment_id=DEPLOYMENT_ID,
            bot_user_id=BOT_USER_ID,
            now=lambda: NOW,
        )
        assert not await mismatch_store.commit_next_offset(expected=4, replacement=7)
        assert not any(name == "advance" for name, _ in repo.calls)

    for result in (None, _cursor(offset=8)):
        repo = _FakeRepository(cursor=_cursor(offset=4), advanced=result)
        failed_store = durable_module.PostgresBotOffsetStore(
            sessions=cast(
                async_sessionmaker[AsyncSession], _SessionFactory([_Session(repo, gate)])
            ),
            deployment_id=DEPLOYMENT_ID,
            bot_user_id=BOT_USER_ID,
            now=lambda: NOW,
        )
        assert not await failed_store.commit_next_offset(expected=4, replacement=7)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_offset_store_validates_inputs_and_restore_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RestoreGateRepository", _FakeRestoreGateRepository)
    store = durable_module.PostgresBotOffsetStore(
        sessions=cast(async_sessionmaker[AsyncSession], _SessionFactory([])),
        deployment_id=DEPLOYMENT_ID,
        bot_user_id=BOT_USER_ID,
        now=lambda: NOW,
    )
    for expected, replacement in ((-1, 1), (True, 2), (1, 1), (1, 0), (1, True)):
        with pytest.raises(BotAPIError, match="BOT_OFFSET_INVALID"):
            await store.commit_next_offset(expected=expected, replacement=replacement)

    for gate in (None, SimpleNamespace(state=RestoreGateState.CLOSED)):
        repo = _FakeRepository(cursor=_cursor(offset=0))
        closed_store = durable_module.PostgresBotOffsetStore(
            sessions=cast(
                async_sessionmaker[AsyncSession],
                _SessionFactory([_Session(repo, gate)]),
            ),
            deployment_id=DEPLOYMENT_ID,
            bot_user_id=BOT_USER_ID,
            now=lambda: NOW,
        )
        with pytest.raises(BotAPIError, match="BOT_RESTORE_GATE_CLOSED"):
            await closed_store.commit_next_offset(expected=0, replacement=1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_claim_replay_and_completion_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RuntimeCursorRepository", _FakeRuntimeCursorRepository)
    executor = _executor(_SessionFactory([]))

    for invalid_id, invalid_update in ((-1, {}), (2**63 - 1, {}), (1, None), (True, {})):
        with pytest.raises(BotAPIError, match="BOT_UPDATE_INVALID"):
            await executor.handle_update(
                update_id=invalid_id, update=cast(Mapping[str, Any], invalid_update)
            )

    repo = _FakeRepository(
        claim=ControlUpdateClaim(ControlUpdateClaimOutcome.ACQUIRED, _claimed_receipt())
    )
    claim = await executor._claim(cast(Any, repo), update_id=10)
    assert claim.outcome is ControlUpdateClaimOutcome.ACQUIRED
    claim_call = next(kwargs for name, kwargs in repo.calls if name == "claim")
    assert claim_call["lease_expires_at"] == NOW + timedelta(minutes=2)

    assert (
        await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.BELOW_OFFSET, None, update_id=10
        )
    ) == DispatchOutcome(UpdateDisposition.IGNORED)
    with pytest.raises(BotAPIError, match="BOT_UPDATE_BUSY"):
        await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.BUSY, _claimed_receipt(), update_id=10
        )
    assert (
        await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.ACQUIRED, _claimed_receipt(), update_id=10
        )
        is None
    )
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_INVALID"):
        await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.COMPLETED, None, update_id=10
        )
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_INVALID"):
        await executor._replay_outcome(
            cast(Any, repo), cast(Any, "bad"), _completed_receipt(), update_id=10
        )

    for state, expected in (
        (ControlUpdateSendState.NOT_REQUIRED, None),
        (ControlUpdateSendState.SENT, BotMutationState.KNOWN),
        (ControlUpdateSendState.NOT_SENT, BotMutationState.REJECTED),
        (ControlUpdateSendState.UNKNOWN, BotMutationState.UNKNOWN),
    ):
        receipt = _completed_receipt(send_state=state)
        result = await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.COMPLETED, receipt, update_id=10
        )
        assert result == DispatchOutcome(UpdateDisposition.HANDLED, expected)

    pending = _completed_receipt(send_state=ControlUpdateSendState.PENDING)
    repo.marked = _completed_receipt(send_state=ControlUpdateSendState.UNKNOWN, version=3)
    replay = await executor._replay_outcome(
        cast(Any, repo), ControlUpdateClaimOutcome.COMPLETED, pending, update_id=10
    )
    assert replay == DispatchOutcome(UpdateDisposition.HANDLED, BotMutationState.UNKNOWN)
    assert any(name == "mark" for name, _ in repo.calls)
    repo.marked = None
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_CONFLICT"):
        await executor._replay_outcome(
            cast(Any, repo), ControlUpdateClaimOutcome.COMPLETED, pending, update_id=10
        )

    complete_repo = _FakeRepository(
        completed=_completed_receipt(send_state=ControlUpdateSendState.PENDING)
    )
    prepared = PreparedDispatch(UpdateDisposition.HANDLED, chat_id=1, reply=BotReply("reply"))
    completed = await executor._complete(
        cast(Any, complete_repo),
        receipt=_claimed_receipt(),
        prepared=prepared,
        update_id=10,
    )
    assert completed.send_state is ControlUpdateSendState.PENDING
    complete_call = next(kwargs for name, kwargs in complete_repo.calls if name == "complete")
    assert complete_call["response_required"] is True
    complete_repo.completed = None
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_CONFLICT"):
        await executor._complete(
            cast(Any, complete_repo),
            receipt=_claimed_receipt(),
            prepared=PreparedDispatch(UpdateDisposition.IGNORED),
            update_id=10,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transactional_executor_commits_before_delivery_and_replays_below_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RuntimeCursorRepository", _FakeRuntimeCursorRepository)
    monkeypatch.setattr(durable_module, "RestoreGateRepository", _FakeRestoreGateRepository)
    gate = SimpleNamespace(state=RestoreGateState.OPEN)
    prepared = PreparedDispatch(UpdateDisposition.HANDLED)
    dispatcher = _Dispatcher(prepared, DispatchOutcome(UpdateDisposition.HANDLED))
    main_repo = _FakeRepository(
        claim=ControlUpdateClaim(ControlUpdateClaimOutcome.ACQUIRED, _claimed_receipt()),
        completed=_completed_receipt(),
    )
    executor = _executor(
        _SessionFactory([_Session(main_repo, gate)]),
        dispatcher_factory=lambda _session: cast(ControlBotDispatcher, dispatcher),
    )
    outcome = await executor.handle_update(update_id=10, update={"message": {}})
    assert outcome == DispatchOutcome(UpdateDisposition.HANDLED)
    assert dispatcher.route_calls == [(10, {"message": {}})]
    assert len(dispatcher.deliver_calls) == 1
    assert [name for name, _ in main_repo.calls] == ["get_or_create", "claim", "complete"]

    replay_repo = _FakeRepository(
        claim=ControlUpdateClaim(ControlUpdateClaimOutcome.BELOW_OFFSET, None)
    )
    replay_executor = _executor(
        _SessionFactory([_Session(replay_repo, gate)]),
        dispatcher_factory=lambda _session: cast(ControlBotDispatcher, dispatcher),
    )
    assert await replay_executor._handle_transactional(update_id=10, update={}) == DispatchOutcome(
        UpdateDisposition.IGNORED
    )

    closed_executor = _executor(
        _SessionFactory([_Session(main_repo, SimpleNamespace(state=RestoreGateState.CLOSED))]),
        dispatcher_factory=lambda _session: cast(ControlBotDispatcher, dispatcher),
    )
    with pytest.raises(BotAPIError, match="BOT_RESTORE_GATE_CLOSED"):
        await closed_executor._handle_transactional(update_id=10, update={})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_boundary_executor_uses_independent_callback_transaction_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RuntimeCursorRepository", _FakeRuntimeCursorRepository)
    monkeypatch.setattr(durable_module, "RestoreGateRepository", _FakeRestoreGateRepository)
    gate = SimpleNamespace(state=RestoreGateState.OPEN)
    prepared = PreparedDispatch(UpdateDisposition.HANDLED, chat_id=1, reply=BotReply("reply"))
    dispatcher = _Dispatcher(
        prepared, DispatchOutcome(UpdateDisposition.HANDLED, BotMutationState.KNOWN)
    )
    claim_repo = _FakeRepository(
        claim=ControlUpdateClaim(ControlUpdateClaimOutcome.ACQUIRED, _claimed_receipt())
    )
    complete_repo = _FakeRepository(
        completed=_completed_receipt(send_state=ControlUpdateSendState.PENDING)
    )
    mark_repo = _FakeRepository(
        marked=_completed_receipt(send_state=ControlUpdateSendState.SENT, version=3)
    )
    callback_session = _Session(_FakeRepository(), gate)
    sessions = _SessionFactory(
        [
            _Session(claim_repo, gate),
            callback_session,
            _Session(complete_repo, gate),
            _Session(mark_repo, gate),
        ]
    )
    factory_sessions: list[object] = []

    def dispatcher_factory(session: AsyncSession) -> ControlBotDispatcher:
        factory_sessions.append(session)
        return cast(ControlBotDispatcher, dispatcher)

    executor = _executor(sessions, dispatcher_factory=dispatcher_factory)
    result = await executor.handle_update(update_id=10, update={"callback_query": {}})
    assert result == DispatchOutcome(UpdateDisposition.HANDLED, BotMutationState.KNOWN)
    assert callback_session.commit_count == 1
    assert len(factory_sessions) == 1
    assert [name for name, _ in claim_repo.calls] == ["get_or_create", "claim"]
    assert [name for name, _ in complete_repo.calls] == ["complete"]
    assert [name for name, _ in mark_repo.calls] == ["mark"]

    boundary_replay_repo = _FakeRepository(
        claim=ControlUpdateClaim(ControlUpdateClaimOutcome.BELOW_OFFSET, None)
    )
    boundary_replay_executor = _executor(
        _SessionFactory([_Session(boundary_replay_repo, gate)]),
        dispatcher_factory=dispatcher_factory,
    )
    assert await boundary_replay_executor._handle_boundary(
        update_id=10, update={"callback_query": {}}
    ) == DispatchOutcome(UpdateDisposition.IGNORED)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivery_records_known_state_skips_unneeded_and_marks_cancellation() -> None:
    prepared = PreparedDispatch(UpdateDisposition.HANDLED)
    known_executor = _RecordingMarkExecutor()
    known = await known_executor._deliver_and_record(
        dispatcher=cast(
            ControlBotDispatcher,
            _Dispatcher(
                prepared, DispatchOutcome(UpdateDisposition.HANDLED, BotMutationState.KNOWN)
            ),
        ),
        prepared=prepared,
        completed=_completed_receipt(send_state=ControlUpdateSendState.PENDING),
        update_id=10,
    )
    assert known.send_state is BotMutationState.KNOWN
    assert known_executor.marked_states == [ControlUpdateSendState.SENT]

    no_send_executor = _RecordingMarkExecutor()
    no_send = await no_send_executor._deliver_and_record(
        dispatcher=cast(
            ControlBotDispatcher,
            _Dispatcher(prepared, DispatchOutcome(UpdateDisposition.IGNORED)),
        ),
        prepared=prepared,
        completed=_completed_receipt(send_state=ControlUpdateSendState.NOT_REQUIRED),
        update_id=10,
    )
    assert no_send.send_state is None
    assert no_send_executor.marked_states == []

    cancel_executor = _RecordingMarkExecutor()
    with pytest.raises(asyncio.CancelledError):
        await cancel_executor._deliver_and_record(
            dispatcher=cast(ControlBotDispatcher, _CancelDispatcher()),
            prepared=prepared,
            completed=_completed_receipt(send_state=ControlUpdateSendState.PENDING),
            update_id=10,
        )
    assert cancel_executor.marked_states == [ControlUpdateSendState.UNKNOWN]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_response_uses_cas_and_rejects_conflict_and_recovery_batch_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_module, "RuntimeCursorRepository", _FakeRuntimeCursorRepository)
    success_repo = _FakeRepository(
        marked=_completed_receipt(send_state=ControlUpdateSendState.SENT, version=3)
    )
    executor = _executor(_SessionFactory([_Session(success_repo)]))
    await executor._mark_response(
        _completed_receipt(send_state=ControlUpdateSendState.PENDING),
        update_id=10,
        send_state=ControlUpdateSendState.SENT,
    )
    mark_call = next(kwargs for name, kwargs in success_repo.calls if name == "mark")
    assert mark_call["expected_version"] == 2

    conflict_repo = _FakeRepository(marked=None)
    conflict_executor = _executor(_SessionFactory([_Session(conflict_repo)]))
    with pytest.raises(BotAPIError, match="BOT_RECEIPT_CONFLICT"):
        await conflict_executor._mark_response(
            _completed_receipt(send_state=ControlUpdateSendState.PENDING),
            update_id=10,
            send_state=ControlUpdateSendState.UNKNOWN,
        )

    recovery_executor = _executor(_SessionFactory([]))
    for batch_size in (0, 1_001, True, "100"):
        with pytest.raises(ValueError, match="recovery batch size"):
            await recovery_executor.recover_pending_responses(batch_size=cast(int, batch_size))
