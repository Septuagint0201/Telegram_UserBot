"""Deterministic embedding chunking and shadow-space activation."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from telegram_userbot.domain.memory.models import (
    EmbeddingRecord,
    EmbeddingSpace,
    EmbeddingState,
)


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    text: str
    source_sha256: bytes


def chunk_text(text: str, *, max_chars: int = 1_500, overlap: int = 100) -> tuple[TextChunk, ...]:
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("embedding chunk settings are invalid")
    if not text:
        return ()
    chunks: list[TextChunk] = []
    start = 0
    index = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        part = text[start:end]
        chunks.append(TextChunk(index, part, hashlib.sha256(part.encode()).digest()))
        if end == len(text):
            break
        start = end - overlap
        index += 1
    return tuple(chunks)


class FakeEmbeddingProvider:
    """Offline provider with stable vectors; never performs network access."""

    def embed(self, text: str, *, dimensions: int) -> tuple[float, ...]:
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        output: list[float] = []
        seed = hashlib.sha256(text.encode()).digest()
        for index in range(dimensions):
            byte = seed[index % len(seed)]
            output.append((byte / 127.5) - 1.0)
        norm = math.sqrt(sum(value * value for value in output))
        return tuple(value / norm for value in output) if norm else tuple(output)


@dataclass(frozen=True, slots=True)
class ShadowBuildResult:
    space: EmbeddingSpace
    records: tuple[EmbeddingRecord, ...]
    source_count: int
    dimension_ok: bool
    source_hashes_ok: bool
    target_coverage_ok: bool
    final_delta: int

    @property
    def verified(self) -> bool:
        return (
            self.dimension_ok
            and self.source_hashes_ok
            and self.target_coverage_ok
            and self.final_delta == 0
        )


@dataclass(slots=True)
class EmbeddingSpaceManager:
    spaces: dict[UUID, EmbeddingSpace] = field(default_factory=dict)
    records: dict[tuple[UUID, str, UUID, int], EmbeddingRecord] = field(default_factory=dict)
    active_by_profile: dict[UUID, UUID] = field(default_factory=dict)
    profile_by_space: dict[UUID, UUID] = field(default_factory=dict)

    def create_shadow(  # noqa: PLR0913 - space identity is explicit
        self,
        *,
        profile_id: UUID,
        model_name: str,
        dimensions: int,
        distance_metric: str = "cosine",
        normalization: str = "l2",
        chunker_version: str = "v1",
    ) -> EmbeddingSpace:
        generation = 1 + max(
            (
                space.generation
                for space_id, space in self.spaces.items()
                if self.profile_by_space.get(space_id) == profile_id
            ),
            default=0,
        )
        space = EmbeddingSpace(
            id=uuid4(),
            model_name=model_name,
            dimensions=dimensions,
            distance_metric=distance_metric,
            normalization=normalization,
            chunker_version=chunker_version,
            generation=generation,
        )
        self.spaces[space.id] = space
        self.profile_by_space[space.id] = profile_id
        return space

    def build(
        self,
        space: EmbeddingSpace,
        *,
        profile_id: UUID,
        targets: tuple[tuple[UUID, str, str], ...],
        provider: FakeEmbeddingProvider | None = None,
    ) -> ShadowBuildResult:
        if self.spaces.get(space.id) != space or self.profile_by_space.get(space.id) != profile_id:
            raise ValueError("embedding space does not belong to the selected profile")
        if space.state is not EmbeddingState.BUILDING:
            raise ValueError("only a building embedding space can accept records")
        target_keys = [(target_id, target_kind) for target_id, target_kind, _content in targets]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("embedding targets must be unique")
        provider = provider or FakeEmbeddingProvider()
        records: list[EmbeddingRecord] = []
        covered_targets: set[tuple[UUID, str]] = set()
        for target_id, target_kind, content in targets:
            if target_kind not in {"memory_version", "summary_version", "message_revision"}:
                raise ValueError("candidate, redacted, and raw media targets are not embeddable")
            chunks = chunk_text(content)
            if chunks:
                covered_targets.add((target_id, target_kind))
            for chunk in chunks:
                vector = provider.embed(chunk.text, dimensions=space.dimensions)
                records.append(
                    EmbeddingRecord(
                        id=uuid4(),
                        space_id=space.id,
                        target_id=target_id,
                        target_kind=target_kind,
                        chunk_index=chunk.index,
                        source_sha256=chunk.source_sha256,
                        vector=vector,
                    )
                )
        for record in records:
            self.records[
                (record.space_id, record.target_kind, record.target_id, record.chunk_index)
            ] = record
        dimension_ok = all(len(record.vector) == space.dimensions for record in records)
        source_hashes_ok = all(len(record.source_sha256) == 32 for record in records)
        target_coverage_ok = len(covered_targets) == len(targets)
        return ShadowBuildResult(
            space,
            tuple(records),
            len(targets),
            dimension_ok,
            source_hashes_ok,
            target_coverage_ok,
            0,
        )

    def activate(self, result: ShadowBuildResult, *, profile_id: UUID) -> EmbeddingSpace:
        if not result.verified:
            raise ValueError("shadow embedding verification failed")
        if (
            self.spaces.get(result.space.id) != result.space
            or self.profile_by_space.get(result.space.id) != profile_id
        ):
            raise ValueError("embedding space does not belong to the selected profile")
        if result.space.state is not EmbeddingState.BUILDING or any(
            record.space_id != result.space.id
            or len(record.vector) != result.space.dimensions
            or self.records.get(
                (record.space_id, record.target_kind, record.target_id, record.chunk_index)
            )
            != record
            for record in result.records
        ):
            raise ValueError("shadow embedding result does not match its building space")
        current = self.active_by_profile.get(profile_id)
        if current is not None:
            prior = self.spaces[current]
            self.spaces[current] = EmbeddingSpace(
                id=prior.id,
                model_name=prior.model_name,
                dimensions=prior.dimensions,
                distance_metric=prior.distance_metric,
                normalization=prior.normalization,
                chunker_version=prior.chunker_version,
                generation=prior.generation,
                state=EmbeddingState.RETIRED,
            )
        space = EmbeddingSpace(
            id=result.space.id,
            model_name=result.space.model_name,
            dimensions=result.space.dimensions,
            distance_metric=result.space.distance_metric,
            normalization=result.space.normalization,
            chunker_version=result.space.chunker_version,
            generation=result.space.generation,
            state=EmbeddingState.ACTIVE,
        )
        self.spaces[space.id] = space
        self.active_by_profile[profile_id] = space.id
        return space
