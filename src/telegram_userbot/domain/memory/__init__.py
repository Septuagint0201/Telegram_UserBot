"""Memory, summary, embedding, and erasure domain contracts."""

from telegram_userbot.domain.memory.embedding import (
    EmbeddingSpaceManager,
    FakeEmbeddingProvider,
    ShadowBuildResult,
    TextChunk,
    chunk_text,
)
from telegram_userbot.domain.memory.lifecycle import (
    AcceptanceResult,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryStore,
)
from telegram_userbot.domain.memory.models import *  # noqa: F403
from telegram_userbot.domain.memory.reconciliation import (
    DerivedKind,
    DerivedReference,
    ErasureEntry,
    FreshnessInput,
    ReconciliationLedger,
    calculate_freshness,
)
from telegram_userbot.domain.memory.summary import (
    SummaryCoverageError,
    SummaryStore,
    SummaryWatermark,
    rolling_summary_due,
    summary_period,
)
from telegram_userbot.domain.memory.trigger import (
    EventRange,
    GenerationQueue,
    MemoryGeneration,
    RangeState,
    TriggerDecision,
    TriggerInput,
    TriggerPolicy,
    TriggerReason,
    evaluate_triggers,
)
from telegram_userbot.domain.memory.validation import (
    EvidenceGraphNode,
    ProposalValidationError,
    ValidatedProposal,
    ValidationIssue,
    parse_agent_response,
    validate_evidence_roots,
    validate_proposal,
    validate_response_json,
)

__all__ = [
    "AcceptanceResult",
    "DerivedKind",
    "DerivedReference",
    "EmbeddingSpaceManager",
    "ErasureEntry",
    "EventRange",
    "EvidenceGraphNode",
    "FakeEmbeddingProvider",
    "FreshnessInput",
    "GenerationQueue",
    "MemoryConflictError",
    "MemoryGeneration",
    "MemoryNotFoundError",
    "MemoryStore",
    "ProposalValidationError",
    "RangeState",
    "ReconciliationLedger",
    "ShadowBuildResult",
    "SummaryCoverageError",
    "SummaryStore",
    "SummaryWatermark",
    "TextChunk",
    "TriggerDecision",
    "TriggerInput",
    "TriggerPolicy",
    "TriggerReason",
    "ValidatedProposal",
    "ValidationIssue",
    "calculate_freshness",
    "chunk_text",
    "evaluate_triggers",
    "parse_agent_response",
    "rolling_summary_due",
    "summary_period",
    "validate_evidence_roots",
    "validate_proposal",
    "validate_response_json",
]
