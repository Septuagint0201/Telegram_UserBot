from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from typing import ClassVar, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.processes.conversation_runtime as runtime_module
from telegram_userbot.adapters.persistence.orchestrator_records import (
    GenerationClaim,
    ModelRunRecord,
    RunResult,
)
from telegram_userbot.adapters.persistence.records import (
    AttemptCompletionRecord,
    OutboundIntentRecord,
    TelegramIngestResult,
)
from telegram_userbot.adapters.telegram_user import (
    PeerAdmission,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.application.ports.model import ModelResponse
from telegram_userbot.application.ports.telegram import TelegramReadReceipt, TelegramReadRequest
from telegram_userbot.domain.conversation import (
    AccountControl,
    BaseMode,
    ConversationControl,
    MaintenanceState,
    WorkSnapshot,
    resolve_mode,
)
from telegram_userbot.domain.messaging import (
    Direction,
    EventKind,
    NormalizedTelegramEvent,
    PeerKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from tests.support.fakes import FakeTelegramGateway

NOW = datetime(2030, 2, 3, 4, 5, 6, tzinfo=UTC)
IDS = deque(UUID(int=value) for value in range(100, 500))


class Context:
    async def __aenter__(self) -> Context:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Result:
    def __init__(self, values: tuple[object, ...] = ()) -> None:
        self.values = values

    def scalars(self) -> tuple[object, ...]:
        return self.values


class FakeSession(Context):
    def begin(self) -> Context:
        return Context()

    async def execute(self, statement: object) -> Result:
        return Result((UUID(int=901), UUID(int=902)))


class SessionFactory:
    def __call__(self) -> FakeSession:
        return FakeSession()


def resolution() -> object:
    return resolve_mode(
        account=AccountControl(BaseMode.AUTO, False, MaintenanceState.INACTIVE, 2),
        conversation=ConversationControl(None, False, None, 3, 4),
        now=NOW,
    )


def claim(*, auto: bool = True) -> GenerationClaim:
    snapshot = WorkSnapshot(2, 3, 4, 1)
    run = ModelRunRecord(
        UUID(int=20),
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        "running",
        1,
        UUID(int=4),
        UUID(int=5),
        UUID(int=6),
        b"i" * 32,
        NOW,
        snapshot,
        "incoming" if auto else "copilot",
    )
    return GenerationClaim(
        run,
        resolution(),  # type: ignore[arg-type]
        42 if auto else None,
        UUID(int=7) if auto else None,
    )


class FakeRepository:
    claim_value = claim()
    result_value = RunResult(UUID(int=20), "succeeded", "DELIVERY_PLANNED", UUID(int=30))
    ingest_actions: ClassVar[list[str]] = []
    failed = False
    preflight_allowed = True

    def __init__(self, session: object, *, new_uuid: object) -> None:
        pass

    async def seal_turn(self, *, turn_id: UUID, now: datetime) -> object:
        return object()

    async def resolve(self, conversation_id: UUID, now: datetime) -> object:
        return resolution()

    async def start_generation(
        self, *, turn_id: UUID, owner: UUID, now: datetime
    ) -> GenerationClaim:
        return self.claim_value

    async def complete_generation(self, **kwargs: object) -> RunResult:
        return self.result_value

    async def fail_generation(self, **kwargs: object) -> None:
        type(self).failed = True

    async def preflight_intent(
        self, *, intent_id: UUID, owner: UUID, now: datetime
    ) -> OutboundIntentRecord | None:
        if not self.preflight_allowed:
            return None
        return OutboundIntentRecord(
            intent_id,
            UUID(int=30),
            UUID(int=1),
            UUID(int=2),
            UUID(int=20),
            0,
            123,
            "synthetic",
            b"h" * 32,
            "sending",
            None,
            1,
        )

    async def handle_new_incoming(self, **kwargs: object) -> None:
        self.ingest_actions.append("incoming")

    async def human_takeover_after_ingest(self, **kwargs: object) -> None:
        self.ingest_actions.append("human")

    async def invalidate_after_content_change(self, **kwargs: object) -> None:
        self.ingest_actions.append(str(kwargs["reason"]))

    async def reconcile_completed_delivery(self, **kwargs: object) -> None:
        self.ingest_actions.append("reconciled")


class FakeLifecycle:
    result = TelegramIngestResult(1, False, True, UUID(int=50), 1, "telegram_user")
    read_records = 0
    typing_records = 0
    attempts = 0

    def __init__(self, session: object, *, new_uuid: object) -> None:
        pass

    async def ingest(self, event: object) -> TelegramIngestResult:
        return self.result

    async def record_read_high_watermark(self, *, record: object) -> bool:
        type(self).read_records += 1
        return True

    async def set_typing_lease(self, *, record: object) -> None:
        type(self).typing_records += 1

    async def finish_attempt(self, **kwargs: object) -> None:
        type(self).attempts += 1


class FakeDelivery:
    outcome = "succeeded"

    def __init__(self, gateway: object) -> None:
        pass

    async def send_prepared(self, **kwargs: object) -> AttemptCompletionRecord:
        return AttemptCompletionRecord(
            self.outcome,
            NOW,
            telegram_message_id=77 if self.outcome == "succeeded" else None,
            error_code=None if self.outcome == "succeeded" else "synthetic_failure",
        )


class GoodModel:
    async def generate(self, request: object) -> ModelResponse:
        return ModelResponse(SensitiveValue("synthetic answer"), "0" * 64)


class BadModel:
    async def generate(self, request: object) -> ModelResponse:
        raise TimeoutError


class FeedbackErrorTelegram(FakeTelegramGateway):
    async def acknowledge_read(self, request: TelegramReadRequest) -> TelegramReadReceipt:
        raise RuntimeError("synthetic feedback failure")


def raw_event(*, kind: EventKind, direction: Direction) -> NormalizedTelegramEvent:
    return normalize_update(
        event_uuid=IDS.popleft(),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=RawTelegramUpdate(
            f"{kind}:{direction}:{IDS.popleft()}",
            kind,
            NOW,
            telegram_message_id=42,
            direction=direction,
            text="synthetic",
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_auto_success_and_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "ConversationOrchestratorRepository", FakeRepository)
    monkeypatch.setattr(runtime_module, "TelegramLifecycleRepository", FakeLifecycle)
    monkeypatch.setattr(runtime_module, "TelegramDeliveryService", FakeDelivery)
    telegram = FakeTelegramGateway()
    service = runtime_module.ConversationRuntimeService(
        cast(async_sessionmaker[AsyncSession], SessionFactory()),
        model=GoodModel(),
        telegram=telegram,
        new_uuid=IDS.popleft,
        now=lambda: NOW,
        entropy=lambda size: b"e" * size,
    )
    result = await service.run_due_turn(turn_id=UUID(int=3), owner=UUID(int=8))
    assert result.delivery_group_id == UUID(int=30)
    assert len(telegram.read_requests) == 1
    assert [request.action for request in telegram.typing_requests] == ["start", "stop"]
    assert FakeLifecycle.read_records == 1
    assert FakeLifecycle.typing_records == 2
    assert FakeLifecycle.attempts == 2

    FakeRepository.failed = False
    failing = runtime_module.ConversationRuntimeService(
        cast(async_sessionmaker[AsyncSession], SessionFactory()),
        model=BadModel(),
        telegram=telegram,
        new_uuid=IDS.popleft,
        now=lambda: NOW,
    )
    failed = await failing.run_due_turn(turn_id=UUID(int=3), owner=UUID(int=8))
    assert failed.reason == "PROVIDER_TIMEOUT"
    assert FakeRepository.failed

    FakeRepository.claim_value = claim(auto=False)
    FakeRepository.result_value = RunResult(
        UUID(int=20), "succeeded", "COPILOT_DRAFT_READY", draft_id=UUID(int=40)
    )
    copilot = await service.run_due_turn(turn_id=UUID(int=3), owner=UUID(int=8))
    assert copilot.draft_id == UUID(int=40)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrated_ingest_routes_all_event_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "ConversationOrchestratorRepository", FakeRepository)
    monkeypatch.setattr(runtime_module, "TelegramLifecycleRepository", FakeLifecycle)
    service = runtime_module.OrchestratedTelegramIngestService(
        cast(async_sessionmaker[AsyncSession], SessionFactory()),
        new_uuid=IDS.popleft,
    )
    FakeRepository.ingest_actions.clear()

    FakeLifecycle.result = TelegramIngestResult(1, False, True, UUID(int=50), 1, "telegram_user")
    await service.ingest(raw_event(kind=EventKind.MESSAGE_CREATED, direction=Direction.INCOMING))

    FakeLifecycle.result = TelegramIngestResult(2, False, True, UUID(int=51), 1, "human")
    await service.ingest(raw_event(kind=EventKind.MESSAGE_CREATED, direction=Direction.OUTGOING))

    for kind in (EventKind.MESSAGE_EDITED, EventKind.MESSAGE_DELETED):
        FakeLifecycle.result = TelegramIngestResult(
            3, False, True, UUID(int=52), 2, "telegram_user"
        )
        await service.ingest(raw_event(kind=kind, direction=Direction.INCOMING))

    FakeLifecycle.result = TelegramIngestResult(4, False, True, UUID(int=53), 1, "ai")
    await service.ingest(raw_event(kind=EventKind.MESSAGE_CREATED, direction=Direction.OUTGOING))
    assert FakeRepository.ingest_actions == [
        "incoming",
        "human",
        "MESSAGE_EDITED",
        "MESSAGE_DELETED",
        "reconciled",
    ]

    FakeLifecycle.result = TelegramIngestResult(5, True, False)
    duplicate = await service.ingest(
        raw_event(kind=EventKind.MESSAGE_CREATED, direction=Direction.INCOMING)
    )
    assert duplicate.duplicate

    FakeLifecycle.result = TelegramIngestResult(6, False, True)
    no_conversation = await service.ingest(
        replace(
            raw_event(kind=EventKind.MESSAGE_CREATED, direction=Direction.INCOMING),
            conversation_id=None,
        )
    )
    assert no_conversation.projected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_feedback_and_dispatch_fail_closed_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "ConversationOrchestratorRepository", FakeRepository)
    monkeypatch.setattr(runtime_module, "TelegramLifecycleRepository", FakeLifecycle)
    monkeypatch.setattr(runtime_module, "TelegramDeliveryService", FakeDelivery)
    FakeRepository.claim_value = claim()
    FakeRepository.result_value = RunResult(
        UUID(int=20), "succeeded", "DELIVERY_PLANNED", UUID(int=30)
    )
    FakeRepository.failed = False
    FakeRepository.preflight_allowed = True
    FakeDelivery.outcome = "succeeded"

    feedback_failure = runtime_module.ConversationRuntimeService(
        cast(async_sessionmaker[AsyncSession], SessionFactory()),
        model=GoodModel(),
        telegram=FeedbackErrorTelegram(),
        new_uuid=IDS.popleft,
        now=lambda: NOW,
    )
    failed = await feedback_failure.run_due_turn(turn_id=UUID(int=3), owner=UUID(int=8))
    assert failed.reason == "TELEGRAM_FEEDBACK_ERROR"
    assert FakeRepository.failed

    service = runtime_module.ConversationRuntimeService(
        cast(async_sessionmaker[AsyncSession], SessionFactory()),
        model=GoodModel(),
        telegram=FakeTelegramGateway(),
        new_uuid=IDS.popleft,
        now=lambda: NOW,
    )
    await service._start_auto_feedback(
        replace(claim(), max_telegram_message_id=None),
        UUID(int=7),
    )
    FakeRepository.preflight_allowed = False
    assert await service.dispatch_group(group_id=UUID(int=30), owner=UUID(int=8)) == 0

    FakeRepository.preflight_allowed = True
    FakeDelivery.outcome = "permanent_failure"
    assert await service.dispatch_group(group_id=UUID(int=30), owner=UUID(int=8)) == 0

    FakeDelivery.outcome = "succeeded"
