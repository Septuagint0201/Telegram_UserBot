"""Frozen 0032 metadata redaction contract. Routing and receipt IDs are retained."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataErasureRule:
    null_columns: tuple[str, ...] = ()
    empty_objects: tuple[str, ...] = ()
    false_columns: tuple[str, ...] = ()
    originally_required: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self.null_columns + self.empty_objects + self.false_columns


METADATA_ERASURE_V1 = {
    "accounts": MetadataErasureRule(
        ("display_label", "default_timezone"),
        originally_required=("display_label", "default_timezone"),
    ),
    "account_peers": MetadataErasureRule(
        ("access_hash", "username", "display_name", "last_observed_at"),
        ("metadata",),
        ("observed_is_contact",),
        ("last_observed_at",),
    ),
    "contacts": MetadataErasureRule(("timezone", "locale")),
    "conversations": MetadataErasureRule(
        ("temporary_human_until", "base_mode_override", "last_message_at", "last_completed_turn_at")
    ),
    "messages": MetadataErasureRule(
        ("sender_account_peer_id", "reply_to_telegram_message_id", "grouped_id"), ("metadata",)
    ),
    "message_events": MetadataErasureRule(("grouped_id",), ("metadata",)),
    "message_media": MetadataErasureRule(
        (
            "telegram_file_ref",
            "declared_mime",
            "declared_size",
            "duration_ms",
            "original_name_sanitized",
        ),
        ("metadata",),
    ),
    "model_runs": MetadataErasureRule(("provider_request_id",)),
    "model_run_attempts": MetadataErasureRule(("provider_request_id",)),
    "account_orchestrator_states": MetadataErasureRule(
        ("updated_by",), originally_required=("updated_by",)
    ),
    "conversation_mode_history": MetadataErasureRule(
        ("actor_ref", "reason"), originally_required=("reason",)
    ),
    "account_control_history": MetadataErasureRule(
        ("actor_ref", "reason"), originally_required=("reason",)
    ),
    "copilot_drafts": MetadataErasureRule(("requested_by",), originally_required=("requested_by",)),
    "control_commands": MetadataErasureRule(("result_payload",)),
    "transactional_outbox": MetadataErasureRule(empty_objects=("payload",)),
    "background_jobs": MetadataErasureRule(empty_objects=("payload",)),
    "audit_log": MetadataErasureRule(("actor_ref", "before_sha256", "after_sha256"), ("metadata",)),
}

# These rows have no receipt or recovery identity that must survive erasure.
METADATA_DELETE_V1 = ("message_reactions", "copilot_edit_sessions")
