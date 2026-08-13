"""Context admission, selection, isolation, and manifest contracts."""

from telegram_userbot.domain.context.manifest import (
    BuiltContext,
    ContextManifest,
    ContextSource,
    ManifestItem,
    TrustLevel,
    build_context,
    rebuild_context,
    render_data_boundary,
)
from telegram_userbot.domain.context.policy import (
    BudgetSnapshot,
    ContextAdmissionError,
    ContextCapabilities,
    ContextPolicy,
    calculate_budget,
    estimate_utf8_bytes_v1,
)
from telegram_userbot.domain.context.selection import (
    Candidate,
    ContextLayer,
    deduplicate_sources,
    select_semantic,
    select_structured,
)

__all__ = [
    "BudgetSnapshot",
    "BuiltContext",
    "Candidate",
    "ContextAdmissionError",
    "ContextCapabilities",
    "ContextLayer",
    "ContextManifest",
    "ContextPolicy",
    "ContextSource",
    "ManifestItem",
    "TrustLevel",
    "build_context",
    "calculate_budget",
    "deduplicate_sources",
    "estimate_utf8_bytes_v1",
    "rebuild_context",
    "render_data_boundary",
    "select_semantic",
    "select_structured",
]
