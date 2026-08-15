"""Strict Memory Agent output validation and evidence policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from telegram_userbot.domain.memory.models import (
    Evidence,
    EvidenceRole,
    InputManifest,
    MemoryOperation,
    MemoryProposal,
    MemoryType,
    ProposalState,
    TrustClass,
)


class ProposalValidationError(ValueError):
    """Stable, user-independent validator failure."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    proposal: MemoryProposal
    state: ProposalState
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def searchable(self) -> bool:
        return self.state is ProposalState.ACCEPTED


@dataclass(frozen=True, slots=True)
class EvidenceGraphNode:
    id: UUID
    kind: str
    parents: tuple[UUID, ...] = ()
    redacted: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"canonical_message", "memory_version", "summary_version"}:
            raise ValueError("evidence graph node kind is invalid")


def validate_evidence_roots(
    node_id: UUID,
    graph: Mapping[UUID, EvidenceGraphNode],
    *,
    max_depth: int = 8,
) -> tuple[UUID, ...]:
    """Resolve derived evidence to current canonical roots with cycle protection."""

    if max_depth <= 0:
        raise ValueError("evidence maximum depth must be positive")
    roots: set[UUID] = set()

    def visit(current_id: UUID, depth: int, path: frozenset[UUID]) -> None:
        if depth > max_depth:
            raise ProposalValidationError("evidence_depth_exceeded", "evidence chain is too deep")
        if current_id in path:
            raise ProposalValidationError("evidence_cycle", "evidence chain contains a cycle")
        node = graph.get(current_id)
        if node is None or node.redacted:
            raise ProposalValidationError(
                "evidence_root_unavailable", "evidence source is unavailable"
            )
        if node.kind == "canonical_message":
            if node.parents:
                raise ProposalValidationError(
                    "invalid_evidence_root", "canonical source cannot have parents"
                )
            roots.add(node.id)
            return
        if not node.parents:
            raise ProposalValidationError(
                "evidence_root_missing", "derived evidence has no canonical root"
            )
        for parent in node.parents:
            visit(parent, depth + 1, path | {current_id})

    visit(node_id, 0, frozenset())
    return tuple(sorted(roots, key=str))


_TOP_LEVEL = {
    "schema_version",
    "proposals",
}
_PROPOSAL_FIELDS = {
    "id",
    "operation",
    "memory_type",
    "semantic_key",
    "payload",
    "rendered_text",
    "confidence",
    "importance",
    "evidence",
    "targets",
    "valid_from",
    "valid_to",
    "visual_only",
}
_EVIDENCE_FIELDS = {
    "source_id",
    "source_revision",
    "source_content_sha256",
    "role",
    "trust",
    "span_start",
    "span_end",
    "visual_only",
}


def _require_object(value: object, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProposalValidationError("invalid_type", "expected object", path=path)
    return cast(Mapping[str, Any], value)


def _unknown_fields(value: Mapping[str, Any], allowed: set[str], *, path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProposalValidationError("unknown_field", f"unknown field: {unknown[0]}", path=path)


def _uuid(value: object, *, path: str) -> UUID:
    if not isinstance(value, str):
        raise ProposalValidationError("invalid_uuid", "UUID must be a string", path=path)
    try:
        return UUID(value)
    except ValueError as exc:
        raise ProposalValidationError("invalid_uuid", "UUID is malformed", path=path) from exc


def _bounded_score(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ProposalValidationError(
            "score_out_of_range", "score must be between zero and one", path=path
        )
    return float(value)


def _strict_bool(value: object, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProposalValidationError("invalid_type", "expected boolean", path=path)
    return value


def _optional_text(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProposalValidationError("invalid_type", "expected text", path=path)
    return value


def _parse_evidence(value: object, *, path: str) -> Evidence:
    raw = _require_object(value, path=path)
    _unknown_fields(raw, _EVIDENCE_FIELDS, path=path)
    required = ("source_id", "source_revision", "source_content_sha256")
    if any(name not in raw for name in required):
        raise ProposalValidationError("missing_field", "evidence field is missing", path=path)
    digest = raw["source_content_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProposalValidationError(
            "invalid_hash", "evidence hash must be 64 hex characters", path=path
        )
    try:
        source_hash = bytes.fromhex(digest)
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(
            "invalid_hash", "evidence hash is not hexadecimal", path=path
        ) from exc
    role = raw.get("role", EvidenceRole.PRIMARY)
    trust = raw.get("trust", TrustClass.USER_STATEMENT)
    try:
        role_value = EvidenceRole(role)
        trust_value = TrustClass(trust)
    except ValueError as exc:
        raise ProposalValidationError(
            "invalid_evidence_class", "evidence role or trust is invalid", path=path
        ) from exc
    revision = raw["source_revision"]
    if not isinstance(revision, str) or not revision:
        raise ProposalValidationError(
            "invalid_revision", "evidence revision is required", path=path
        )
    span_start = raw.get("span_start")
    span_end = raw.get("span_end")
    if span_start is not None and (isinstance(span_start, bool) or not isinstance(span_start, int)):
        raise ProposalValidationError("invalid_span", "span_start must be an integer", path=path)
    if span_end is not None and (isinstance(span_end, bool) or not isinstance(span_end, int)):
        raise ProposalValidationError("invalid_span", "span_end must be an integer", path=path)
    if (span_start is None) != (span_end is None):
        raise ProposalValidationError("invalid_span", "span bounds must both be present", path=path)
    visual_only = raw.get("visual_only", False)
    return Evidence(
        source_id=_uuid(raw["source_id"], path=f"{path}.source_id"),
        source_revision=revision,
        source_content_sha256=source_hash,
        role=role_value,
        trust=trust_value,
        span_start=span_start,
        span_end=span_end,
        visual_only=_strict_bool(visual_only, path=f"{path}.visual_only"),
    )


def parse_agent_response(  # noqa: PLR0915 - strict fail-closed parsing is explicit
    payload: object,
    *,
    account_id: UUID,
    conversation_id: UUID,
) -> tuple[MemoryProposal, ...]:
    """Parse strict JSON-shaped provider output; unknown keys fail closed."""

    raw = _require_object(payload, path="$")
    _unknown_fields(raw, _TOP_LEVEL, path="$")
    if raw.get("schema_version") != 1:
        raise ProposalValidationError(
            "unsupported_schema", "memory output schema must be 1", path="$.schema_version"
        )
    proposals = raw.get("proposals")
    if not isinstance(proposals, list) or len(proposals) > 100:
        raise ProposalValidationError(
            "invalid_proposals", "proposals must be a list of at most 100", path="$.proposals"
        )
    parsed: list[MemoryProposal] = []
    for index, item in enumerate(proposals):
        path = f"$.proposals[{index}]"
        value = _require_object(item, path=path)
        _unknown_fields(value, _PROPOSAL_FIELDS, path=path)
        required = {
            "operation",
            "memory_type",
            "semantic_key",
            "payload",
            "confidence",
            "importance",
            "evidence",
        }
        if required - set(value):
            raise ProposalValidationError("missing_field", "proposal field is missing", path=path)
        try:
            operation = MemoryOperation(value["operation"])
            memory_type = MemoryType(value["memory_type"])
        except (TypeError, ValueError) as exc:
            raise ProposalValidationError(
                "invalid_enum", "operation or memory type is invalid", path=path
            ) from exc
        if not isinstance(value["semantic_key"], str) or not value["semantic_key"].strip():
            raise ProposalValidationError(
                "invalid_semantic_key", "semantic key is required", path=f"{path}.semantic_key"
            )
        proposal_id = (
            _uuid(value["id"], path=f"{path}.id") if "id" in value else UUID(int=index + 1)
        )
        evidence_raw = value["evidence"]
        if not isinstance(evidence_raw, list) or not evidence_raw or len(evidence_raw) > 32:
            raise ProposalValidationError(
                "invalid_evidence", "one to 32 evidence items are required", path=f"{path}.evidence"
            )
        evidence = tuple(
            _parse_evidence(item, path=f"{path}.evidence[{n}]")
            for n, item in enumerate(evidence_raw)
        )
        evidence_keys = [(item.source_id, item.role) for item in evidence]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ProposalValidationError(
                "duplicate_evidence",
                "evidence source and role pairs must be unique",
                path=f"{path}.evidence",
            )
        targets = value.get("targets", [])
        if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
            raise ProposalValidationError(
                "invalid_targets", "targets must be UUID strings", path=f"{path}.targets"
            )
        if len(targets) != len(set(targets)):
            raise ProposalValidationError(
                "duplicate_target", "target IDs must be unique", path=f"{path}.targets"
            )
        parsed_payload = _require_object(value["payload"], path=f"{path}.payload")
        rendered_text = _optional_text(value.get("rendered_text"), path=f"{path}.rendered_text")
        confidence = _bounded_score(value["confidence"], path=f"{path}.confidence")
        importance = _bounded_score(value["importance"], path=f"{path}.importance")
        target_ids = tuple(_uuid(item, path=f"{path}.targets") for item in targets)
        valid_from = _parse_datetime(value.get("valid_from"), path=f"{path}.valid_from")
        valid_to = _parse_datetime(value.get("valid_to"), path=f"{path}.valid_to")
        visual_only = _strict_bool(value.get("visual_only", False), path=f"{path}.visual_only")
        try:
            parsed_proposal = MemoryProposal(
                id=proposal_id,
                account_id=account_id,
                conversation_id=conversation_id,
                operation=operation,
                memory_type=memory_type,
                semantic_key=value["semantic_key"],
                payload=parsed_payload,
                rendered_text=rendered_text,
                confidence=confidence,
                importance=importance,
                evidence=evidence,
                target_memory_ids=target_ids,
                valid_from=valid_from,
                valid_to=valid_to,
                visual_only=visual_only,
            )
        except ValueError as exc:
            raise ProposalValidationError(
                "invalid_proposal", "proposal values are inconsistent", path=path
            ) from exc
        parsed.append(parsed_proposal)
    proposal_ids = [proposal.id for proposal in parsed]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ProposalValidationError(
            "duplicate_proposal", "proposal IDs must be unique", path="$.proposals"
        )
    return tuple(parsed)


def _parse_datetime(value: object, *, path: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProposalValidationError("invalid_time", "timestamp must be ISO text", path=path)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProposalValidationError("invalid_time", "timestamp is malformed", path=path) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProposalValidationError("invalid_time", "timestamp must include timezone", path=path)
    return parsed


def _contains_instruction_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in {"system", "developer", "instruction", "instructions", "role"}
            or _contains_instruction_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_instruction_key(item) for item in value)
    return False


def validate_proposal(  # noqa: PLR0912 - each fail-closed evidence rule is explicit
    proposal: MemoryProposal, manifest: InputManifest
) -> ValidatedProposal:
    """Validate scope/coverage and apply the auto/candidate/rejected policy."""

    issues: list[ValidationIssue] = []
    sources = {source.source_id: source for source in manifest.sources}
    if (
        proposal.account_id != manifest.account_id
        or proposal.conversation_id != manifest.conversation_id
    ):
        issues.append(
            ValidationIssue("scope_mismatch", "scope", "proposal scope differs from manifest")
        )
    if proposal.operation is MemoryOperation.CREATE and proposal.target_memory_ids:
        issues.append(
            ValidationIssue(
                "create_has_target", "targets", "create cannot target an existing memory"
            )
        )
    if proposal.operation is not MemoryOperation.CREATE and not proposal.target_memory_ids:
        issues.append(ValidationIssue("missing_target", "targets", "operation requires a target"))
    if not proposal.evidence:
        issues.append(ValidationIssue("missing_evidence", "evidence", "evidence is required"))
    visual_source = False
    for index, evidence in enumerate(proposal.evidence):
        source = sources.get(evidence.source_id)
        if source is None:
            issues.append(
                ValidationIssue("source_not_in_manifest", f"evidence[{index}]", "source is absent")
            )
            continue
        if source.redacted or source.revision != evidence.source_revision:
            issues.append(
                ValidationIssue(
                    "source_revision_changed", f"evidence[{index}]", "source is not current"
                )
            )
        if source.source_type != "message_revision":
            issues.append(
                ValidationIssue(
                    "evidence_root_unverified",
                    f"evidence[{index}]",
                    "derived evidence must first resolve to a canonical message revision",
                )
            )
        if source.trust is not evidence.trust:
            issues.append(
                ValidationIssue(
                    "source_trust_mismatch",
                    f"evidence[{index}]",
                    "evidence trust differs from the canonical source",
                )
            )
        if source.content_sha256 != evidence.source_content_sha256:
            issues.append(
                ValidationIssue("source_hash_mismatch", f"evidence[{index}]", "source hash differs")
            )
        if evidence.span_end is not None and evidence.span_end > len(source.content):
            issues.append(
                ValidationIssue(
                    "evidence_span_out_of_range", f"evidence[{index}]", "span exceeds source"
                )
            )
        if evidence.visual_only or source.visual_only:
            visual_source = True
            issues.append(
                ValidationIssue(
                    "visual_only", f"evidence[{index}]", "visual evidence requires review"
                )
            )
    if proposal.rendered_text is not None and len(proposal.rendered_text) > 20_000:
        issues.append(
            ValidationIssue("text_too_long", "rendered_text", "rendered text is too long")
        )
    if _contains_instruction_key(proposal.payload):
        issues.append(
            ValidationIssue(
                "instruction_payload", "payload", "payload cannot contain instruction fields"
            )
        )
    if (
        proposal.operation
        in {MemoryOperation.UPDATE, MemoryOperation.SUPERSEDE, MemoryOperation.INVALIDATE}
        and len(proposal.target_memory_ids) != 1
    ):
        issues.append(
            ValidationIssue("target_count_invalid", "targets", "operation requires one target")
        )
    if proposal.operation is MemoryOperation.MERGE and len(proposal.target_memory_ids) < 2:
        issues.append(
            ValidationIssue("target_count_invalid", "targets", "merge requires multiple targets")
        )
    if proposal.operation is MemoryOperation.INVALIDATE and proposal.confidence < 0.85:
        issues.append(
            ValidationIssue(
                "unsafe_invalidation", "confidence", "invalidation needs high confidence"
            )
        )

    visual = (
        proposal.visual_only or visual_source or any(item.visual_only for item in proposal.evidence)
    )
    conflicting = any(item.role is EvidenceRole.CONTRADICTING for item in proposal.evidence)
    trusted = all(
        item.trust in {TrustClass.USER_STATEMENT, TrustClass.OBSERVED} for item in proposal.evidence
    )
    hard_issues = tuple(issue for issue in issues if issue.code != "visual_only")
    if hard_issues:
        return ValidatedProposal(proposal, ProposalState.REJECTED, hard_issues)
    if proposal.confidence < 0.60:
        return ValidatedProposal(
            proposal,
            ProposalState.REJECTED,
            (
                ValidationIssue(
                    "confidence_low", "confidence", "confidence is below candidate floor"
                ),
            ),
        )
    if proposal.confidence >= 0.85 and trusted and not conflicting and not visual:
        return ValidatedProposal(proposal, ProposalState.ACCEPTED)
    return ValidatedProposal(
        proposal,
        ProposalState.CANDIDATE,
        tuple(
            issue
            for issue in (
                ValidationIssue("manual_review_required", "confidence", "proposal requires review")
                if proposal.confidence < 0.85
                else None,
                ValidationIssue(
                    "conflicting_evidence", "evidence", "conflicting evidence requires review"
                )
                if conflicting
                else None,
                ValidationIssue("visual_only", "evidence", "visual-only proposal requires review")
                if visual
                else None,
            )
            if issue is not None
        ),
    )


def validate_response_json(
    raw_json: str, *, account_id: UUID, conversation_id: UUID
) -> tuple[MemoryProposal, ...]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ProposalValidationError("malformed_json", "provider output is not JSON") from exc
    return parse_agent_response(payload, account_id=account_id, conversation_id=conversation_id)
