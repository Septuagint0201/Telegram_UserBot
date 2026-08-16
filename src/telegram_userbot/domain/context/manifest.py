"""Canonical data boundaries and immutable, content-free context manifests."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from telegram_userbot.domain.context.policy import (
    BudgetSnapshot,
    ContextAdmissionError,
    estimate_utf8_bytes_v1,
)
from telegram_userbot.domain.context.selection import Candidate, ContextLayer
from telegram_userbot.domain.shared.hashing import JsonValue, stable_json_bytes
from telegram_userbot.domain.shared.redaction import SensitiveValue


class TrustLevel(StrEnum):
    SYSTEM = "system"
    TRUSTED_DERIVED = "trusted_derived"
    TRUSTED_HISTORY = "trusted_history"
    MODEL_GENERATED_HISTORY = "model_generated_history"
    UNTRUSTED_USER = "untrusted_user"
    UNTRUSTED_EXTERNAL = "untrusted_external"


@dataclass(frozen=True, slots=True)
class ContextSource:
    candidate: Candidate
    canonical_role: str
    source_actor: str
    trust_level: TrustLevel
    content: SensitiveValue[str] = field(repr=False)
    source_type: str
    selection_reasons: tuple[str, ...] = ("eligible",)
    image_detail: str | None = None
    image_tokens: int = 0

    def __post_init__(self) -> None:  # noqa: PLR0912 - source contract checks are explicit
        if self.canonical_role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("canonical context role is invalid")
        if self.candidate.layer is ContextLayer.INSTRUCTION:
            if self.trust_level is not TrustLevel.SYSTEM or self.canonical_role not in {
                "system",
                "developer",
            }:
                raise ValueError("trusted instructions require a privileged role")
        elif self.canonical_role in {"system", "developer"}:
            raise ValueError("data source cannot be promoted to an instruction")
        allowed_source_types = {
            "trusted_instruction",
            "message_revision",
            "media_object",
            "memory_version",
            "summary_version",
        }
        if self.source_type not in allowed_source_types:
            raise ValueError("canonical context source type is invalid")
        revision_prefix = {
            "trusted_instruction": "version-",
            "message_revision": "revision-",
            "media_object": "sha256-",
            "memory_version": "version-",
            "summary_version": "version-",
        }[self.source_type]
        if not self.candidate.source_revision.startswith(revision_prefix):
            raise ValueError("canonical context source revision is invalid")
        if (self.candidate.layer is ContextLayer.INSTRUCTION) != (
            self.source_type == "trusted_instruction"
        ):
            raise ValueError("instruction source type does not match its layer")
        allowed_layers = {
            "trusted_instruction": {ContextLayer.INSTRUCTION},
            "message_revision": {
                ContextLayer.RELATIONSHIP_TIME,
                ContextLayer.RECENT,
                ContextLayer.CURRENT,
            },
            "media_object": {ContextLayer.CURRENT},
            "memory_version": {
                ContextLayer.IDENTITY,
                ContextLayer.PERSONALITY,
                ContextLayer.RELATIONSHIP_TIME,
                ContextLayer.STRUCTURED_MEMORY,
                ContextLayer.SEMANTIC_MEMORY,
            },
            "summary_version": {ContextLayer.SUMMARY},
        }
        if self.candidate.layer not in allowed_layers[self.source_type]:
            raise ValueError("context source type does not match its layer")
        if self.image_detail not in {None, "auto"} or self.image_tokens < 0:
            raise ValueError("canonical image metadata is invalid")
        if self.source_type == "media_object":
            if self.image_detail != "auto" or self.image_tokens <= 0:
                raise ValueError("canonical media source must bind an image budget")
        elif self.image_detail is not None or self.image_tokens != 0:
            raise ValueError("non-media source cannot carry image metadata")
        is_image = self.source_type == "media_object"
        if is_image and self.candidate.layer is not ContextLayer.CURRENT:
            raise ValueError("V1 images must belong to the current turn")
        if not self.content.reveal_for_use():
            raise ValueError("context source content cannot be empty")


@dataclass(frozen=True, slots=True)
class ManifestItem:
    ordinal: int
    layer: str
    canonical_role: str
    source_actor: str
    source_type: str
    source_id: str
    source_revision: str
    trust_level: str
    token_estimate: int
    estimated_image_tokens: int
    content_sha256: str
    rendered_part_sha256: str
    reasons: tuple[str, ...]
    rank_position: int | None
    base_score: float | None
    final_score: float | None
    image_detail: str | None

    def payload(self) -> dict[str, JsonValue]:
        return {
            "ordinal": self.ordinal,
            "layer": self.layer,
            "canonical_role": self.canonical_role,
            "source_actor": self.source_actor,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "trust_level": self.trust_level,
            "token_estimate": self.token_estimate,
            "estimated_image_tokens": self.estimated_image_tokens,
            "content_sha256": self.content_sha256,
            "rendered_part_sha256": self.rendered_part_sha256,
            "reasons": list(self.reasons),
            "rank_position": self.rank_position,
            "base_score": self.base_score,
            "final_score": self.final_score,
            "image_detail": self.image_detail,
        }


@dataclass(frozen=True, slots=True)
class ContextManifest:
    id: UUID
    purpose: str
    logical_role: str
    builder_version: str
    prompt_version: str
    prompt_bundle_sha256: str
    context_policy_version: str
    retrieval_policy_version: str
    token_estimator_version: str
    capability_snapshot_sha256: str
    memory_freshness: str
    effective_input_budget: int
    safety_reserve_tokens: int
    estimated_instruction_tokens: int
    estimated_text_tokens: int
    estimated_image_tokens: int
    estimated_structural_tokens: int
    items: tuple[ManifestItem, ...]
    omissions: tuple[str, ...]
    source_revision_vector_sha256: str
    manifest_sha256: str

    @property
    def input_token_estimate(self) -> int:
        return (
            self.estimated_instruction_tokens
            + self.estimated_text_tokens
            + self.estimated_image_tokens
            + self.estimated_structural_tokens
        )


@dataclass(frozen=True, slots=True)
class BuiltContext:
    manifest: ContextManifest
    ordered_sources: tuple[ContextSource, ...] = field(repr=False)


def render_data_boundary(source: ContextSource) -> str:
    content = source.content.reveal_for_use()
    if source.candidate.layer is ContextLayer.INSTRUCTION:
        return content
    label = (
        f"[CONTEXT_DATA layer={source.candidate.layer.value} "
        f"source={source.candidate.source_id} trust={source.trust_level.value}]"
    )
    return f"{label}\n{content}\n[/CONTEXT_DATA]"


def build_context(  # noqa: PLR0912,PLR0913,PLR0915 - deterministic policy branches are explicit
    *,
    manifest_id: UUID,
    purpose: str,
    logical_role: str,
    sources: tuple[ContextSource, ...],
    budget: BudgetSnapshot,
    builder_version: str,
    prompt_version: str,
    prompt_bundle_sha256: str,
    context_policy_version: str,
    retrieval_policy_version: str,
    capability_snapshot_sha256: str,
    memory_freshness: str = "fresh",
) -> BuiltContext:
    mandatory = [item for item in sources if item.candidate.layer is ContextLayer.INSTRUCTION]
    current = [item for item in sources if item.candidate.layer is ContextLayer.CURRENT]
    image_sources = [item for item in sources if item.image_detail is not None]
    if (
        len(image_sources) > budget.image_count_cap
        or sum(item.image_tokens for item in image_sources) != budget.image_token_reserve
    ):
        raise ContextAdmissionError("context_image_budget_mismatch")
    mandatory_tokens = sum(_source_tokens(item) for item in mandatory)
    current_tokens = sum(_source_tokens(item) for item in current)
    if mandatory_tokens > budget.effective_input_budget:
        raise ContextAdmissionError("context_mandatory_over_budget")
    if (
        mandatory_tokens + current_tokens + budget.image_token_reserve
        > budget.effective_input_budget
    ):
        raise ContextAdmissionError("context_current_turn_over_budget")

    layer_order = {layer: index for index, layer in enumerate(ContextLayer)}
    ordered = tuple(
        sorted(
            sources,
            key=lambda item: (
                layer_order[item.candidate.layer],
                item.candidate.rank_position or 0,
                item.candidate.occurred_at or _minimum_datetime(),
                item.candidate.source_id,
            ),
        )
    )
    current = [item for item in ordered if item.candidate.layer is ContextLayer.CURRENT]
    content_pool = budget.effective_input_budget - mandatory_tokens - budget.image_token_reserve
    capacities = {
        name: content_pool * basis_points // 10_000
        for name, basis_points in budget.section_basis_points
    }
    capacities["current"] += content_pool - sum(capacities.values())
    selected: dict[tuple[str, UUID], tuple[ContextSource, tuple[str, ...]]] = {
        _source_key(item): (item, ()) for item in mandatory
    }
    omitted: dict[tuple[str, UUID], ContextSource] = {}

    for source in current:
        borrowed = _consume_budget(
            capacities,
            _source_tokens(source),
            ("current", "summary", "semantic", "structured", "profile", "recent"),
            owner="current",
        )
        if borrowed is None:
            raise ContextAdmissionError("context_current_turn_over_budget")
        selected[_source_key(source)] = (source, borrowed)

    pending: dict[str, list[ContextSource]] = {
        name: [] for name in ("recent", "profile", "structured", "semantic", "summary")
    }
    for source in ordered:
        group = _budget_group(source.candidate.layer)
        if group is None or group == "current":
            continue
        borrowed = _consume_budget(capacities, _source_tokens(source), (group,), owner=group)
        if borrowed is None:
            pending[group].append(source)
        else:
            selected[_source_key(source)] = (source, borrowed)

    for group, lenders in (
        ("recent", ("recent", "summary", "semantic", "structured", "profile")),
        ("structured", ("structured", "semantic", "summary")),
    ):
        for source in pending[group]:
            borrowed = _consume_budget(capacities, _source_tokens(source), lenders, owner=group)
            if borrowed is None:
                omitted[_source_key(source)] = source
            else:
                selected[_source_key(source)] = (source, borrowed)
        pending[group] = []
    for remaining in pending.values():
        for source in remaining:
            omitted[_source_key(source)] = source

    selected_in_order = [
        selected[_source_key(item)] for item in ordered if _source_key(item) in selected
    ]
    omissions = [
        f"{item.candidate.layer.value}:budget_exhausted"
        for item in ordered
        if _source_key(item) in omitted
    ]
    items = tuple(
        _manifest_item(index, source, extra_reasons=reasons)
        for index, (source, reasons) in enumerate(selected_in_order, 1)
    )
    source_vector = [
        {
            "source_type": item.source_type,
            "source_id": item.source_id,
            "revision": item.source_revision,
        }
        for item in items
    ]
    source_vector_hash = hashlib.sha256(
        stable_json_bytes(cast(JsonValue, source_vector))
    ).hexdigest()
    if len(prompt_bundle_sha256) != 64 or len(capability_snapshot_sha256) != 64:
        raise ValueError("context snapshot hashes must be sha256 hex")
    try:
        bytes.fromhex(prompt_bundle_sha256)
        bytes.fromhex(capability_snapshot_sha256)
    except ValueError as error:
        raise ValueError("context snapshot hashes must be sha256 hex") from error
    manifest_identity: dict[str, JsonValue] = {
        "purpose": purpose,
        "logical_role": logical_role,
        "builder_version": builder_version,
        "prompt_version": prompt_version,
        "prompt_bundle_sha256": prompt_bundle_sha256,
        "context_policy_version": context_policy_version,
        "retrieval_policy_version": retrieval_policy_version,
        "token_estimator_version": "utf8_bytes_v1",
        "capability_snapshot_sha256": capability_snapshot_sha256,
        "memory_freshness": memory_freshness,
        "budget": budget.effective_input_budget,
        "items": [item.payload() for item in items],
        "omissions": omissions,
    }
    manifest_hash = hashlib.sha256(stable_json_bytes(manifest_identity)).hexdigest()
    instruction_tokens = sum(
        item.token_estimate for item in items if item.layer == ContextLayer.INSTRUCTION.value
    )
    image_tokens = budget.image_token_reserve
    text_tokens = sum(item.token_estimate for item in items) - instruction_tokens
    manifest = ContextManifest(
        manifest_id,
        purpose,
        logical_role,
        builder_version,
        prompt_version,
        prompt_bundle_sha256,
        context_policy_version,
        retrieval_policy_version,
        "utf8_bytes_v1",
        capability_snapshot_sha256,
        memory_freshness,
        budget.effective_input_budget,
        budget.safety_reserve_tokens,
        instruction_tokens,
        text_tokens,
        image_tokens,
        0,
        items,
        tuple(omissions),
        source_vector_hash,
        manifest_hash,
    )
    return BuiltContext(manifest, tuple(source for source, _ in selected_in_order))


def rebuild_context(expected: ContextManifest, sources: tuple[ContextSource, ...]) -> BuiltContext:
    by_identity = {
        (item.source_type, str(item.candidate.source_id), item.candidate.source_revision): item
        for item in sources
    }
    ordered: list[ContextSource] = []
    for item in expected.items:
        source = by_identity.get((item.source_type, item.source_id, item.source_revision))
        if source is None:
            raise ValueError("context_source_unavailable")
        budget_reasons = tuple(
            reason for reason in item.reasons if reason.startswith("budget_borrowed_from:")
        )
        rebuilt = _manifest_item(item.ordinal, source, extra_reasons=budget_reasons)
        if rebuilt != item:
            raise ValueError("context_source_revision_changed")
        ordered.append(source)
    return BuiltContext(expected, tuple(ordered))


def _source_tokens(source: ContextSource) -> int:
    return source.candidate.token_estimate or estimate_utf8_bytes_v1(render_data_boundary(source))


def _manifest_item(
    ordinal: int, source: ContextSource, *, extra_reasons: tuple[str, ...] = ()
) -> ManifestItem:
    raw = source.content.reveal_for_use()
    rendered = render_data_boundary(source)
    return ManifestItem(
        ordinal,
        source.candidate.layer.value,
        source.canonical_role,
        source.source_actor,
        source.source_type,
        str(source.candidate.source_id),
        source.candidate.source_revision,
        source.trust_level.value,
        _source_tokens(source),
        source.image_tokens,
        hashlib.sha256(raw.encode()).hexdigest(),
        hashlib.sha256(rendered.encode()).hexdigest(),
        source.selection_reasons + extra_reasons,
        source.candidate.rank_position,
        source.candidate.base_score,
        source.candidate.final_score,
        source.image_detail,
    )


def _minimum_datetime() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


def _source_key(source: ContextSource) -> tuple[str, UUID]:
    return source.source_type, source.candidate.source_id


def _budget_group(layer: ContextLayer) -> str | None:
    groups = {
        ContextLayer.INSTRUCTION: None,
        ContextLayer.CURRENT: "current",
        ContextLayer.RECENT: "recent",
        ContextLayer.IDENTITY: "profile",
        ContextLayer.PERSONALITY: "profile",
        ContextLayer.RELATIONSHIP_TIME: "profile",
        ContextLayer.STRUCTURED_MEMORY: "structured",
        ContextLayer.SEMANTIC_MEMORY: "semantic",
        ContextLayer.SUMMARY: "summary",
    }
    return groups[layer]


def _consume_budget(
    capacities: dict[str, int],
    token_count: int,
    order: tuple[str, ...],
    *,
    owner: str,
) -> tuple[str, ...] | None:
    if token_count > sum(capacities[name] for name in order):
        return None
    remaining = token_count
    borrowed: list[str] = []
    for name in order:
        consumed = min(remaining, capacities[name])
        if consumed:
            capacities[name] -= consumed
            remaining -= consumed
            if name != owner:
                borrowed.append(f"budget_borrowed_from:{name}")
        if not remaining:
            break
    return tuple(borrowed)
