"""Derived-state invalidation and one-way erasure ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class DerivedKind(StrEnum):
    MEMORY = "memory"
    SUMMARY = "summary"
    EMBEDDING = "embedding"
    CONTEXT_MANIFEST = "context_manifest"
    CACHE = "cache"


@dataclass(frozen=True, slots=True, order=True)
class DerivedReference:
    kind: DerivedKind
    target_id: UUID


@dataclass(frozen=True, slots=True)
class ErasureEntry:
    source_id: UUID
    reason: str
    erased_at: datetime
    derived: tuple[DerivedReference, ...]
    content_hash: bytes | None = None

    def __post_init__(self) -> None:
        if (
            not self.reason
            or self.erased_at.tzinfo is None
            or self.erased_at.utcoffset() is None
            or (self.content_hash is not None and len(self.content_hash) != 32)
        ):
            raise ValueError("erasure entry is invalid")
        if len(self.derived) != len(set(self.derived)):
            raise ValueError("erasure entry contains duplicate derived references")


@dataclass(slots=True)
class ReconciliationLedger:
    entries: dict[UUID, ErasureEntry] = field(default_factory=dict)
    invalidated: dict[DerivedKind, set[UUID]] = field(
        default_factory=lambda: {kind: set() for kind in DerivedKind}
    )

    def forget(
        self,
        source_id: UUID,
        *,
        reason: str,
        derived_ids: dict[DerivedKind, set[UUID]],
        content_hash: bytes | None = None,
        now: datetime | None = None,
    ) -> ErasureEntry:
        if source_id in self.entries:
            return self.entries[source_id]
        entry = ErasureEntry(
            source_id=source_id,
            reason=reason,
            erased_at=now or datetime.now(UTC),
            derived=tuple(
                sorted(
                    (
                        DerivedReference(kind, target_id)
                        for kind, target_ids in derived_ids.items()
                        for target_id in target_ids
                    ),
                    key=lambda value: (value.kind.value, str(value.target_id)),
                )
            ),
            content_hash=content_hash,
        )
        self.entries[source_id] = entry
        for kind, ids in derived_ids.items():
            self.invalidated.setdefault(kind, set()).update(ids)
        return entry

    def is_erased(self, source_id: UUID) -> bool:
        return source_id in self.entries

    def can_reopen(self, source_id: UUID) -> bool:
        """Erasure is one-way; a rebuild must use a new source revision."""
        return source_id not in self.entries

    def restore_replay(self, entries: tuple[ErasureEntry, ...]) -> int:
        applied = 0
        for entry in entries:
            if entry.source_id not in self.entries:
                self.entries[entry.source_id] = entry
                for reference in entry.derived:
                    self.invalidated.setdefault(reference.kind, set()).add(reference.target_id)
                applied += 1
        return applied


@dataclass(frozen=True, slots=True)
class FreshnessInput:
    now: datetime
    last_success_at: datetime | None
    oldest_uncovered_at: datetime | None
    rebuilding: bool = False
    blocked: bool = False


def calculate_freshness(value: FreshnessInput) -> str:
    if value.blocked:
        return "blocked"
    if value.rebuilding:
        return "rebuilding"
    if value.last_success_at is None:
        return "stale"
    if value.oldest_uncovered_at is None:
        return "fresh"
    age = value.now - value.oldest_uncovered_at
    if age.total_seconds() <= 600:
        return "degraded"
    return "stale"
