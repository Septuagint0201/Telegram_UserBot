from collections import deque
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    AttemptCompletionRecord,
    NewDeliveryGroupRecord,
    OutboundIntentRecord,
    ReadHighWatermarkRecord,
    TypingLeaseRecord,
)
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)
from telegram_userbot.adapters.telegram_user import (
    PeerAdmission,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.domain.messaging import (
    AttemptOutcome,
    DeliveryGroupState,
    Direction,
    EventKind,
    NormalizedTelegramEvent,
    OutboundChunk,
    OutboundIntentState,
    PeerKind,
    payload_sha256,
)

NOW = datetime(2030, 3, 4, 5, 6, 7, tzinfo=UTC)


class ScriptedResult:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        scalar_rows: tuple[object, ...] = (),
        rows: tuple[object, ...] = (),
        rowcount: int = 1,
    ) -> None:
        self.row = row
        self.scalar_rows = scalar_rows
        self.rows = rows
        self.rowcount = rowcount

    def mappings(self) -> ScriptedResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row

    def one(self) -> dict[str, object]:
        assert self.row is not None
        return self.row

    def scalars(self) -> tuple[object, ...]:
        return self.scalar_rows

    def all(self) -> tuple[object, ...]:
        return self.rows


class ScriptedSession:
    def __init__(
        self,
        *,
        scalar_values: tuple[object, ...] = (),
        execute_results: tuple[ScriptedResult, ...] = (),
    ) -> None:
        self.scalar_values = deque(scalar_values)
        self.execute_results = deque(execute_results)
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_values.popleft() if self.scalar_values else None

    async def execute(self, statement: object) -> ScriptedResult:
        self.statements.append(statement)
        return self.execute_results.popleft() if self.execute_results else ScriptedResult()


def session(value: ScriptedSession) -> AsyncSession:
    return cast(AsyncSession, value)


def event(
    *, identity: str = "create:10", peer: PeerKind = PeerKind.PRIVATE_USER
) -> NormalizedTelegramEvent:
    conversation_id = UUID(int=2) if peer.supported else None
    return normalize_update(
        event_uuid=UUID(int=3),
        admission=PeerAdmission(UUID(int=1), conversation_id, peer, 42),
        raw=RawTelegramUpdate(
            identity,
            EventKind.MESSAGE_CREATED,
            NOW,
            telegram_event_at=NOW,
            telegram_message_id=10,
            direction=Direction.INCOMING,
            text="synthetic private body",
        ),
    )


def reaction_event(*, identity: str, actor_peer_id: int | None = None) -> NormalizedTelegramEvent:
    return normalize_update(
        event_uuid=UUID(int=4),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=RawTelegramUpdate(
            identity,
            EventKind.REACTION_CHANGED,
            NOW,
            telegram_message_id=10,
            reaction_key="emoji:wave",
            reaction_actor_telegram_peer_id=actor_peer_id,
        ),
    )


def outgoing_event(
    *, identity: str, kind: EventKind = EventKind.MESSAGE_CREATED
) -> NormalizedTelegramEvent:
    return normalize_update(
        event_uuid=UUID(int=5),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=RawTelegramUpdate(
            identity,
            kind,
            NOW,
            telegram_message_id=10 if kind is not EventKind.SERVICE else None,
            direction=Direction.OUTGOING,
            outbound_random_id=44 if kind is not EventKind.SERVICE else None,
            text="synthetic output" if kind is not EventKind.SERVICE else None,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_duplicate_unsupported_and_new_message_paths() -> None:
    duplicate_session = ScriptedSession(scalar_values=(None, 7))
    duplicate = await TelegramLifecycleRepository(session(duplicate_session)).ingest(event())
    assert duplicate.duplicate
    assert duplicate.event_id == 7

    unsupported_session = ScriptedSession(scalar_values=(8,))
    unsupported = await TelegramLifecycleRepository(session(unsupported_session)).ingest(
        event(identity="group:10", peer=PeerKind.GROUP)
    )
    assert unsupported.projected
    assert unsupported.message_id is None

    ids = deque((UUID(int=10), UUID(int=11)))
    created_session = ScriptedSession(
        scalar_values=(9, UUID(int=11)),
        execute_results=(ScriptedResult(None),),
    )
    created = await TelegramLifecycleRepository(
        session(created_session), new_uuid=ids.popleft
    ).ingest(event(identity="create:new"))
    assert created.message_id == UUID(int=10)
    assert created.revision_no == 1
    assert created.source == "telegram_user"
    assert len(created_session.statements) >= 7

    with pytest.raises(RuntimeError, match="event disappeared"):
        await TelegramLifecycleRepository(
            session(ScriptedSession(scalar_values=(None, None)))
        ).ingest(event(identity="duplicate:vanished"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reaction_projection_handles_missing_inserted_and_existing_rows() -> None:
    missing = await TelegramLifecycleRepository(
        session(ScriptedSession(scalar_values=(20, None)))
    ).ingest(reaction_event(identity="reaction:missing-message"))
    assert missing.message_id is None

    inserted = await TelegramLifecycleRepository(
        session(ScriptedSession(scalar_values=(21, UUID(int=10), None))),
        new_uuid=lambda: UUID(int=11),
    ).ingest(reaction_event(identity="reaction:insert"))
    assert inserted.message_id == UUID(int=10)

    updated_session = ScriptedSession(scalar_values=(22, UUID(int=10), UUID(int=12), UUID(int=13)))
    updated = await TelegramLifecycleRepository(session(updated_session)).ingest(
        reaction_event(identity="reaction:update", actor_peer_id=700)
    )
    assert updated.message_id == UUID(int=10)
    assert len(updated_session.statements) == 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outgoing_source_classification_covers_resolved_pending_and_human_paths() -> None:
    resolved_session = ScriptedSession(
        execute_results=(ScriptedResult({"source": "ai", "id": UUID(int=31)}),)
    )
    resolved = await TelegramLifecycleRepository(session(resolved_session))._classify_source(
        outgoing_event(identity="outgoing:resolved")
    )
    assert resolved == ("ai", "resolved", UUID(int=31))

    pending_session = ScriptedSession(
        scalar_values=(UUID(int=32),), execute_results=(ScriptedResult(None),)
    )
    pending = await TelegramLifecycleRepository(session(pending_session))._classify_source(
        outgoing_event(identity="outgoing:pending")
    )
    assert pending == ("system_pending", "pending", None)

    human_session = ScriptedSession(scalar_values=(None,), execute_results=(ScriptedResult(None),))
    human = await TelegramLifecycleRepository(session(human_session))._classify_source(
        outgoing_event(identity="outgoing:human")
    )
    assert human == ("human", "resolved", None)

    no_identity_session = ScriptedSession(scalar_values=(UUID(int=33),))
    no_identity = await TelegramLifecycleRepository(session(no_identity_session))._classify_source(
        outgoing_event(identity="service:pending", kind=EventKind.SERVICE)
    )
    assert no_identity == ("system_pending", "pending", None)


def group_record() -> tuple[NewDeliveryGroupRecord, OutboundChunk]:
    chunk = OutboundChunk(
        UUID(int=31), 0, 44, "synthetic output", payload_sha256("synthetic output")
    )
    group = NewDeliveryGroupRecord(
        UUID(int=30),
        UUID(int=1),
        UUID(int=2),
        UUID(int=29),
        "ai",
        sha256(b"group").digest(),
        NOW,
    )
    return group, chunk


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_delivery_group_validates_and_is_idempotent() -> None:
    group, chunk = group_record()
    inserted_session = ScriptedSession(scalar_values=(group.id,))
    repository = TelegramLifecycleRepository(session(inserted_session))
    assert await repository.create_delivery_group(group=group, chunks=(chunk,)) == group.id

    duplicate_session = ScriptedSession(scalar_values=(None, group.id))
    assert (
        await TelegramLifecycleRepository(session(duplicate_session)).create_delivery_group(
            group=group, chunks=(chunk,)
        )
        == group.id
    )
    with pytest.raises(ValueError, match="at least one"):
        await repository.create_delivery_group(group=group, chunks=())
    invalid_sequence = OutboundChunk(UUID(int=32), 2, 45, "two", payload_sha256("two"))
    with pytest.raises(ValueError, match="contiguous"):
        await repository.create_delivery_group(group=group, chunks=(invalid_sequence,))
    invalid_hash = OutboundChunk(UUID(int=33), 0, 46, "text", b"x" * 32)
    hash_session = ScriptedSession(scalar_values=(group.id,))
    with pytest.raises(ValueError, match="hash mismatch"):
        await TelegramLifecycleRepository(session(hash_session)).create_delivery_group(
            group=group, chunks=(invalid_hash,)
        )
    with pytest.raises(RuntimeError, match="group disappeared"):
        await TelegramLifecycleRepository(
            session(ScriptedSession(scalar_values=(None, None)))
        ).create_delivery_group(group=group, chunks=(chunk,))


def intent_row(*, state: str = "pending", attempt_count: int = 0) -> dict[str, object]:
    return {
        "id": UUID(int=31),
        "delivery_group_id": UUID(int=30),
        "account_id": UUID(int=1),
        "conversation_id": UUID(int=2),
        "model_run_id": UUID(int=29),
        "sequence_no": 0,
        "telegram_random_id": 44,
        "text_content": "synthetic output",
        "payload_sha256": payload_sha256("synthetic output"),
        "state": state,
        "telegram_message_id": None,
        "attempt_count": attempt_count,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claim_finish_recover_and_get_intent_state_paths() -> None:
    claimed_row = intent_row(state="sending", attempt_count=1)
    scripted = ScriptedSession(
        execute_results=(
            ScriptedResult(intent_row()),
            ScriptedResult(claimed_row),
            ScriptedResult(),
            ScriptedResult(),
            ScriptedResult(),
            ScriptedResult(),
            ScriptedResult(scalar_rows=("sent",)),
            ScriptedResult(),
            ScriptedResult(claimed_row),
        )
    )
    repository = TelegramLifecycleRepository(session(scripted))
    claimed = await repository.claim_intent(account_id=UUID(int=1), intent_id=UUID(int=31), now=NOW)
    assert claimed is not None
    assert claimed.attempt_count == 1
    await repository.finish_attempt(
        intent=claimed,
        completion=AttemptCompletionRecord(AttemptOutcome.SUCCEEDED, NOW, telegram_message_id=700),
    )
    fetched = await repository.get_intent(account_id=UUID(int=1), intent_id=UUID(int=31))
    assert fetched is not None

    missing = await TelegramLifecycleRepository(
        session(ScriptedSession(execute_results=(ScriptedResult(None),)))
    ).claim_intent(account_id=UUID(int=1), intent_id=UUID(int=99), now=NOW)
    assert missing is None

    recovery_session = ScriptedSession(
        execute_results=(
            ScriptedResult(
                rows=(
                    SimpleNamespace(id=UUID(int=31), delivery_group_id=UUID(int=30)),
                    SimpleNamespace(id=UUID(int=32), delivery_group_id=UUID(int=30)),
                )
            ),
            ScriptedResult(),
            ScriptedResult(scalar_rows=("unknown",)),
            ScriptedResult(),
        )
    )
    recovered = await TelegramLifecycleRepository(session(recovery_session)).recover_stale_sending(
        older_than=NOW - timedelta(seconds=5), now=NOW
    )
    assert recovered == 2

    assert (
        await TelegramLifecycleRepository(
            session(ScriptedSession(execute_results=(ScriptedResult(rows=()),)))
        ).recover_stale_sending(older_than=NOW - timedelta(seconds=5), now=NOW)
        == 0
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((OutboundIntentState.UNKNOWN,), DeliveryGroupState.UNKNOWN),
        (
            (OutboundIntentState.SENT, OutboundIntentState.FAILED),
            DeliveryGroupState.PARTIAL,
        ),
        ((OutboundIntentState.FAILED,), DeliveryGroupState.FAILED),
        (
            (OutboundIntentState.SENT, OutboundIntentState.PENDING),
            DeliveryGroupState.PARTIAL,
        ),
        ((OutboundIntentState.PENDING,), DeliveryGroupState.SENDING),
    ],
)
async def test_delivery_group_state_reflects_all_intent_outcomes(
    states: tuple[OutboundIntentState, ...], expected: DeliveryGroupState
) -> None:
    scripted = ScriptedSession(execute_results=(ScriptedResult(scalar_rows=states),))
    await TelegramLifecycleRepository(session(scripted))._refresh_group(UUID(int=30), NOW)
    assert cast(Any, scripted.statements[-1]).compile().params["state"] is expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_validation_read_watermark_and_typing_paths() -> None:
    intent = OutboundIntentRecord(
        UUID(int=31),
        UUID(int=30),
        UUID(int=1),
        UUID(int=2),
        UUID(int=29),
        0,
        44,
        "synthetic output",
        payload_sha256("synthetic output"),
        "sending",
        None,
        1,
    )
    repository = TelegramLifecycleRepository(session(ScriptedSession()))
    with pytest.raises(ValueError, match="requires Telegram message id"):
        await repository.finish_attempt(
            intent=intent,
            completion=AttemptCompletionRecord(AttemptOutcome.SUCCEEDED, NOW),
        )

    read_session = ScriptedSession(scalar_values=(UUID(int=50), None))
    read_repository = TelegramLifecycleRepository(session(read_session))
    read = ReadHighWatermarkRecord(
        UUID(int=50), UUID(int=1), UUID(int=2), 90, sha256(b"read").digest(), NOW
    )
    assert await read_repository.record_read_high_watermark(record=read)
    assert not await read_repository.record_read_high_watermark(record=read)

    typing_repository = TelegramLifecycleRepository(session(ScriptedSession()))
    await typing_repository.set_typing_lease(
        record=TypingLeaseRecord(
            UUID(int=1), UUID(int=2), UUID(int=60), NOW + timedelta(seconds=8), NOW
        )
    )
    await typing_repository.set_typing_lease(
        record=TypingLeaseRecord(UUID(int=1), UUID(int=2), None, None, NOW)
    )
    with pytest.raises(ValueError, match="set together"):
        await typing_repository.set_typing_lease(
            record=TypingLeaseRecord(UUID(int=1), UUID(int=2), UUID(int=60), None, NOW)
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_outbound_completion_is_fenced_by_claim_attempt_and_lease() -> None:
    intent = OutboundIntentRecord(
        UUID(int=31),
        UUID(int=30),
        UUID(int=1),
        UUID(int=2),
        UUID(int=29),
        0,
        44,
        "synthetic output",
        payload_sha256("synthetic output"),
        "sending",
        None,
        2,
        7,
        NOW + timedelta(seconds=30),
    )
    scripted = ScriptedSession(execute_results=(ScriptedResult(rowcount=0),))
    completed = await TelegramLifecycleRepository(session(scripted)).finish_attempt(
        intent=intent,
        completion=AttemptCompletionRecord(AttemptOutcome.SUCCEEDED, NOW, telegram_message_id=701),
    )
    assert not completed
    statement = str(cast(Any, scripted.statements[0]).compile())
    assert "send_fencing_token" in statement
    assert "send_lease_expires_at" in statement


@pytest.mark.unit
def test_scripted_session_shape_is_intentionally_runtime_only() -> None:
    assert cast(Any, ScriptedResult({"value": 1})).mappings().one()["value"] == 1
