"""Frozen 0034 retention boundary: preferences are not receipt evidence."""

from telegram_userbot.adapters.persistence.metadata_erasure_rules import MetadataErasureRule

RETENTION_ERASURE_V1 = {
    "proactive_policies": MetadataErasureRule(
        ("timezone_name", "quiet_start_local", "quiet_end_local"),
        ("settings_json",),
        ("enabled",),
        ("timezone_name", "quiet_start_local", "quiet_end_local"),
    ),
    "proactive_contact_settings": MetadataErasureRule(
        ("relationship_level", "timezone_name", "daily_limit", "minimum_interval_seconds"),
        false_columns=("enabled",),
        originally_required=("relationship_level",),
    ),
    "proactive_occurrences": MetadataErasureRule(
        ("timezone_name",), originally_required=("timezone_name",)
    ),
    "proactive_candidates": MetadataErasureRule(
        ("timezone_name",), originally_required=("timezone_name",)
    ),
    "proactive_budget_buckets": MetadataErasureRule(
        ("timezone_name_snapshot",), originally_required=("timezone_name_snapshot",)
    ),
    "data_erasure_requests": MetadataErasureRule(
        ("requested_by",), originally_required=("requested_by",)
    ),
    "memory_proposals": MetadataErasureRule(("decision_actor_id",)),
    **{
        name: MetadataErasureRule(
            ("timezone_name",)
            if name == "proactive_decisions"
            else ("timezone_name", "valid_from_at", "valid_until_at"),
            originally_required=("timezone_name",),
        )
        for name in (
            "proactive_life_events",
            "proactive_intentions",
            "proactive_relationships",
            "proactive_decisions",
        )
    },
}
