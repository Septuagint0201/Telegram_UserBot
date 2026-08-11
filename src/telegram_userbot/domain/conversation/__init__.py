"""Conversation domain placeholder; implemented from M4."""

from telegram_userbot.domain.conversation.draft import (
    ACTIVE_DRAFT_STATES,
    DraftActionToken,
    DraftState,
    validate_draft_transition,
)
from telegram_userbot.domain.conversation.mode import (
    AccountControl,
    BaseMode,
    ConversationControl,
    EffectiveMode,
    MaintenanceState,
    ModeResolution,
    OperationalState,
    resolve_mode,
)
from telegram_userbot.domain.conversation.turn import (
    DebouncePolicy,
    FinalGateInput,
    GateDecision,
    GenerationRaceDecision,
    TurnState,
    WorkSnapshot,
    evaluate_final_gate,
    generation_race_decision,
    split_telegram_text,
)

__all__ = [
    "ACTIVE_DRAFT_STATES",
    "AccountControl",
    "BaseMode",
    "ConversationControl",
    "DebouncePolicy",
    "DraftActionToken",
    "DraftState",
    "EffectiveMode",
    "FinalGateInput",
    "GateDecision",
    "GenerationRaceDecision",
    "MaintenanceState",
    "ModeResolution",
    "OperationalState",
    "TurnState",
    "WorkSnapshot",
    "evaluate_final_gate",
    "generation_race_decision",
    "resolve_mode",
    "split_telegram_text",
    "validate_draft_transition",
]
