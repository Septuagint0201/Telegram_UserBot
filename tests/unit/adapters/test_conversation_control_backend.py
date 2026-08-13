from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.orchestrator_records import (
    ControlCommandRecord,
    ControlResult,
    ConversationActivityRecord,
    DraftRecord,
    TurnRecord,
)
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
    OrchestratorConflictError,
)
from telegram_userbot.adapters.telegram_bot import (
    ConversationControlCommandProcessor,
    ConversationTargetTokenCodec,
    DurableConversationControlBackend,
)
from telegram_userbot.domain.conversation import (
    AccountControl,
    BaseMode,
    EffectiveMode,
    MaintenanceState,
    ModeResolution,
    OperationalState,
    WorkSnapshot,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 4, 5, 6, 7, 8, tzinfo=UTC)
ACCOUNT = UUID(int=1)
CONVERSATION = UUID(int=2)
ADMIN = 42
BOT_CHAT = ADMIN


def resolution(
    *, mode: BaseMode = BaseMode.AUTO, effective: EffectiveMode = EffectiveMode.AUTO
) -> ModeResolution:
    return ModeResolution(
        mode,
        "account_default",
        effective,
        None,
        OperationalState.READY,
        None,
        3,
        4,
        5,
        10,
        9,
    )


def command_record(  # noqa: PLR0913 - compact durable-command fixture
    *,
    update_id: int,
    kind: str,
    conversation_id: UUID | None,
    state: str = "pending",
    result_code: str | None = None,
    result_changed: bool | None = None,
    result_payload: dict[str, Any] | None = None,
    admin: int = ADMIN,
    bot_chat: int = BOT_CHAT,
) -> ControlCommandRecord:
    return ControlCommandRecord(
        UUID(int=update_id + 100),
        ACCOUNT,
        conversation_id,
        "control-bot",
        update_id,
        admin,
        bot_chat,
        kind,
        None,
        None,
        None,
        None,
        state,
        result_code,
        result_changed,
        result_payload,
    )


class RepositoryFake:
    def __init__(self) -> None:
        self.commands: dict[int, ControlCommandRecord] = {}
        self.outbox: list[tuple[str, UUID]] = []
        self.mode_calls: list[dict[str, Any]] = []
        self.pause_calls: list[dict[str, Any]] = []
        self.work_calls: list[str] = []
        self.temporary = True
        self.reject_mode = False

    async def record_control_command(self, **values: Any) -> ControlCommandRecord:
        update_id = values["telegram_update_id"]
        if update_id not in self.commands:
            self.commands[update_id] = command_record(
                update_id=update_id,
                kind=values["command_kind"],
                conversation_id=values["conversation_id"],
                admin=values["admin_telegram_user_id"],
                bot_chat=values["bot_chat_id"],
            )
        return self.commands[update_id]

    async def get_control_command(
        self, *, bot_identity: str, telegram_update_id: int
    ) -> ControlCommandRecord | None:
        assert bot_identity == "control-bot"
        return self.commands.get(telegram_update_id)

    async def add_control_command_outbox(self, **values: Any) -> None:
        self.outbox.append((values["topic"], values["command_id"]))

    async def claim_pending_control_command(self, **values: Any) -> ControlCommandRecord | None:
        assert values["account_id"] == ACCOUNT
        command_id = values.get("command_id")
        for command in self.commands.values():
            if command.state == "pending" and (command_id is None or command.id == command_id):
                return command
        return None

    async def bind_control_command_versions(self, **values: Any) -> None:
        command = next(item for item in self.commands.values() if item.id == values["command_id"])
        self.commands[command.telegram_update_id] = ControlCommandRecord(
            command.id,
            command.account_id,
            command.conversation_id,
            command.bot_identity,
            command.telegram_update_id,
            command.admin_telegram_user_id,
            command.bot_chat_id,
            command.command_kind,
            values["expected_control_version"],
            values["expected_mode_version"],
            command.result_control_version,
            command.result_mode_version,
            command.state,
            command.result_code,
            command.result_changed,
            command.result_payload,
        )

    async def finish_control_command(self, **values: Any) -> None:
        command = next(item for item in self.commands.values() if item.id == values["command_id"])
        self.commands[command.telegram_update_id] = ControlCommandRecord(
            command.id,
            command.account_id,
            command.conversation_id,
            command.bot_identity,
            command.telegram_update_id,
            command.admin_telegram_user_id,
            command.bot_chat_id,
            command.command_kind,
            command.expected_control_version,
            command.expected_mode_version,
            values.get("result_control_version"),
            values.get("result_mode_version"),
            "applied" if values["accepted"] else "rejected",
            values["result_code"],
            values["result_changed"],
            values.get("result_payload"),
        )

    async def account_control(self, account_id: UUID, now: datetime) -> AccountControl:
        assert account_id == ACCOUNT
        assert now == NOW
        return AccountControl(BaseMode.HUMAN, False, MaintenanceState.INACTIVE, 3)

    async def conversation_activity(
        self, conversation_id: UUID, now: datetime
    ) -> ConversationActivityRecord:
        assert conversation_id == CONVERSATION
        assert now == NOW
        return ConversationActivityRecord(resolution(), 2, None, None)

    async def set_account_control(self, **values: Any) -> tuple[bool, int]:
        self.mode_calls.append(values)
        return True, values["expected_version"] + 1

    async def set_conversation_control(self, **values: Any) -> ControlResult:
        if self.reject_mode:
            raise OrchestratorConflictError("MODE_VERSION_CONFLICT")
        if "base_mode_override" in values:
            self.mode_calls.append(values)
        else:
            self.pause_calls.append(values)
        return ControlResult(True, "CHANGED", resolution(), True)

    async def request_copilot_draft(self, **_: Any) -> DraftRecord:
        self.work_calls.append("draft")
        return DraftRecord(
            UUID(int=300),
            ACCOUNT,
            CONVERSATION,
            UUID(int=301),
            None,
            "collecting",
            None,
            None,
            WorkSnapshot(3, 4, 5, 1),
        )

    async def create_pending_turn(self, **_: Any) -> TurnRecord:
        self.work_calls.append("reply_pending")
        return TurnRecord(
            UUID(int=400),
            ACCOUNT,
            CONVERSATION,
            "collecting",
            "manual_pending_reply",
            1,
            WorkSnapshot(3, 4, 5, 1),
            NOW,
            NOW,
            None,
            None,
            0,
        )

    async def end_temporary_human(self, **_: Any) -> ControlResult:
        self.work_calls.append("takeover_end")
        return ControlResult(self.temporary, "TAKEOVER_ENDED", resolution(), False)


class SessionFake:
    class _Savepoint:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> None:
            return None

    def begin_nested(self) -> _Savepoint:
        return self._Savepoint()


def service(repository: RepositoryFake) -> tuple[DurableConversationControlBackend, str]:
    codec = ConversationTargetTokenCodec(SensitiveValue(b"t" * 32), deployment_id="test")
    token = codec.issue(
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        expires_at=NOW + timedelta(minutes=5),
    )
    backend = DurableConversationControlBackend(
        session=cast(Any, object()),
        account_id=ACCOUNT,
        target_tokens=codec,
        new_uuid=lambda: UUID(int=500),
    )
    backend._repository = cast(ConversationOrchestratorRepository, repository)
    return backend, token


def target_token(*, account_id: UUID = ACCOUNT) -> tuple[ConversationTargetTokenCodec, str]:
    codec = ConversationTargetTokenCodec(SensitiveValue(b"t" * 32), deployment_id="test")
    return codec, codec.issue(
        account_id=account_id,
        conversation_id=CONVERSATION,
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        expires_at=NOW + timedelta(minutes=5),
    )


def processor(repository: RepositoryFake) -> ConversationControlCommandProcessor:
    service = ConversationControlCommandProcessor(
        session=cast(Any, SessionFake()),
        account_id=ACCOUNT,
        allowed_admin_ids=frozenset({ADMIN}),
    )
    service._repository = cast(ConversationOrchestratorRepository, repository)
    return service


@pytest.mark.unit
def test_target_token_is_bound_to_admin_expiry_and_integrity() -> None:
    codec = ConversationTargetTokenCodec(SensitiveValue(b"t" * 32), deployment_id="test")
    token = codec.issue(
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        expires_at=NOW + timedelta(seconds=1),
    )
    assert str(ACCOUNT) not in token
    assert str(CONVERSATION) not in token
    assert (
        codec.resolve(token, admin_id=ADMIN, bot_chat_id=BOT_CHAT, now=NOW).conversation_id
        == CONVERSATION
    )
    tampered = token[:20] + ("A" if token[20] != "A" else "B") + token[21:]
    for candidate_admin, candidate_chat, candidate_now, candidate_token in (
        (ADMIN + 1, BOT_CHAT, NOW, token),
        (ADMIN, BOT_CHAT + 1, NOW, token),
        (ADMIN, BOT_CHAT, NOW + timedelta(seconds=1), token),
        (ADMIN, BOT_CHAT, NOW, tampered),
    ):
        with pytest.raises(ValueError, match="token"):
            codec.resolve(
                candidate_token,
                admin_id=candidate_admin,
                bot_chat_id=candidate_chat,
                now=candidate_now,
            )


@pytest.mark.unit
def test_target_token_rejects_invalid_configuration_and_encoding() -> None:
    with pytest.raises(ValueError, match="deployment"):
        ConversationTargetTokenCodec(SensitiveValue(b"t" * 32), deployment_id="")
    with pytest.raises(ValueError, match="256 bits"):
        ConversationTargetTokenCodec(SensitiveValue(b"short"), deployment_id="test")
    codec = ConversationTargetTokenCodec(SensitiveValue(b"t" * 32), deployment_id="test")
    with pytest.raises(ValueError, match="private chat"):
        codec.issue(
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            admin_id=ADMIN,
            bot_chat_id=-100123,
            expires_at=NOW + timedelta(minutes=1),
        )
    bad_nonce = ConversationTargetTokenCodec(
        SensitiveValue(b"t" * 32), deployment_id="test", nonce_source=lambda _size: b"short"
    )
    with pytest.raises(ValueError, match="12 bytes"):
        bad_nonce.issue(
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT,
            expires_at=NOW + timedelta(minutes=1),
        )
    for token in ("wrong", "ct_!", "ct_YQ"):
        with pytest.raises(ValueError, match="token"):
            codec.resolve(token, admin_id=ADMIN, bot_chat_id=BOT_CHAT, now=NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_backend_only_enqueues_and_replays_without_state_reads() -> None:
    repository = RepositoryFake()
    backend, token = service(repository)
    first = await backend.set_mode(
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        telegram_update_id=1,
        target_token=token,
        mode=BaseMode.COPILOT,
        now=NOW,
    )
    assert first.pending
    assert first.result_code == "CONTROL_COMMAND_QUEUED"
    assert repository.mode_calls == []
    assert repository.work_calls == []
    assert repository.outbox == [("control.command.requested", repository.commands[1].id)]
    replay = await backend.set_mode(
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        telegram_update_id=1,
        target_token=token,
        mode=BaseMode.COPILOT,
        now=NOW + timedelta(hours=1),
    )
    assert replay == first
    assert len(repository.outbox) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_control_backend_validates_scope_and_identity() -> None:
    repository = RepositoryFake()
    backend, token = service(repository)
    with pytest.raises(ValueError, match="inherit"):
        await backend.set_mode(
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT,
            telegram_update_id=2,
            target_token=None,
            mode=None,
            now=NOW,
        )
    with pytest.raises(OrchestratorConflictError, match="UNSUPPORTED"):
        await backend.execute(
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT,
            telegram_update_id=3,
            command="unknown",
            target_token=token,
            now=NOW,
        )
    repository.commands[4] = command_record(update_id=4, kind="human", conversation_id=CONVERSATION)
    with pytest.raises(ValueError, match="identity mismatch"):
        await backend.set_mode(
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT,
            telegram_update_id=4,
            target_token=token,
            mode=BaseMode.AUTO,
            now=NOW,
        )

    with pytest.raises(ValueError, match="private chat"):
        await backend.set_pause(
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT + 1,
            telegram_update_id=5,
            target_token=None,
            paused=True,
            now=NOW,
        )
    other_codec, other_token = target_token(account_id=UUID(int=999))
    del other_codec
    with pytest.raises(ValueError, match="account mismatch"):
        await backend.status(
            admin_id=ADMIN,
            bot_chat_id=BOT_CHAT,
            telegram_update_id=6,
            target_token=other_token,
            now=NOW,
        )


@pytest.mark.unit
def test_backend_and_processor_reject_invalid_construction() -> None:
    codec, _ = target_token()
    with pytest.raises(ValueError, match="bot identity"):
        DurableConversationControlBackend(
            session=cast(Any, object()),
            account_id=ACCOUNT,
            target_tokens=codec,
            bot_identity="",
        )
    for allowlist, identity in ((frozenset(), "control-bot"), (frozenset({ADMIN}), "")):
        with pytest.raises(ValueError, match="allowlist"):
            ConversationControlCommandProcessor(
                session=cast(Any, SessionFake()),
                account_id=ACCOUNT,
                allowed_admin_ids=allowlist,
                bot_identity=identity,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_executes_routes_and_persists_terminal_versions() -> None:
    repository = RepositoryFake()
    repository.commands[10] = command_record(
        update_id=10, kind="copilot", conversation_id=CONVERSATION
    )
    result = await processor(repository).process_next(now=NOW)
    assert result is not None
    assert result.changed
    durable = repository.commands[10]
    assert durable.state == "applied"
    assert durable.expected_control_version == 3
    assert durable.expected_mode_version == 4
    assert durable.result_control_version == 3
    assert durable.result_mode_version == 4
    assert len(repository.mode_calls) == 1
    assert repository.outbox[-1] == ("control.command.completed", durable.id)

    for update_id, kind in enumerate(
        ("draft", "reply_pending", "cancel", "takeover_end"), start=11
    ):
        repository.commands[update_id] = command_record(
            update_id=update_id, kind=kind, conversation_id=CONVERSATION
        )
        assert await processor(repository).process_next(
            now=NOW, command_id=repository.commands[update_id].id
        )
    assert repository.work_calls == ["draft", "reply_pending", "takeover_end"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_executes_account_commands_and_empty_queue() -> None:
    cases = (
        (50, "auto", "MODE_CHANGED"),
        (51, "pause", "PAUSED"),
        (52, "resume", "RESUMED"),
        (53, "status", "STATUS"),
    )
    for update_id, kind, expected in cases:
        repository = RepositoryFake()
        repository.commands[update_id] = command_record(
            update_id=update_id, kind=kind, conversation_id=None
        )
        result = await processor(repository).process_next(now=NOW)
        assert result is not None
        assert result.result_code == expected
        durable = repository.commands[update_id]
        assert durable.expected_control_version == 3
        assert durable.expected_mode_version is None
        if kind == "status":
            assert result.status is not None
            assert result.status.mode_version is None
    assert await processor(RepositoryFake()).process_next(now=NOW) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_status_reports_maintenance_pause() -> None:
    class MaintenanceRepository(RepositoryFake):
        async def account_control(self, account_id: UUID, now: datetime) -> AccountControl:
            assert account_id == ACCOUNT
            assert now == NOW
            return AccountControl(BaseMode.AUTO, False, MaintenanceState.ACTIVE, 8)

    repository = MaintenanceRepository()
    repository.commands[54] = command_record(update_id=54, kind="status", conversation_id=None)
    result = await processor(repository).process_next(now=NOW)
    assert result is not None
    assert result.status is not None
    assert result.status.effective_mode == "PAUSED"
    assert result.status.operational_state == "PAUSED"
    assert result.status.pause_reason == "maintenance_active"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_executes_remaining_conversation_commands() -> None:
    for update_id, kind, expected in (
        (60, "pause", "PAUSED"),
        (61, "resume", "RESUMED"),
        (62, "mode_inherit", "CHANGED"),
    ):
        repository = RepositoryFake()
        repository.commands[update_id] = command_record(
            update_id=update_id, kind=kind, conversation_id=CONVERSATION
        )
        result = await processor(repository).process_next(now=NOW)
        assert result is not None
        assert result.result_code == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_status_is_content_free_and_replayable() -> None:
    repository = RepositoryFake()
    repository.commands[20] = command_record(
        update_id=20, kind="status", conversation_id=CONVERSATION
    )
    result = await processor(repository).process_next(now=NOW)
    assert result is not None
    assert result.status is not None
    assert result.status.unanswered_count == 2
    payload = repository.commands[20].result_payload
    assert payload is not None
    assert "content" not in payload
    assert "message" not in payload
    assert "api_key" not in payload
    backend, token = service(repository)
    replay = await backend.status(
        admin_id=ADMIN,
        bot_chat_id=BOT_CHAT,
        telegram_update_id=20,
        target_token=token,
        now=NOW + timedelta(hours=1),
    )
    assert replay.status == result.status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_rejects_conflict_and_bad_principal() -> None:
    repository = RepositoryFake()
    repository.reject_mode = True
    repository.commands[30] = command_record(
        update_id=30, kind="human", conversation_id=CONVERSATION
    )
    rejected = await processor(repository).process_next(now=NOW)
    assert rejected is not None
    assert not rejected.accepted
    assert repository.commands[30].result_code == "MODE_VERSION_CONFLICT"

    repository.commands[31] = command_record(
        update_id=31,
        kind="auto",
        conversation_id=CONVERSATION,
        admin=ADMIN + 1,
        bot_chat=ADMIN + 1,
    )
    rejected = await processor(repository).process_next(
        now=NOW, command_id=repository.commands[31].id
    )
    assert rejected is not None
    assert not rejected.accepted
    assert repository.commands[31].result_code == "CONTROL_COMMAND_PRINCIPAL_REJECTED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_processor_leaves_unexpected_failure_pending_for_retry() -> None:
    class UnexpectedFailureRepository(RepositoryFake):
        async def set_conversation_control(self, **values: Any) -> ControlResult:
            del values
            raise OSError("synthetic database disconnect")

    repository = UnexpectedFailureRepository()
    repository.commands[40] = command_record(
        update_id=40, kind="human", conversation_id=CONVERSATION
    )
    with pytest.raises(OSError, match="disconnect"):
        await processor(repository).process_next(now=NOW)
    assert repository.commands[40].state == "pending"
    assert repository.commands[40].result_code is None
    assert repository.outbox == []
