"""Deterministic context candidate scoring, reranking, and deduplication."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from telegram_userbot.domain.shared.time import require_aware


class ContextLayer(StrEnum):
    INSTRUCTION = "instruction"
    IDENTITY = "identity"
    PERSONALITY = "personality"
    RELATIONSHIP_TIME = "relationship_time"
    STRUCTURED_MEMORY = "structured_memory"
    SEMANTIC_MEMORY = "semantic_memory"
    SUMMARY = "summary"
    RECENT = "recent"
    CURRENT = "current"


LAYER_PRIORITY = {
    ContextLayer.RECENT: 0,
    ContextLayer.STRUCTURED_MEMORY: 1,
    ContextLayer.SUMMARY: 2,
    ContextLayer.SEMANTIC_MEMORY: 3,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    source_id: UUID
    source_revision: str
    source_root: str
    layer: ContextLayer
    occurred_at: datetime | None
    token_estimate: int
    topic_relevance: float = 0.0
    importance: float = 0.0
    confidence: float = 0.0
    freshness: float = 0.0
    source_quality: float = 0.0
    semantic_key: str | None = None
    exact_distance: float | None = None
    embedding_space_id: UUID | None = None
    target_type: str = ""
    chunk_index: int = 0
    base_score: float | None = None
    final_score: float | None = None
    rank_position: int | None = None

    def __post_init__(self) -> None:
        if not self.source_revision or not self.source_root or self.token_estimate < 0:
            raise ValueError("context candidate identity is invalid")
        features = (
            self.topic_relevance,
            self.importance,
            self.confidence,
            self.freshness,
            self.source_quality,
        )
        if any(not 0 <= value <= 1 for value in features):
            raise ValueError("context score features must be normalized")
        if self.exact_distance is not None and self.exact_distance < 0:
            raise ValueError("exact distance cannot be negative")
        if self.occurred_at is not None:
            object.__setattr__(
                self,
                "occurred_at",
                require_aware(self.occurred_at, "occurred_at"),
            )


def select_structured(
    candidates: tuple[Candidate, ...], *, limit: int = 12
) -> tuple[Candidate, ...]:
    eligible = [item for item in candidates if item.layer is ContextLayer.STRUCTURED_MEMORY]
    covered: set[str] = set()
    selected: list[Candidate] = []
    while eligible and len(selected) < limit:
        scored: list[Candidate] = []
        for item in eligible:
            base = (
                0.30 * item.topic_relevance
                + 0.25 * item.importance
                + 0.20 * item.confidence
                + 0.15 * item.freshness
                + 0.10 * item.source_quality
            )
            novelty = 1.0 if item.semantic_key is None or item.semantic_key not in covered else 0.0
            scored.append(replace(item, base_score=base, final_score=0.90 * base + 0.10 * novelty))
        chosen = min(
            scored,
            key=lambda item: (
                -(item.final_score or 0.0),
                -(item.occurred_at.timestamp() if item.occurred_at is not None else float("-inf")),
                item.source_id,
            ),
        )
        selected.append(replace(chosen, rank_position=len(selected) + 1))
        if chosen.semantic_key is not None:
            covered.add(chosen.semantic_key)
        eligible = [item for item in eligible if item.source_id != chosen.source_id]
    return tuple(selected)


def select_semantic(
    candidates: tuple[Candidate, ...], *, active_space_id: UUID, limit: int = 8
) -> tuple[Candidate, ...]:
    semantic = [item for item in candidates if item.layer is ContextLayer.SEMANTIC_MEMORY]
    if any(item.embedding_space_id != active_space_id for item in semantic):
        raise ValueError("context_embedding_space_mismatch")
    scored: list[Candidate] = []
    for item in semantic:
        if item.exact_distance is None:
            raise ValueError("context_exact_distance_required")
        similarity = 1 / (1 + item.exact_distance)
        base = (
            0.50 * similarity
            + 0.20 * item.importance
            + 0.15 * item.confidence
            + 0.10 * item.freshness
            + 0.05 * item.source_quality
        )
        scored.append(replace(item, base_score=base, final_score=base))
    scored.sort(
        key=lambda item: (
            -(item.base_score or 0.0),
            item.exact_distance or 0.0,
            item.target_type,
            item.source_id,
            item.chunk_index,
        )
    )
    return tuple(replace(item, rank_position=index) for index, item in enumerate(scored[:limit], 1))


def deduplicate_sources(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Keep the preferred representation for each canonical source root."""

    ordered = sorted(
        candidates,
        key=lambda item: (
            LAYER_PRIORITY.get(item.layer, -1),
            item.rank_position or 0,
            item.source_id,
        ),
    )
    chosen: dict[str, Candidate] = {}
    for item in ordered:
        chosen.setdefault(item.source_root, item)
    return tuple(chosen[key] for key in sorted(chosen))
