"""Memory pipeline value objects and immutable contracts.

The memory agent is deliberately represented by typed values before anything is
persisted.  This keeps provider output untrusted until the validator has
checked scope, evidence, and the confidence policy.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from telegram_userbot.domain.shared.hashing import JsonValue, stable_json_bytes
from telegram_userbot.domain.shared.time import require_aware


class MemoryType(StrEnum):
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"
    INTENTION = "intention"
    STYLE = "style"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    FORGOTTEN = "forgotten"


class MemoryOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    INVALIDATE = "invalidate"


class ProposalState(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANDIDATE = "candidate"
    ERROR = "error"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class EvidenceRole(StrEnum):
    PRIMARY = "primary"
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"


class TrustClass(StrEnum):
    USER_STATEMENT = "user_statement"
    OBSERVED = "observed"
    TRUSTED_DERIVED = "trusted_derived"
    MODEL_INFERENCE = "model_inference"
    EXTERNAL = "external"


class SummaryKind(StrEnum):
    ROLLING = "rolling"
    DAILY = "daily"
    WEEKLY = "weekly"
    CONSOLIDATED = "consolidated"


class SummaryStatus(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    INVALIDATED = "invalidated"


class EmbeddingState(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class EmbeddingRecordState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    INVALIDATED = "invalidated"
    FAILED = "failed"


class Freshness(StrEnum):
    FRESH = "fresh"
    DEGRADED = "degraded"
    STALE = "stale"
    REBUILDING = "rebuilding"
    BLOCKED = "blocked"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _json_object(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Copy a provider object while rejecting values JSON cannot represent."""

    def convert(item: Any) -> JsonValue:
        if item is None or isinstance(item, (str, int, bool)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite JSON value")
            return item
        if isinstance(item, Mapping):
            return {str(key): convert(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(nested) for nested in item]
        raise ValueError("payload contains a non-JSON value")

    return {str(key): convert(nested) for key, nested in value.items()}


def normalize_semantic_key(value: str) -> str:
    """Normalize keys without changing user-visible payload text."""

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return " ".join(normalized.split())


def semantic_key_hash(value: str) -> bytes:
    return hashlib.sha256(normalize_semantic_key(value).encode()).digest()


@dataclass(frozen=True, slots=True)
class Evidence:
    source_id: UUID
    source_revision: str
    source_content_sha256: bytes
    role: EvidenceRole = EvidenceRole.PRIMARY
    trust: TrustClass = TrustClass.USER_STATEMENT
    span_start: int | None = None
    span_end: int | None = None
    visual_only: bool = False

    def __post_init__(self) -> None:
        if not self.source_revision:
            raise ValueError("evidence revision is required")
        if len(self.source_content_sha256) != 32:
            raise ValueError("evidence hash must be SHA-256")
        if self.span_start is not None and self.span_start < 0:
            raise ValueError("evidence span start must be non-negative")
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("evidence span bounds must both be present")
        if self.span_end is not None and (
            self.span_start is None or self.span_end <= self.span_start
        ):
            raise ValueError("evidence span end must be after start")


@dataclass(frozen=True, slots=True)
class InputSource:
    source_id: UUID
    revision: str
    content: str
    content_sha256: bytes
    source_type: str = "message_revision"
    trust: TrustClass = TrustClass.USER_STATEMENT
    redacted: bool = False
    visual_only: bool = False

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("source revision is required")
        if self.source_type not in {
            "message_revision",
            "media_object",
            "memory_version",
            "summary_version",
        }:
            raise ValueError("unsupported memory source type")
        if len(self.content_sha256) != 32:
            raise ValueError("source hash must be SHA-256")
        if hashlib.sha256(self.content.encode()).digest() != self.content_sha256:
            raise ValueError("source content hash mismatch")


@dataclass(frozen=True, slots=True)
class InputManifest:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    generation: int
    range_start_event_id: int
    range_end_event_id: int
    sources: tuple[InputSource, ...]
    pipeline_version: str
    policy_version: str
    prompt_version: str
    input_schema_version: int = 1
    output_schema_version: int = 1
    input_token_estimate: int = 0
    image_count: int = 0
    manifest_sha256: bytes = field(init=False)

    def __post_init__(self) -> None:
        if self.generation <= 0 or self.range_start_event_id < 0:
            raise ValueError("manifest generation and range must be positive")
        if self.range_end_event_id < self.range_start_event_id:
            raise ValueError("manifest range is reversed")
        if self.input_token_estimate < 0 or self.image_count < 0:
            raise ValueError("manifest estimates cannot be negative")
        if self.input_schema_version <= 0 or self.output_schema_version <= 0:
            raise ValueError("manifest schema versions must be positive")
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest source membership must be unique")
        object.__setattr__(self, "manifest_sha256", self.compute_hash())

    def compute_hash(self) -> bytes:
        payload: JsonValue = {
            "account_id": str(self.account_id),
            "conversation_id": str(self.conversation_id),
            "generation": self.generation,
            "range_start_event_id": self.range_start_event_id,
            "range_end_event_id": self.range_end_event_id,
            "sources": [
                {
                    "source_id": str(source.source_id),
                    "revision": source.revision,
                    "source_type": source.source_type,
                    "content_sha256": source.content_sha256.hex(),
                    "trust": source.trust.value,
                    "redacted": source.redacted,
                    "visual_only": source.visual_only,
                }
                for source in self.sources
            ],
            "pipeline_version": self.pipeline_version,
            "policy_version": self.policy_version,
            "prompt_version": self.prompt_version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "input_token_estimate": self.input_token_estimate,
            "image_count": self.image_count,
        }
        return hashlib.sha256(stable_json_bytes(payload)).digest()

    def source(self, source_id: UUID) -> InputSource | None:
        return next((source for source in self.sources if source.source_id == source_id), None)


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    operation: MemoryOperation
    memory_type: MemoryType
    semantic_key: str
    payload: Mapping[str, Any]
    confidence: float
    importance: float
    evidence: tuple[Evidence, ...]
    target_memory_ids: tuple[UUID, ...] = ()
    rendered_text: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    visual_only: bool = False
    state: ProposalState = ProposalState.RECEIVED

    def __post_init__(self) -> None:
        if not self.semantic_key.strip():
            raise ValueError("semantic key cannot be empty")
        if not 0 <= self.confidence <= 1 or not 0 <= self.importance <= 1:
            raise ValueError("confidence and importance must be between zero and one")
        valid_from = (
            require_aware(self.valid_from, "valid_from") if self.valid_from is not None else None
        )
        valid_to = require_aware(self.valid_to, "valid_to") if self.valid_to is not None else None
        if valid_from is not None and valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "payload", _json_object(self.payload))

    @property
    def semantic_key_hash(self) -> bytes:
        return semantic_key_hash(self.semantic_key)


@dataclass(frozen=True, slots=True)
class MemoryVersion:
    id: UUID
    memory_id: UUID
    version_no: int
    operation: MemoryOperation
    memory_type: MemoryType
    semantic_key_hash: bytes
    payload: Mapping[str, JsonValue]
    rendered_text: str | None
    confidence: float
    importance: float
    acceptance_kind: str
    evidence: tuple[Evidence, ...]
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    redacted_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        if self.redacted_at is not None:
            object.__setattr__(
                self,
                "redacted_at",
                require_aware(self.redacted_at, "redacted_at"),
            )
        if self.version_no <= 0:
            raise ValueError("memory version must be positive")
        if len(self.semantic_key_hash) != 32:
            raise ValueError("memory semantic key hash must be SHA-256")
        if self.acceptance_kind not in {"automatic", "manual", "reconciliation", "migration"}:
            raise ValueError("unknown acceptance kind")
        if self.rendered_text is not None and len(self.rendered_text) > 20_000:
            raise ValueError("memory rendered text is too long")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    memory_type: MemoryType
    semantic_key_hash: bytes
    current_version_no: int
    status: MemoryStatus = MemoryStatus.ACTIVE
    versions: tuple[MemoryVersion, ...] = ()
    superseded_by: UUID | None = None

    @property
    def current(self) -> MemoryVersion:
        for version in self.versions:
            if version.version_no == self.current_version_no:
                return version
        raise RuntimeError("memory current pointer has no version")

    def with_version(self, version: MemoryVersion, *, status: MemoryStatus | None = None) -> Self:
        if version.memory_id != self.id or version.version_no != self.current_version_no + 1:
            raise ValueError("memory version chain is not contiguous")
        return type(self)(
            id=self.id,
            account_id=self.account_id,
            conversation_id=self.conversation_id,
            memory_type=self.memory_type,
            semantic_key_hash=self.semantic_key_hash,
            current_version_no=version.version_no,
            status=status or version.status,
            versions=(*self.versions, version),
            superseded_by=self.superseded_by,
        )


@dataclass(frozen=True, slots=True)
class SummarySource:
    source_id: UUID
    source_kind: str
    content_sha256: bytes
    ordinal: int

    def __post_init__(self) -> None:
        if self.ordinal <= 0 or len(self.content_sha256) != 32:
            raise ValueError("summary source membership is invalid")
        if self.source_kind not in {"message_revision", "prior_summary_version"}:
            raise ValueError("summary source kind is invalid")


@dataclass(frozen=True, slots=True)
class SummaryVersion:
    id: UUID
    summary_id: UUID
    version_no: int
    kind: SummaryKind
    range_start_event_id: int
    range_end_event_id: int
    content_text: str
    sources: tuple[SummarySource, ...]
    manifest_sha256: bytes
    status: SummaryStatus = SummaryStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        if self.version_no <= 0 or self.range_end_event_id < self.range_start_event_id:
            raise ValueError("summary version range is invalid")
        if not self.content_text.strip():
            raise ValueError("summary content cannot be empty")
        if len(self.manifest_sha256) != 32:
            raise ValueError("summary manifest hash must be SHA-256")


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    id: UUID
    model_name: str
    dimensions: int
    distance_metric: str
    normalization: str
    chunker_version: str
    generation: int
    state: EmbeddingState = EmbeddingState.BUILDING

    def __post_init__(self) -> None:
        if not self.model_name or self.dimensions <= 0 or self.generation <= 0:
            raise ValueError("embedding space identity is invalid")
        if self.distance_metric not in {"cosine", "inner_product", "l2"}:
            raise ValueError("unsupported embedding distance metric")
        if self.normalization not in {"none", "l2"}:
            raise ValueError("unsupported embedding normalization")


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    id: UUID
    space_id: UUID
    target_id: UUID
    target_kind: str
    chunk_index: int
    source_sha256: bytes
    vector: tuple[float, ...]
    state: EmbeddingRecordState = EmbeddingRecordState.READY

    def __post_init__(self) -> None:
        if self.target_kind not in {"memory_version", "summary_version", "message_revision"}:
            raise ValueError("embedding target kind is invalid")
        if self.chunk_index < 0 or len(self.source_sha256) != 32:
            raise ValueError("embedding record identity is invalid")
        if not self.vector or any(not math.isfinite(value) for value in self.vector):
            raise ValueError("embedding vector is invalid")


def new_uuid() -> UUID:
    return uuid4()
