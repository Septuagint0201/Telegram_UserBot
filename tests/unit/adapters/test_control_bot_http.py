import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.telegram_bot.dispatcher import (
    ControlBotDispatcher,
    DispatchOutcome,
    PublicServiceState,
    ServerStatusSnapshot,
    UpdateDisposition,
)
from telegram_userbot.adapters.telegram_bot.durable_control import DurableControlUpdateExecutor
from telegram_userbot.adapters.telegram_bot.http import (
    ALLOWED_UPDATES,
    BotAPIError,
    BotHTTPSender,
    BotMutationResult,
    BotMutationState,
    HttpxTelegramBotSender,
    KnownBotMessage,
    TelegramBotAPI,
    TelegramBotIdentity,
)
from telegram_userbot.adapters.telegram_bot.model_control import BotReply
from telegram_userbot.adapters.telegram_bot.polling import (
    ControlBotPoller,
    ControlUpdateExecutor,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.runtime import (
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
)

TOKEN = "123456789:SYNTHETIC_BOT_TOKEN_123456"  # noqa: S105 - synthetic fixture
IDENTITY = TelegramBotIdentity(777, "ControlTestBot")
NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        content_type: bytes = b"application/json; charset=utf-8",
        raw: bytes | None = None,
    ) -> None:
        body = raw if raw is not None else json.dumps(payload).encode()
        self.status_code = status
        self.headers = (
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode()),
        )
        self.body = body
        self.closed = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class FakeSender:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, Any], float]] = []
        self.closed = False

    async def post(
        self,
        *,
        token: SensitiveValue[str],
        method: str,
        body: SensitiveValue[bytes],
        timeout_seconds: float,
    ) -> FakeResponse:
        assert token.reveal_for_use() == TOKEN
        self.calls.append((method, json.loads(body.reveal_for_use()), timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        self.closed = True


def _api(sender: FakeSender) -> TelegramBotAPI:
    return TelegramBotAPI(SensitiveValue(TOKEN), IDENTITY, cast(BotHTTPSender, sender))


@pytest.mark.unit
async def test_bot_http_identity_polling_and_known_mutations_are_bounded() -> None:
    sender = FakeSender(
        [
            FakeResponse(
                {"ok": True, "result": {"id": 777, "is_bot": True, "username": "controltestbot"}}
            ),
            FakeResponse({"ok": True, "result": [{"update_id": 10, "message": {}}]}),
            FakeResponse({"ok": True, "result": {"message_id": 9, "chat": {"id": 42}}}),
            FakeResponse({"ok": True, "result": True}),
            FakeResponse({"ok": True, "result": True}),
        ]
    )
    api = _api(sender)
    await api.verify_identity()
    assert (await api.get_updates(offset=10))[0]["update_id"] == 10
    sent = await api.send_message(chat_id=42, text="safe status")
    assert sent == BotMutationResult(BotMutationState.KNOWN, KnownBotMessage(42, 9))
    assert sent.message is not None
    assert (await api.delete_message(sent.message)).state is BotMutationState.KNOWN
    assert (
        await api.answer_callback_query(callback_query_id="synthetic-callback")
    ).state is BotMutationState.KNOWN
    poll = sender.calls[1]
    assert poll[0] == "getUpdates"
    assert poll[1]["allowed_updates"] == list(ALLOWED_UPDATES)
    assert poll[1]["offset"] == 10
    with pytest.raises(BotAPIError, match="KNOWN_MESSAGE"):
        await api.delete_message(cast(KnownBotMessage, object()))
    await api.aclose()
    assert sender.closed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse({"ok": False}, status=400), BotMutationState.REJECTED),
        (FakeResponse({"ok": False}), BotMutationState.REJECTED),
        (FakeResponse({"ok": True}, raw=b"not-json"), BotMutationState.UNKNOWN),
        (FakeResponse({"ok": True}, status=302), BotMutationState.REJECTED),
        (FakeResponse({"ok": False}, status=500), BotMutationState.UNKNOWN),
    ],
)
async def test_bot_send_classifies_known_rejection_and_unknown_without_retry(
    response: FakeResponse, expected: BotMutationState
) -> None:
    sender = FakeSender([response])
    result = await _api(sender).send_message(chat_id=42, text="safe")
    assert result.state is expected
    assert len(sender.calls) == 1


@pytest.mark.unit
async def test_bot_transport_failure_is_send_unknown_and_secret_safe() -> None:
    sender = FakeSender([RuntimeError(TOKEN)])
    api = _api(sender)
    result = await api.send_message(chat_id=42, text="safe")
    assert result.state is BotMutationState.UNKNOWN
    assert len(sender.calls) == 1
    assert TOKEN not in repr(api)
    assert TOKEN not in repr(BotAPIError("BOT_NETWORK_FAILED"))
    assert TOKEN not in str(BotAPIError("BOT_NETWORK_FAILED"))
    http_sender = HttpxTelegramBotSender()
    assert TOKEN not in repr(http_sender)
    await http_sender.aclose()
    with pytest.raises(ValueError, match="origin"):
        HttpxTelegramBotSender(base_origin="https://example.invalid")


class ControllerFake:
    def __init__(self, name: str, reply: BotReply | None = None) -> None:
        self.name = name
        self.reply = reply or BotReply(f"{name}-reply")
        self.calls: list[dict[str, Any]] = []

    async def handle(self, **kwargs: Any) -> BotReply:
        self.calls.append(kwargs)
        return self.reply

    async def confirm_callback(self, **kwargs: Any) -> BotReply:
        self.calls.append(kwargs)
        return BotReply(f"{self.name}-confirmed")


class StatusFake:
    async def snapshot(self) -> ServerStatusSnapshot:
        return ServerStatusSnapshot(
            PublicServiceState.HEALTHY,
            PublicServiceState.DEGRADED,
            PublicServiceState.DOWN,
        )


class DispatcherAPIFake:
    identity = IDENTITY

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.answers: list[str] = []

    async def send_message(self, **kwargs: Any) -> BotMutationResult:
        self.sends.append(kwargs)
        return BotMutationResult(BotMutationState.KNOWN, KnownBotMessage(kwargs["chat_id"], 88))

    async def answer_callback_query(self, *, callback_query_id: str) -> BotMutationResult:
        self.answers.append(callback_query_id)
        return BotMutationResult(BotMutationState.KNOWN)


def _message(text: str, *, user_id: int = 42, chat_type: str = "private") -> dict[str, Any]:
    return {
        "update_id": 10,
        "message": {
            "message_id": 5,
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": user_id, "type": chat_type},
            "text": text,
        },
    }


def _dispatcher() -> tuple[ControlBotDispatcher, DispatcherAPIFake, dict[str, ControllerFake]]:
    api = DispatcherAPIFake()
    controllers = {
        "model": ControllerFake("model"),
        "conversation": ControllerFake("conversation"),
        "memory": ControllerFake(
            "memory", BotReply("memory-reply", callback_token=SensitiveValue("ma_once"))
        ),
        "context": ControllerFake(
            "context", BotReply("context-reply", callback_token=SensitiveValue("ctx_once"))
        ),
    }
    dispatcher = ControlBotDispatcher(
        identity=IDENTITY,
        allowed_admin_ids=frozenset({42}),
        api=cast(TelegramBotAPI, api),
        model=controllers["model"],
        conversation=controllers["conversation"],
        memory=controllers["memory"],
        context=controllers["context"],
        status_provider=StatusFake(),
        now=lambda: NOW,
    )
    return dispatcher, api, controllers


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "owner"),
    [
        ("/models", "model"),
        ("draft input", "model"),
        ("   ", "model"),
        ("/ai", "conversation"),
        ("/memory selected", "memory"),
        ("/context selected", "context"),
    ],
)
async def test_dispatcher_command_ownership_matrix(command: str, owner: str) -> None:
    dispatcher, api, controllers = _dispatcher()
    outcome = await dispatcher.handle_update(update_id=10, update=_message(command))
    assert outcome == DispatchOutcome(UpdateDisposition.HANDLED, BotMutationState.KNOWN)
    assert len(controllers[owner].calls) == 1
    assert sum(len(controller.calls) for controller in controllers.values()) == 1
    assert api.sends[-1]["chat_id"] == 42


@pytest.mark.unit
async def test_dispatcher_status_webapp_callback_and_bot_message_binding() -> None:
    dispatcher, api, controllers = _dispatcher()
    await dispatcher.handle_update(update_id=10, update=_message("/server_status"))
    status = api.sends[-1]["text"]
    assert status == "Server status:\napp=healthy\ncontrol=degraded\nworker=down"
    assert "endpoint" not in status
    assert "secret" not in status
    assert "path" not in status

    callback = {
        "update_id": 11,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 42, "is_bot": False},
            "data": "memory:ma_once",
            "message": {
                "message_id": 88,
                "from": {"id": 777, "is_bot": True, "username": "ControlTestBot"},
                "chat": {"id": 42, "type": "private"},
            },
        },
    }
    await dispatcher.handle_update(update_id=11, update=callback)
    assert api.answers == ["cb-1"]
    assert controllers["memory"].calls[-1]["callback_token"].reveal_for_use() == "ma_once"

    callback_payload = cast(dict[str, Any], callback["callback_query"])
    callback_message = cast(dict[str, Any], callback_payload["message"])
    callback_sender = cast(dict[str, Any], callback_message["from"])
    callback_sender["id"] = 999
    rejected = await dispatcher.handle_update(update_id=12, update=callback)
    assert rejected.disposition is UpdateDisposition.REJECTED
    assert api.answers == ["cb-1"]


@pytest.mark.unit
async def test_dispatcher_rejects_nonadmin_group_forward_webapp_and_other_bot() -> None:
    dispatcher, api, controllers = _dispatcher()
    cases = [
        _message("/models", user_id=7),
        _message("/models", chat_type="group"),
        _message("/models@AnotherBot"),
        _message("/models"),
        _message("/models"),
    ]
    cases[3]["message"]["forward_origin"] = {"type": "user"}
    cases[4]["message"]["web_app_data"] = {"data": TOKEN}
    for update in cases:
        outcome = await dispatcher.handle_update(update_id=10, update=update)
        assert outcome.disposition is UpdateDisposition.REJECTED
    assert api.sends == []
    assert all(controller.calls == [] for controller in controllers.values())


class OffsetStoreFake:
    def __init__(self) -> None:
        self.offset = 0
        self.commits: list[tuple[int, int]] = []

    async def load_next_offset(self) -> int:
        return self.offset

    async def commit_next_offset(self, *, expected: int, replacement: int) -> bool:
        if self.offset != expected:
            return False
        self.commits.append((expected, replacement))
        self.offset = replacement
        return True


class PollAPIFake:
    identity = IDENTITY

    def __init__(self, batches: list[tuple[Mapping[str, Any], ...]]) -> None:
        self.batches = batches
        self.closed = False
        self.verified = False

    async def verify_identity(self) -> None:
        self.verified = True

    async def get_updates(self, **kwargs: Any) -> tuple[Mapping[str, Any], ...]:
        del kwargs
        return self.batches.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class PollDispatcherFake:
    def __init__(self) -> None:
        self.ids: list[int] = []
        self.recoveries = 0

    async def recover_pending_responses(self, *, batch_size: int = 100) -> int:
        assert batch_size == 100
        self.recoveries += 1
        return 0

    async def handle_update(self, *, update_id: int, update: Mapping[str, Any]) -> DispatchOutcome:
        del update
        self.ids.append(update_id)
        return DispatchOutcome(UpdateDisposition.REJECTED)


@pytest.mark.unit
async def test_poller_commits_rejected_updates_and_suppresses_replayed_update_ids() -> None:
    api = PollAPIFake(
        [
            ({"update_id": 5}, {"update_id": 5}, {"update_id": 6}),
            ({"update_id": 5}, {"update_id": 6}),
        ]
    )
    offsets = OffsetStoreFake()
    dispatcher = PollDispatcherFake()
    poller = ControlBotPoller(
        api=cast(TelegramBotAPI, api),
        dispatcher=cast(ControlUpdateExecutor, dispatcher),
        offsets=offsets,
    )
    assert await poller.poll_once() == 2
    assert await poller.poll_once() == 0
    assert dispatcher.ids == [5, 6]
    assert dispatcher.recoveries == 2
    assert offsets.commits == [(0, 6), (6, 7)]


@pytest.mark.unit
async def test_poller_stop_closes_without_live_polling() -> None:
    api = PollAPIFake([])
    stop = asyncio.Event()
    stop.set()
    poller = ControlBotPoller(
        api=cast(TelegramBotAPI, api),
        dispatcher=cast(ControlUpdateExecutor, PollDispatcherFake()),
        offsets=OffsetStoreFake(),
    )
    assert not poller.identity_verified
    await poller.run(stop)
    assert api.verified
    assert api.closed
    assert not poller.identity_verified


class _RoutingExecutor(DurableControlUpdateExecutor):
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def _handle_boundary(self, **_: Any) -> DispatchOutcome:
        self.paths.append("boundary")
        return DispatchOutcome(UpdateDisposition.HANDLED)

    async def _handle_transactional(self, **_: Any) -> DispatchOutcome:
        self.paths.append("transactional")
        return DispatchOutcome(UpdateDisposition.HANDLED)


@pytest.mark.unit
async def test_executor_routes_only_callback_and_model_validate_through_commit_boundary() -> None:
    executor = _RoutingExecutor()
    callback = {"callback_query": {"id": "synthetic"}}

    await executor.handle_update(update_id=1, update=_message("/model_validate main_ai"))
    await executor.handle_update(update_id=2, update=callback)
    await executor.handle_update(update_id=3, update=_message("/models"))

    assert executor.paths == ["boundary", "boundary", "transactional"]


def _pending_receipt() -> ControlUpdateReceipt:
    return ControlUpdateReceipt(
        deployment_id="synthetic-deployment",
        bot_user_id=IDENTITY.user_id,
        update_id=10,
        state=ControlUpdateState.COMPLETED,
        disposition=ControlUpdateDisposition.HANDLED,
        send_state=ControlUpdateSendState.PENDING,
        owner_instance_id=uuid7(),
        claimed_at=NOW,
        lease_expires_at=NOW,
        completed_at=NOW,
        attempt_count=1,
        version=2,
    )


class _DeliveryDispatcherFake:
    def __init__(self, outcome: DispatchOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def deliver(self, _: object) -> DispatchOutcome:
        self.calls += 1
        return self.outcome


class _RecordingExecutor(DurableControlUpdateExecutor):
    def __init__(self) -> None:
        self.states: list[ControlUpdateSendState] = []

    async def _mark_response(
        self,
        receipt: ControlUpdateReceipt,
        *,
        update_id: int,
        send_state: ControlUpdateSendState,
    ) -> None:
        assert receipt.send_state is ControlUpdateSendState.PENDING
        assert update_id == receipt.update_id
        self.states.append(send_state)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation_state", "durable_state"),
    [
        (BotMutationState.REJECTED, ControlUpdateSendState.NOT_SENT),
        (BotMutationState.UNKNOWN, ControlUpdateSendState.UNKNOWN),
    ],
)
async def test_executor_terminalizes_rejected_and_unknown_delivery_without_retry(
    mutation_state: BotMutationState,
    durable_state: ControlUpdateSendState,
) -> None:
    executor = _RecordingExecutor()
    dispatcher = _DeliveryDispatcherFake(DispatchOutcome(UpdateDisposition.HANDLED, mutation_state))

    outcome = await executor._deliver_and_record(
        dispatcher=cast(ControlBotDispatcher, dispatcher),
        prepared=cast(Any, object()),
        completed=_pending_receipt(),
        update_id=10,
    )

    assert outcome.send_state is mutation_state
    assert dispatcher.calls == 1
    assert executor.states == [durable_state]


class _RecoveryResult:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def mappings(self) -> _RecoveryResult:
        return self

    def all(self) -> list[dict[str, object]]:
        assert isinstance(self.payload, list)
        return cast(list[dict[str, object]], self.payload)

    def one_or_none(self) -> dict[str, object] | None:
        assert self.payload is None or isinstance(self.payload, dict)
        return cast(dict[str, object] | None, self.payload)


class _AsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _RecoverySession:
    def __init__(self, results: list[_RecoveryResult]) -> None:
        self.results = results
        self.statements: list[object] = []

    async def __aenter__(self) -> _RecoverySession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> _AsyncContext:
        return _AsyncContext()

    async def execute(self, statement: object) -> _RecoveryResult:
        self.statements.append(statement)
        return self.results.pop(0)


class _RecoverySessions:
    def __init__(self, session: _RecoverySession) -> None:
        self.session = session

    def __call__(self) -> _RecoverySession:
        return self.session


def _terminal_receipt_row(*, update_id: int, owner: UUID) -> dict[str, object]:
    return {
        "deployment_id": "synthetic-deployment",
        "bot_user_id": IDENTITY.user_id,
        "update_id": update_id,
        "state": "completed",
        "disposition": "handled",
        "send_state": "unknown",
        "owner_instance_id": owner,
        "claimed_at": NOW,
        "lease_expires_at": NOW,
        "completed_at": NOW,
        "attempt_count": 1,
        "version": 3,
    }


@pytest.mark.unit
async def test_pending_recovery_is_one_bounded_pass_when_a_row_loses_its_cas() -> None:
    owner = uuid7()
    session = _RecoverySession(
        [
            _RecoveryResult([{"update_id": 10, "version": 2}, {"update_id": 11, "version": 2}]),
            _RecoveryResult(None),
            _RecoveryResult(_terminal_receipt_row(update_id=11, owner=owner)),
        ]
    )
    executor = DurableControlUpdateExecutor(
        sessions=cast(async_sessionmaker[AsyncSession], _RecoverySessions(session)),
        dispatcher_factory=cast(Any, object()),
        deployment_id="synthetic-deployment",
        bot_user_id=IDENTITY.user_id,
        owner_instance_id=owner,
    )

    assert await executor.recover_pending_responses(batch_size=2) == 1
    assert len(session.statements) == 3
    assert session.results == []
