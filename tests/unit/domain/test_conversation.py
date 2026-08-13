from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from telegram_userbot.domain.conversation import (
    AccountControl,
    BaseMode,
    ConversationControl,
    DebouncePolicy,
    DraftActionToken,
    DraftState,
    EffectiveMode,
    FinalGateInput,
    GenerationRaceDecision,
    MaintenanceState,
    OperationalState,
    WorkSnapshot,
    evaluate_final_gate,
    generation_race_decision,
    resolve_mode,
    split_telegram_text,
    validate_draft_transition,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def controls(  # noqa: PLR0913 - compact mode-matrix fixture
    *,
    default: BaseMode = BaseMode.AUTO,
    override: BaseMode | None = None,
    global_paused: bool = False,
    contact_paused: bool = False,
    maintenance: MaintenanceState = MaintenanceState.INACTIVE,
    temporary_until: datetime | None = None,
) -> tuple[AccountControl, ConversationControl]:
    return (
        AccountControl(default, global_paused, maintenance, 3, 10),
        ConversationControl(override, contact_paused, temporary_until, 4, 5, 12, 11),
    )


@pytest.mark.unit
def test_mode_priority_snapshots_and_policy_blocks() -> None:
    account, conversation = controls()
    resolved = resolve_mode(account=account, conversation=conversation, now=NOW)
    assert resolved.permits_auto
    assert not resolved.permits_copilot
    assert resolved.base_source == "account_default"
    assert resolved.automation_resume_floor_event_id == 12

    account, conversation = controls(override=BaseMode.COPILOT)
    resolved = resolve_mode(account=account, conversation=conversation, now=NOW)
    assert resolved.permits_copilot
    assert resolved.base_source == "conversation_override"

    for account, conversation, reason in (
        (*controls(maintenance=MaintenanceState.ACTIVE), "maintenance_active"),
        (*controls(global_paused=True), "global_pause"),
        (*controls(contact_paused=True), "contact_pause"),
    ):
        resolved = resolve_mode(account=account, conversation=conversation, now=NOW)
        assert resolved.effective_mode is EffectiveMode.PAUSED
        assert resolved.pause_reason == reason

    account, conversation = controls(temporary_until=NOW + timedelta(seconds=1))
    assert (
        resolve_mode(account=account, conversation=conversation, now=NOW).effective_mode
        is EffectiveMode.HUMAN
    )
    assert (
        resolve_mode(
            account=account,
            conversation=conversation,
            now=NOW + timedelta(seconds=1),
        ).effective_mode
        is EffectiveMode.AUTO
    )

    for kwargs, reason in (
        ({"account_active": False}, "ACCOUNT_INACTIVE"),
        ({"contact_automation_status": "review"}, "CONTACT_REVIEW"),
        ({"dependency_block_reason": "MAIN_AI_UNAVAILABLE"}, "MAIN_AI_UNAVAILABLE"),
    ):
        blocked = resolve_mode(
            account=account,
            conversation=conversation,
            now=NOW + timedelta(seconds=1),
            **kwargs,
        )
        assert blocked.operational_state is OperationalState.BLOCKED
        assert blocked.block_reason == reason


@pytest.mark.unit
def test_control_values_validate_versions() -> None:
    with pytest.raises(ValueError, match="control version"):
        AccountControl(BaseMode.AUTO, False, MaintenanceState.INACTIVE, 0)
    with pytest.raises(ValueError, match="conversation versions"):
        ConversationControl(None, False, None, 0, 0)
    with pytest.raises(ValueError, match="conversation versions"):
        ConversationControl(None, False, None, 1, -1)


def auto_resolution(*, revision: int = 5) -> object:
    account, conversation = controls()
    conversation = ConversationControl(None, False, None, 4, revision, 12, 11)
    return resolve_mode(account=account, conversation=conversation, now=NOW)


@pytest.mark.unit
def test_debounce_grace_and_work_snapshot_validation() -> None:
    policy = DebouncePolicy()
    quiet, hard = policy.collection_deadlines(started_at=NOW, observed_at=NOW)
    assert quiet == NOW + timedelta(seconds=3)
    assert hard == NOW + timedelta(seconds=10)
    capped, _ = policy.collection_deadlines(started_at=NOW, observed_at=NOW + timedelta(seconds=9))
    assert capped == hard
    for values in ((0, 10, 3), (3, 2, 3), (3, 10, 0)):
        with pytest.raises(ValueError, match=r"positive|hard cap"):
            DebouncePolicy(*values)
    with pytest.raises(ValueError, match="work versions"):
        WorkSnapshot(0, 1, 0, 1)
    with pytest.raises(ValueError, match="content revision"):
        WorkSnapshot(1, 1, -1, 1)

    assert (
        generation_race_decision(
            has_new_incoming=False,
            has_non_grace_change=False,
            run_started_at=NOW,
            checked_at=NOW,
            model_completed_at=None,
        )
        is GenerationRaceDecision.UNCHANGED
    )
    assert (
        generation_race_decision(
            has_new_incoming=True,
            has_non_grace_change=True,
            run_started_at=NOW,
            checked_at=NOW,
            model_completed_at=NOW,
        )
        is GenerationRaceDecision.SUPERSEDE
    )
    assert (
        generation_race_decision(
            has_new_incoming=True,
            has_non_grace_change=False,
            run_started_at=NOW,
            checked_at=NOW + timedelta(seconds=1),
            model_completed_at=None,
        )
        is GenerationRaceDecision.WAIT_FOR_GRACE
    )
    assert (
        generation_race_decision(
            has_new_incoming=True,
            has_non_grace_change=False,
            run_started_at=NOW,
            checked_at=NOW + timedelta(seconds=2),
            model_completed_at=NOW + timedelta(seconds=2),
        )
        is GenerationRaceDecision.AUTHORIZE_GRACE
    )
    assert (
        generation_race_decision(
            has_new_incoming=True,
            has_non_grace_change=False,
            run_started_at=NOW,
            checked_at=NOW + timedelta(seconds=3),
            model_completed_at=None,
        )
        is GenerationRaceDecision.SUPERSEDE
    )


@pytest.mark.unit
def test_final_gate_fails_closed_for_every_stale_dimension() -> None:
    snapshot = WorkSnapshot(3, 4, 5, 1)
    resolution = auto_resolution()
    baseline = {
        "snapshot": snapshot,
        "current": resolution,
        "required_mode": EffectiveMode.AUTO,
        "active_work": True,
        "lease_owned": True,
    }
    assert evaluate_final_gate(FinalGateInput(**baseline)).reason == "AUTHORIZED"  # type: ignore[arg-type]
    cases = (
        ({"current": auto_resolution(revision=6)}, "CONTENT_REVISION_STALE"),
        ({"required_mode": EffectiveMode.COPILOT}, "EFFECTIVE_MODE_MISMATCH"),
        ({"snapshot": WorkSnapshot(2, 4, 5, 1)}, "CONTROL_VERSION_STALE"),
        ({"snapshot": WorkSnapshot(3, 3, 5, 1)}, "MODE_VERSION_STALE"),
        ({"active_work": False}, "WORK_NOT_ACTIVE"),
        ({"lease_owned": False}, "LEASE_LOST"),
        ({"duplicate_delivery": True}, "DUPLICATE_DELIVERY"),
        ({"source_valid": False}, "SOURCE_INVALIDATED"),
    )
    for override, reason in cases:
        values = {**baseline, **override}
        decision = evaluate_final_gate(FinalGateInput(**values))  # type: ignore[arg-type]
        assert not decision.allowed
        assert decision.reason == reason
    allowed_grace = evaluate_final_gate(
        FinalGateInput(
            snapshot,
            auto_resolution(revision=6),  # type: ignore[arg-type]
            EffectiveMode.AUTO,
            True,
            True,
            grace_authorized=True,
        )
    )
    assert allowed_grace.allowed
    continuation = evaluate_final_gate(
        FinalGateInput(
            snapshot,
            auto_resolution(revision=6),  # type: ignore[arg-type]
            EffectiveMode.AUTO,
            True,
            True,
            content_revision_required=False,
        )
    )
    assert continuation.allowed
    account, conversation = controls()
    blocked = resolve_mode(
        account=account,
        conversation=conversation,
        now=NOW,
        dependency_block_reason="MODEL_DOWN",
    )
    assert (
        evaluate_final_gate(
            FinalGateInput(snapshot, blocked, EffectiveMode.AUTO, True, True)
        ).reason
        == "MODEL_DOWN"
    )


@pytest.mark.unit
def test_splitter_is_deterministic_and_respects_combining_boundaries() -> None:
    assert split_telegram_text("short") == ("short",)
    assert split_telegram_text("alpha beta gamma", max_chars=10) == ("alpha beta", " gamma")
    combined = "a" * 5 + "e\u0301" + "z" * 5
    chunks = split_telegram_text(combined, max_chars=6)
    assert "".join(chunks) == combined
    assert not any(chunk and chunk[0] == "\u0301" for chunk in chunks)
    with pytest.raises(ValueError, match="positive"):
        split_telegram_text("")
    with pytest.raises(ValueError, match="positive"):
        split_telegram_text("x", max_chars=0)
    with pytest.raises(ValueError, match="TOO_LONG"):
        split_telegram_text("abcdefghij", max_chars=2, max_chunks=2)


@pytest.mark.unit
def test_draft_token_binding_and_state_machine() -> None:
    action_token = "opaque"  # noqa: S105 - synthetic callback token, not a credential
    token = DraftActionToken(
        sha256(action_token.encode()).digest(),
        11,
        22,
        "send",
        1,
        NOW + timedelta(minutes=1),
    )
    assert token.accepts(
        raw_token=action_token,
        admin_telegram_user_id=11,
        bot_chat_id=22,
        purpose="send",
        revision_no=1,
        now=NOW,
    )
    for overrides in (
        {"raw_token": "wrong"},
        {"admin_telegram_user_id": 12},
        {"bot_chat_id": 23},
        {"purpose": "ignore"},
        {"revision_no": 2},
        {"now": NOW + timedelta(minutes=1)},
    ):
        values = {
            "raw_token": action_token,
            "admin_telegram_user_id": 11,
            "bot_chat_id": 22,
            "purpose": "send",
            "revision_no": 1,
            "now": NOW,
            **overrides,
        }
        assert not token.accepts(**values)  # type: ignore[arg-type]
    used = DraftActionToken(token.token_sha256, 11, 22, "send", 1, token.expires_at, used_at=NOW)
    assert not used.accepts(
        raw_token=action_token,
        admin_telegram_user_id=11,
        bot_chat_id=22,
        purpose="send",
        revision_no=1,
        now=NOW,
    )

    path = (
        (DraftState.REQUESTED, DraftState.COLLECTING),
        (DraftState.COLLECTING, DraftState.GENERATING),
        (DraftState.GENERATING, DraftState.READY),
        (DraftState.READY, DraftState.EDITING),
        (DraftState.EDITING, DraftState.READY),
        (DraftState.READY, DraftState.APPROVED),
        (DraftState.APPROVED, DraftState.SEND_QUEUED),
        (DraftState.SEND_QUEUED, DraftState.SEND_UNKNOWN),
        (DraftState.SEND_UNKNOWN, DraftState.SENT),
    )
    for current, target in path:
        validate_draft_transition(current, target)
    validate_draft_transition(DraftState.READY, DraftState.IGNORED)
    with pytest.raises(ValueError, match="invalid draft transition"):
        validate_draft_transition(DraftState.SENT, DraftState.READY)
