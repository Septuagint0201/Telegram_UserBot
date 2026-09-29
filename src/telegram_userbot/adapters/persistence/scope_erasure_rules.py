"""Frozen v1 payload erasure rules shared by metadata and migration 0030.

Identity, authorization and external-outcome fields are deliberately excluded.
Changes to these rules require a new migration, not editing the v1 contract.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PayloadErasureRule:
    null_columns: tuple[str, ...]
    empty_objects: tuple[str, ...] = ()
    originally_required: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self.null_columns + self.empty_objects


PAYLOAD_ERASURE_V1: dict[str, PayloadErasureRule] = {
    "memories": PayloadErasureRule(("semantic_key_hash",), (), ("semantic_key_hash",)),
    "memory_versions": PayloadErasureRule(
        ("rendered_text", "observed_at", "valid_from", "valid_to", "timezone"), ("payload",), ()
    ),
    "memory_proposals": PayloadErasureRule(
        ("semantic_key_hash", "proposed_text", "proposed_valid_from", "proposed_valid_to"),
        ("proposed_payload",),
        ("semantic_key_hash",),
    ),
    "memory_evidence": PayloadErasureRule(
        ("source_content_sha256",), (), ("source_content_sha256",)
    ),
    "memory_proposal_evidence": PayloadErasureRule(
        ("source_content_sha256", "quoted_span_start", "quoted_span_end"),
        (),
        ("source_content_sha256",),
    ),
    "summaries": PayloadErasureRule(
        ("period_key", "timezone_snapshot", "period_start_at", "period_end_at"), (), ()
    ),
    "summary_versions": PayloadErasureRule(
        (
            "content_text",
            "content_sha256",
            "manifest_sha256",
            "period_start_at",
            "period_end_at",
            "timezone_snapshot",
        ),
        (),
        ("manifest_sha256",),
    ),
    "summary_version_sources": PayloadErasureRule(
        ("source_content_sha256",), (), ("source_content_sha256",)
    ),
    "copilot_draft_revisions": PayloadErasureRule(("content_text", "content_sha256"), (), ()),
    "outbound_intents": PayloadErasureRule(
        ("text_content", "payload_sha256"), (), ("text_content", "payload_sha256")
    ),
    "outbound_delivery_groups": PayloadErasureRule(("logical_content_sha256",), (), ()),
    "model_runs": PayloadErasureRule(
        ("input_fingerprint", "output_fingerprint", "error_detail_redacted"),
        (),
        ("input_fingerprint",),
    ),
    "memory_input_manifests": PayloadErasureRule(("manifest_sha256",), (), ("manifest_sha256",)),
    "memory_input_manifest_items": PayloadErasureRule(
        ("source_content_sha256",), (), ("source_content_sha256",)
    ),
    "context_manifests": PayloadErasureRule(
        ("source_revision_vector_sha256", "manifest_sha256"),
        (),
        ("source_revision_vector_sha256", "manifest_sha256"),
    ),
    "context_manifest_items": PayloadErasureRule(
        ("content_sha256", "rendered_part_sha256", "score_features"),
        (),
        ("content_sha256", "rendered_part_sha256"),
    ),
    "proactive_life_events": PayloadErasureRule(("source_hash",), ("payload",), ("source_hash",)),
    "proactive_intentions": PayloadErasureRule(("source_hash",), ("payload",), ("source_hash",)),
    "proactive_relationships": PayloadErasureRule(("source_hash",), ("payload",), ("source_hash",)),
    "proactive_occurrence_evidence": PayloadErasureRule(
        ("source_hash", "summary"), (), ("source_hash", "summary")
    ),
    "proactive_decisions": PayloadErasureRule(("topic", "output_hash"), (), ("output_hash",)),
    "proactive_input_manifests": PayloadErasureRule(
        ("decision_snapshot", "decision_snapshot_sha256", "manifest_sha256"), (), ()
    ),
    "proactive_input_manifest_items": PayloadErasureRule(
        ("source_hash", "summary"), (), ("source_hash", "summary")
    ),
}
