"""SQLAlchemy Core schema for the M1 durable-state boundary.

PostgreSQL is the canonical source of durable facts.  This module is imported by
Alembic and repositories, but application startup never calls ``create_all``.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)

UUID_TYPE = UUID(as_uuid=True)
UTC_TIMESTAMP = DateTime(timezone=True)
NOW = text("CURRENT_TIMESTAMP")
EMPTY_JSON = text("'{}'::jsonb")


accounts = Table(
    "accounts",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("telegram_user_id", BigInteger, nullable=False, unique=True),
    Column("display_label", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("default_timezone", Text, nullable=False, server_default=text("'UTC'")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("deleted_at", UTC_TIMESTAMP),
    CheckConstraint(
        "status IN ('bootstrap_required','active','disabled','deleting')",
        name="status_values",
    ),
    UniqueConstraint("id", "telegram_user_id", name="uq_accounts_id_telegram_user_id"),
)

telegram_peers = Table(
    "telegram_peers",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("peer_type", Text, nullable=False),
    Column("telegram_peer_id", BigInteger, nullable=False),
    Column("is_bot", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("peer_type IN ('user','chat','channel')", name="peer_type_values"),
    UniqueConstraint("peer_type", "telegram_peer_id", name="uq_telegram_peers_type_native_id"),
)

account_peers = Table(
    "account_peers",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_account_peers_account_id_accounts"),
        nullable=False,
    ),
    Column(
        "peer_id",
        UUID_TYPE,
        ForeignKey("telegram_peers.id", name="fk_account_peers_peer_id_telegram_peers"),
        nullable=False,
    ),
    Column("access_hash", BigInteger),
    Column("username", Text),
    Column("display_name", Text),
    Column("observed_is_contact", Boolean, nullable=False, server_default=text("false")),
    Column("last_observed_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("metadata_schema_version", SmallInteger, nullable=False, server_default=text("1")),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    UniqueConstraint("account_id", "peer_id", name="uq_account_peers_account_peer"),
    UniqueConstraint("id", "account_id", name="uq_account_peers_id_account"),
)

contacts = Table(
    "contacts",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("account_peer_id", UUID_TYPE, nullable=False),
    Column("automation_status", Text, nullable=False, server_default=text("'review'")),
    Column("proactive_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("timezone", Text),
    Column("locale", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("deleted_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["account_peer_id", "account_id"],
        ["account_peers.id", "account_peers.account_id"],
        name="fk_contacts_peer_account",
    ),
    CheckConstraint(
        "automation_status IN ('allowed','blocked','review','deleting')",
        name="automation_status_values",
    ),
    UniqueConstraint("account_id", "account_peer_id", name="uq_contacts_account_peer"),
    UniqueConstraint("id", "account_id", name="uq_contacts_id_account"),
    UniqueConstraint("id", "account_id", "account_peer_id", name="uq_contacts_id_account_peer"),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("account_peer_id", UUID_TYPE, nullable=False),
    Column("telegram_chat_id", BigInteger, nullable=False),
    Column("base_mode_override", Text),
    Column("contact_paused", Boolean, nullable=False, server_default=text("false")),
    Column("temporary_human_until", UTC_TIMESTAMP),
    Column("mode_version", BigInteger, nullable=False, server_default=text("1")),
    Column("content_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("automation_resume_floor_event_id", BigInteger),
    Column("last_response_covered_event_id", BigInteger),
    Column("last_message_at", UTC_TIMESTAMP),
    Column("last_completed_turn_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("deleted_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["contact_id", "account_id", "account_peer_id"],
        ["contacts.id", "contacts.account_id", "contacts.account_peer_id"],
        name="fk_conversations_contact_scope",
    ),
    CheckConstraint(
        "base_mode_override IS NULL OR base_mode_override IN ('AUTO','HUMAN','COPILOT')",
        name="base_mode_values",
    ),
    CheckConstraint("mode_version >= 1", name="mode_version_positive"),
    CheckConstraint("content_revision >= 0", name="content_revision_nonnegative"),
    UniqueConstraint("account_id", "account_peer_id", name="uq_conversations_account_peer"),
    UniqueConstraint("account_id", "telegram_chat_id", name="uq_conversations_account_chat"),
    UniqueConstraint("id", "account_id", name="uq_conversations_id_account"),
    UniqueConstraint(
        "id",
        "account_id",
        "contact_id",
        name="uq_conversations_id_account_contact",
    ),
    UniqueConstraint(
        "id",
        "account_id",
        "contact_id",
        "account_peer_id",
        name="uq_conversations_full_scope",
    ),
)

message_events = Table(
    "message_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("event_uuid", UUID_TYPE, nullable=False, unique=True),
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_message_events_account_id_accounts"),
        nullable=False,
    ),
    Column("conversation_id", UUID_TYPE),
    Column("event_kind", Text, nullable=False),
    Column("telegram_message_id", BigInteger),
    Column("grouped_id", BigInteger),
    Column("fingerprint_version", SmallInteger, nullable=False),
    Column("update_fingerprint", LargeBinary, nullable=False),
    Column("telegram_event_at", UTC_TIMESTAMP),
    Column("observed_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("ordering_key", Text, nullable=False),
    Column("metadata_schema_version", SmallInteger, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("projected_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_message_events_conversation_scope",
    ),
    CheckConstraint("octet_length(update_fingerprint) = 32", name="fingerprint_32_bytes"),
    CheckConstraint("fingerprint_version > 0", name="fingerprint_version_positive"),
    UniqueConstraint(
        "account_id",
        "fingerprint_version",
        "update_fingerprint",
        name="uq_message_events_fingerprint",
    ),
    UniqueConstraint("id", "account_id", name="uq_message_events_id_account"),
    UniqueConstraint(
        "id",
        "account_id",
        "conversation_id",
        name="uq_message_events_full_scope",
    ),
)
Index("ix_message_events_conversation_id", message_events.c.conversation_id, message_events.c.id)

messages = Table(
    "messages",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("telegram_message_id", BigInteger, nullable=False),
    Column("sender_account_peer_id", UUID_TYPE),
    Column("direction", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("source_status", Text, nullable=False),
    Column("current_revision_no", Integer, nullable=False),
    Column("grouped_id", BigInteger),
    Column("reply_to_telegram_message_id", BigInteger),
    Column("telegram_created_at", UTC_TIMESTAMP, nullable=False),
    Column("edited_at", UTC_TIMESTAMP),
    Column("deleted_at", UTC_TIMESTAMP),
    Column("is_tombstone", Boolean, nullable=False, server_default=text("false")),
    Column("first_observed_at", UTC_TIMESTAMP, nullable=False),
    Column("last_observed_at", UTC_TIMESTAMP, nullable=False),
    Column("metadata_schema_version", SmallInteger, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_messages_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["sender_account_peer_id", "account_id"],
        ["account_peers.id", "account_peers.account_id"],
        name="fk_messages_sender_scope",
    ),
    CheckConstraint("direction IN ('incoming','outgoing')", name="direction_values"),
    CheckConstraint("role IN ('user','assistant','system')", name="role_values"),
    CheckConstraint(
        "source IN ('telegram_user','ai','proactive_ai','copilot_approved',"
        "'human','system_pending','system')",
        name="source_values",
    ),
    CheckConstraint(
        "source_status IN ('resolved','pending','corrected')", name="source_status_values"
    ),
    CheckConstraint("current_revision_no >= 1", name="current_revision_positive"),
    CheckConstraint("is_tombstone = (deleted_at IS NOT NULL)", name="tombstone_matches_delete"),
    UniqueConstraint(
        "account_id",
        "conversation_id",
        "telegram_message_id",
        name="uq_messages_business_key",
    ),
    UniqueConstraint("id", "account_id", name="uq_messages_id_account"),
    UniqueConstraint(
        "id", "account_id", "conversation_id", name="uq_messages_id_conversation_scope"
    ),
)
Index(
    "ix_messages_conversation_created",
    messages.c.conversation_id,
    messages.c.telegram_created_at.desc(),
    messages.c.telegram_message_id.desc(),
)
Index(
    "ix_messages_album",
    messages.c.conversation_id,
    messages.c.grouped_id,
    messages.c.telegram_message_id,
    postgresql_where=messages.c.grouped_id.is_not(None),
)

message_revisions = Table(
    "message_revisions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("message_id", UUID_TYPE, nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("body_kind", Text, nullable=False),
    Column("text_content", Text),
    Column("caption", Text),
    Column("entities_schema_version", SmallInteger, nullable=False),
    Column("entities", JSONB),
    Column("content_sha256", LargeBinary),
    Column("source_event_id", BigInteger, nullable=False),
    Column("telegram_edited_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("redacted_at", UTC_TIMESTAMP),
    Column("redaction_reason", Text),
    ForeignKeyConstraint(
        ["message_id", "account_id"],
        ["messages.id", "messages.account_id"],
        name="fk_message_revisions_message_scope",
    ),
    ForeignKeyConstraint(
        ["source_event_id", "account_id"],
        ["message_events.id", "message_events.account_id"],
        name="fk_message_revisions_source_event_scope",
    ),
    CheckConstraint("revision_no >= 1", name="revision_positive"),
    CheckConstraint(
        "(redacted_at IS NOT NULL AND text_content IS NULL AND caption IS NULL) OR "
        "(redacted_at IS NULL AND body_kind = 'text' AND "
        "text_content IS NOT NULL AND caption IS NULL) OR "
        "(redacted_at IS NULL AND body_kind = 'caption' AND "
        "caption IS NOT NULL AND text_content IS NULL) OR "
        "(redacted_at IS NULL AND body_kind = 'none' AND "
        "text_content IS NULL AND caption IS NULL)",
        name="body_kind_matches_content",
    ),
    CheckConstraint(
        "(redacted_at IS NULL AND redaction_reason IS NULL) OR "
        "(redacted_at IS NOT NULL AND redaction_reason IN "
        "('telegram_delete','contact_purge','account_wipe','policy') AND "
        "text_content IS NULL AND caption IS NULL AND entities IS NULL AND "
        "content_sha256 IS NULL)",
        name="redaction_is_content_free",
    ),
    CheckConstraint(
        "content_sha256 IS NULL OR octet_length(content_sha256) = 32",
        name="content_hash_32_bytes",
    ),
    UniqueConstraint("message_id", "revision_no", name="uq_message_revisions_message_no"),
    UniqueConstraint(
        "message_id",
        "account_id",
        "revision_no",
        name="uq_message_revisions_message_account_no",
    ),
    UniqueConstraint("id", "account_id", name="uq_message_revisions_id_account"),
    UniqueConstraint("id", "account_id", "message_id", name="uq_message_revisions_full_scope"),
)
messages.append_constraint(
    ForeignKeyConstraint(
        [messages.c.id, messages.c.account_id, messages.c.current_revision_no],
        [
            message_revisions.c.message_id,
            message_revisions.c.account_id,
            message_revisions.c.revision_no,
        ],
        name="fk_messages_current_revision",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

media_objects = Table(
    "media_objects",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_media_objects_account_id_accounts"),
        nullable=False,
    ),
    Column("object_kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("storage_key", Text, unique=True),
    Column("sha256", LargeBinary),
    Column("validated_mime", Text),
    Column("byte_size", BigInteger),
    Column("width", Integer),
    Column("height", Integer),
    Column("parent_object_id", UUID_TYPE),
    Column("validation_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("ready_at", UTC_TIMESTAMP),
    Column("delete_requested_at", UTC_TIMESTAMP),
    Column("deleted_at", UTC_TIMESTAMP),
    Column("retention_class", Text, nullable=False),
    Column("expires_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["parent_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_media_objects_parent_scope",
    ),
    UniqueConstraint("id", "account_id", name="uq_media_objects_id_account"),
    CheckConstraint("object_kind IN ('original','provider_copy')", name="object_kind_values"),
    CheckConstraint(
        "status IN ('pending','ready','rejected','delete_pending','deleted','failed')",
        name="status_values",
    ),
    CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="byte_size_nonnegative"),
    CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
    CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
    CheckConstraint("sha256 IS NULL OR octet_length(sha256) = 32", name="sha256_32_bytes"),
    CheckConstraint(
        "storage_key IS NULL OR (storage_key !~ '(^/|(^|/)\\.\\.(/|$)|\\\\)')",
        name="storage_key_relative",
    ),
    CheckConstraint(
        "retention_class IN ('media_original_30d','media_provider_copy_24h')",
        name="retention_class_values",
    ),
)
Index(
    "ix_media_objects_expiry",
    media_objects.c.expires_at,
    postgresql_where=(media_objects.c.status == "ready") & media_objects.c.expires_at.is_not(None),
)

message_media = Table(
    "message_media",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("message_revision_id", UUID_TYPE, nullable=False),
    Column("media_object_id", UUID_TYPE),
    Column("media_kind", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("telegram_file_ref", Text),
    Column("declared_mime", Text),
    Column("declared_size", BigInteger),
    Column("duration_ms", BigInteger),
    Column("original_name_sanitized", Text),
    Column("metadata_schema_version", SmallInteger, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_message_media_revision_scope",
    ),
    ForeignKeyConstraint(
        ["media_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_message_media_object_scope",
    ),
    CheckConstraint(
        "media_kind IN ('photo','image_document','voice','audio','video','video_note',"
        "'document','sticker')",
        name="media_kind_values",
    ),
    CheckConstraint("position >= 0", name="position_nonnegative"),
    CheckConstraint("declared_size IS NULL OR declared_size >= 0", name="size_nonnegative"),
    CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
    CheckConstraint(
        "original_name_sanitized IS NULL OR original_name_sanitized !~ '[/\\\\]'",
        name="original_name_has_no_path",
    ),
    UniqueConstraint("message_revision_id", "position", name="uq_message_media_revision_position"),
)

message_reactions = Table(
    "message_reactions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("message_id", UUID_TYPE, nullable=False),
    Column("actor_peer_id", UUID_TYPE, ForeignKey("telegram_peers.id")),
    Column("reaction_key", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["message_id", "account_id"],
        ["messages.id", "messages.account_id"],
        name="fk_message_reactions_message_scope",
    ),
)
Index(
    "uq_message_reactions_actor",
    message_reactions.c.message_id,
    message_reactions.c.actor_peer_id,
    message_reactions.c.reaction_key,
    unique=True,
    postgresql_where=message_reactions.c.actor_peer_id.is_not(None),
)
Index(
    "uq_message_reactions_aggregate",
    message_reactions.c.message_id,
    message_reactions.c.reaction_key,
    unique=True,
    postgresql_where=message_reactions.c.actor_peer_id.is_(None),
)

account_orchestrator_states = Table(
    "account_orchestrator_states",
    metadata,
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_account_orchestrator_states_account_id_accounts"),
        primary_key=True,
    ),
    Column("default_base_mode", Text, nullable=False, server_default=text("'HUMAN'")),
    Column("global_paused", Boolean, nullable=False, server_default=text("false")),
    Column("maintenance_state", Text, nullable=False, server_default=text("'inactive'")),
    Column("temporary_takeover_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("temporary_takeover_seconds", Integer, nullable=False, server_default=text("600")),
    Column("resume_pending_policy", Text, nullable=False, server_default=text("'ignore'")),
    Column("resume_floor_event_id", BigInteger),
    Column("control_version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("default_base_mode IN ('AUTO','HUMAN','COPILOT')", name="default_mode_values"),
    CheckConstraint(
        "maintenance_state IN ('inactive','draining','active')", name="maintenance_values"
    ),
    CheckConstraint("temporary_takeover_seconds > 0", name="takeover_seconds_positive"),
    CheckConstraint("resume_pending_policy = 'ignore'", name="resume_policy_v1"),
    CheckConstraint("control_version >= 1", name="control_version_positive"),
)

conversation_mode_history = Table(
    "conversation_mode_history",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("mode_version", BigInteger, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("previous_state", Text),
    Column("new_state", Text),
    Column("reason", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_ref", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_conversation_mode_history_scope",
    ),
    CheckConstraint("actor_type IN ('admin','human','system')", name="actor_type_values"),
    UniqueConstraint(
        "conversation_id", "mode_version", name="uq_conversation_mode_history_version"
    ),
)

account_control_history = Table(
    "account_control_history",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_account_control_history_account_id_accounts"),
        nullable=False,
    ),
    Column("control_version", BigInteger, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("previous_state", Text),
    Column("new_state", Text),
    Column("reason", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_ref", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    UniqueConstraint("account_id", "control_version", name="uq_account_control_history_version"),
)

conversation_turns = Table(
    "conversation_turns",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("state", Text, nullable=False),
    Column("trigger_kind", Text, nullable=False),
    Column("supersedes_turn_id", UUID_TYPE),
    Column("collection_sequence", BigInteger, nullable=False),
    Column("active_generation_no", Integer, nullable=False, server_default=text("0")),
    Column("base_mode_snapshot", Text),
    Column("base_mode_source_snapshot", Text),
    Column("effective_mode_snapshot", Text),
    Column("account_control_version_snapshot", BigInteger),
    Column("mode_version_snapshot", BigInteger),
    Column("content_revision_snapshot", BigInteger),
    Column("resume_floor_event_id_snapshot", BigInteger),
    Column("coverage_event_id_snapshot", BigInteger),
    Column("debounce_seconds", Integer, nullable=False, server_default=text("3")),
    Column("hard_cap_seconds", Integer, nullable=False, server_default=text("10")),
    Column("collect_started_at", UTC_TIMESTAMP),
    Column("quiet_deadline_at", UTC_TIMESTAMP),
    Column("hard_deadline_at", UTC_TIMESTAMP),
    Column("sealed_at", UTC_TIMESTAMP),
    Column("lease_owner", UUID_TYPE),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("terminal_reason", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_conversation_turns_scope",
    ),
    ForeignKeyConstraint(
        ["supersedes_turn_id", "account_id", "conversation_id"],
        [
            "conversation_turns.id",
            "conversation_turns.account_id",
            "conversation_turns.conversation_id",
        ],
        name="fk_conversation_turns_supersedes_scope",
    ),
    CheckConstraint(
        "state IN ('collecting','ready','generating','output_ready','superseded',"
        "'completed','cancelled','failed')",
        name="state_values",
    ),
    CheckConstraint(
        "trigger_kind IN ('incoming','replacement','manual_pending_reply','copilot','proactive')",
        name="trigger_kind_values",
    ),
    CheckConstraint("collection_sequence >= 1", name="collection_sequence_positive"),
    CheckConstraint("active_generation_no >= 0", name="generation_nonnegative"),
    CheckConstraint("debounce_seconds > 0", name="debounce_seconds_positive"),
    CheckConstraint("hard_cap_seconds >= debounce_seconds", name="hard_cap_not_before_debounce"),
    CheckConstraint("fencing_token >= 0", name="fencing_token_nonnegative"),
    CheckConstraint(
        "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="lease_fields_match",
    ),
    UniqueConstraint(
        "conversation_id", "collection_sequence", name="uq_conversation_turns_sequence"
    ),
    UniqueConstraint("id", "account_id", name="uq_conversation_turns_id_account"),
    UniqueConstraint(
        "id", "account_id", "conversation_id", name="uq_conversation_turns_full_scope"
    ),
)
Index(
    "ix_conversation_turns_active",
    conversation_turns.c.conversation_id,
    conversation_turns.c.state,
    conversation_turns.c.quiet_deadline_at,
)
Index(
    "uq_conversation_turns_collecting",
    conversation_turns.c.conversation_id,
    unique=True,
    postgresql_where=conversation_turns.c.state == "collecting",
)
Index(
    "uq_conversation_turns_processing",
    conversation_turns.c.conversation_id,
    unique=True,
    postgresql_where=conversation_turns.c.state.in_(("ready", "generating", "output_ready")),
)

background_jobs = Table(
    "background_jobs",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, ForeignKey("accounts.id")),
    Column("queue_name", Text, nullable=False),
    Column("job_type", Text, nullable=False),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("priority", SmallInteger, nullable=False, server_default=text("0")),
    Column("payload_schema_version", SmallInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("available_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("lease_owner", UUID_TYPE),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("dispatch_generation", BigInteger, nullable=False, server_default=text("1")),
    Column("last_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    Column("expires_at", UTC_TIMESTAMP),
    CheckConstraint(
        "state IN ('pending','leased','retry_wait','succeeded','failed','dead_letter','cancelled')",
        name="state_values",
    ),
    CheckConstraint("priority BETWEEN -32768 AND 32767", name="priority_range"),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_key_32_bytes"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint("fencing_token >= 0", name="fencing_token_nonnegative"),
    CheckConstraint("dispatch_generation >= 1", name="dispatch_generation_positive"),
    CheckConstraint(
        "(state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
        name="lease_matches_state",
    ),
    UniqueConstraint("queue_name", "idempotency_key", name="uq_background_jobs_idempotency"),
    UniqueConstraint("id", "account_id", name="uq_background_jobs_id_account"),
)
Index(
    "ix_background_jobs_claim",
    background_jobs.c.queue_name,
    background_jobs.c.state,
    background_jobs.c.priority.desc(),
    background_jobs.c.available_at,
    postgresql_where=background_jobs.c.state.in_(("pending", "retry_wait")),
)
Index(
    "ix_background_jobs_lease_expiry",
    background_jobs.c.lease_expires_at,
    postgresql_where=background_jobs.c.state == "leased",
)

transactional_outbox = Table(
    "transactional_outbox",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("account_id", UUID_TYPE, ForeignKey("accounts.id")),
    Column("topic", Text, nullable=False),
    Column("aggregate_type", Text, nullable=False),
    Column("aggregate_id", Text, nullable=False),
    Column("aggregate_version", BigInteger, nullable=False),
    Column("payload_schema_version", SmallInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("published_at", UTC_TIMESTAMP),
    Column("publish_attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error_code", Text),
    CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
    CheckConstraint("publish_attempts >= 0", name="publish_attempts_nonnegative"),
    UniqueConstraint(
        "topic",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        name="uq_transactional_outbox_generation",
    ),
)
Index(
    "ix_transactional_outbox_unpublished",
    transactional_outbox.c.id,
    postgresql_where=transactional_outbox.c.published_at.is_(None),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("occurred_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("account_id", UUID_TYPE, ForeignKey("accounts.id")),
    Column("actor_type", Text, nullable=False),
    Column("actor_ref", Text),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("reason_code", Text),
    Column("request_id", UUID_TYPE, nullable=False),
    Column("before_sha256", LargeBinary),
    Column("after_sha256", LargeBinary),
    Column("metadata_schema_version", SmallInteger, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    CheckConstraint("actor_type IN ('admin','human','service','system')", name="actor_type_values"),
    CheckConstraint("result IN ('success','rejected','failed')", name="result_values"),
)

data_erasure_requests = Table(
    "data_erasure_requests",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column(
        "account_id",
        UUID_TYPE,
        ForeignKey("accounts.id", name="fk_data_erasure_requests_account_id_accounts"),
        nullable=False,
    ),
    Column("scope_type", Text, nullable=False),
    # Account-scoped target FKs are added by 0011 after M6 memories exist.
    Column("memory_id", UUID_TYPE),
    Column("contact_id", UUID_TYPE),
    Column("state", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("request_idempotency_key", LargeBinary, nullable=False),
    Column("policy_version", BigInteger, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    Column("last_error_code", Text),
    CheckConstraint("scope_type IN ('memory','contact','account')", name="scope_type_values"),
    CheckConstraint(
        "(scope_type = 'memory' AND memory_id IS NOT NULL AND contact_id IS NULL) OR "
        "(scope_type = 'contact' AND memory_id IS NULL AND contact_id IS NOT NULL) OR "
        "(scope_type = 'account' AND memory_id IS NULL AND contact_id IS NULL)",
        name="scope_target_matches",
    ),
    CheckConstraint(
        "state IN ('requested','quiescing','redacting','media_cleanup','derived_cleanup',"
        "'completed','failed')",
        name="state_values",
    ),
    CheckConstraint("octet_length(request_idempotency_key) = 32", name="idempotency_key_32_bytes"),
    UniqueConstraint(
        "account_id", "request_idempotency_key", name="uq_data_erasure_requests_idempotency"
    ),
)

erasure_progress = Table(
    "erasure_progress",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "request_id",
        UUID_TYPE,
        ForeignKey("data_erasure_requests.id", name="fk_erasure_progress_request"),
        nullable=False,
    ),
    Column("step_name", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("watermark", Text),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("state IN ('pending','running','completed','failed')", name="state_values"),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_key_32_bytes"),
    UniqueConstraint("request_id", "step_name", name="uq_erasure_progress_request_step"),
    UniqueConstraint("idempotency_key", name="uq_erasure_progress_idempotency"),
)

erasure_ledger = Table(
    "erasure_ledger",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("account_scope_hmac", LargeBinary, nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("target_scope_hmac", LargeBinary, nullable=False),
    Column(
        "request_id",
        UUID_TYPE,
        ForeignKey("data_erasure_requests.id", name="fk_erasure_ledger_request"),
        nullable=False,
        unique=True,
    ),
    Column("policy_version", BigInteger, nullable=False),
    Column("completed_at", UTC_TIMESTAMP, nullable=False),
    CheckConstraint("octet_length(account_scope_hmac) = 32", name="account_hmac_32_bytes"),
    CheckConstraint("octet_length(target_scope_hmac) = 32", name="target_hmac_32_bytes"),
    CheckConstraint("scope_type IN ('memory','contact','account')", name="scope_type_values"),
)

migration_progress = Table(
    "migration_progress",
    metadata,
    Column("migration_key", Text, primary_key=True),
    Column("step_name", Text, nullable=False),
    Column("watermark", Text),
    Column("completed", Boolean, nullable=False, server_default=text("false")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("version >= 1", name="version_positive"),
)

M1_TABLES = tuple(metadata.tables)


# M2 model-control tables intentionally follow the frozen M1 table set above.
model_endpoints = Table(
    "model_endpoints",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("label", Text, nullable=False),
    Column("base_url", Text, nullable=False),
    Column("canonical_sha256", LargeBinary, nullable=False),
    Column("network_policy_id", UUID_TYPE, nullable=False),
    Column("network_policy_version", BigInteger, nullable=False),
    Column("network_category", Text, nullable=False),
    Column("created_by_admin_id", BigInteger, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("octet_length(canonical_sha256) = 32", name="canonical_hash_32_bytes"),
    CheckConstraint("network_policy_version >= 1", name="network_policy_version_positive"),
    CheckConstraint("network_category IN ('public','private')", name="network_category_values"),
    UniqueConstraint(
        "canonical_sha256",
        "network_policy_id",
        "network_policy_version",
        name="uq_model_endpoints_canonical_policy",
    ),
)

model_profiles = Table(
    "model_profiles",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("logical_role", Text, nullable=False, unique=True),
    Column("profile_kind", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'disabled'")),
    Column("active_config_version_no", Integer),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint(
        "logical_role IN ('main_ai','memory_agent','proactive_agent','embedding')",
        name="logical_role_values",
    ),
    CheckConstraint("profile_kind IN ('generation','embedding')", name="profile_kind_values"),
    CheckConstraint("state IN ('disabled','active','blocked')", name="state_values"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(
        "(logical_role = 'embedding') = (profile_kind = 'embedding')",
        name="role_matches_kind",
    ),
    CheckConstraint(
        "(state = 'active' AND active_config_version_no IS NOT NULL) OR (state <> 'active')",
        name="active_requires_config",
    ),
    UniqueConstraint("id", "logical_role", name="uq_model_profiles_id_role"),
    UniqueConstraint("id", "profile_kind", name="uq_model_profiles_id_kind"),
)

model_credentials = Table(
    "model_credentials",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column(
        "profile_id",
        UUID_TYPE,
        ForeignKey("model_profiles.id", name="fk_model_credentials_profile_id_model_profiles"),
        nullable=False,
        unique=True,
    ),
    Column("status", Text, nullable=False, server_default=text("'missing'")),
    Column("active_version_no", Integer),
    Column("latest_version_no", Integer, nullable=False, server_default=text("0")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("status IN ('missing','active','deleted')", name="status_values"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint("latest_version_no >= 0", name="latest_version_nonnegative"),
    CheckConstraint(
        "active_version_no IS NULL OR active_version_no <= latest_version_no",
        name="active_not_after_latest",
    ),
    CheckConstraint(
        "(status = 'active') = (active_version_no IS NOT NULL)",
        name="active_version_matches_status",
    ),
    UniqueConstraint("id", "profile_id", name="uq_model_credentials_id_profile"),
)

model_credential_versions = Table(
    "model_credential_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("credential_id", UUID_TYPE, nullable=False),
    Column("profile_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("algorithm", Text, nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("aad_schema_version", SmallInteger, nullable=False),
    Column("nonce", LargeBinary),
    Column("ciphertext", LargeBinary),
    Column("secret_fingerprint", LargeBinary),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("destroyed_at", UTC_TIMESTAMP),
    Column("destroy_reason", Text),
    ForeignKeyConstraint(
        ["credential_id", "profile_id"],
        ["model_credentials.id", "model_credentials.profile_id"],
        name="fk_model_credential_versions_credential_scope",
    ),
    CheckConstraint("version_no >= 1", name="version_no_positive"),
    CheckConstraint("key_version >= 1", name="key_version_positive"),
    CheckConstraint("aad_schema_version >= 1", name="aad_version_positive"),
    CheckConstraint("algorithm = 'aes_256_gcm'", name="algorithm_supported"),
    CheckConstraint(
        "(destroyed_at IS NULL AND destroy_reason IS NULL AND nonce IS NOT NULL AND "
        "ciphertext IS NOT NULL AND secret_fingerprint IS NOT NULL) OR "
        "(destroyed_at IS NOT NULL AND destroy_reason IS NOT NULL AND nonce IS NULL AND "
        "ciphertext IS NULL AND secret_fingerprint IS NULL)",
        name="destroy_is_one_way_redaction",
    ),
    CheckConstraint("nonce IS NULL OR octet_length(nonce) = 12", name="nonce_12_bytes"),
    CheckConstraint(
        "secret_fingerprint IS NULL OR octet_length(secret_fingerprint) = 32",
        name="fingerprint_32_bytes",
    ),
    UniqueConstraint(
        "credential_id", "version_no", name="uq_model_credential_versions_credential_no"
    ),
    UniqueConstraint(
        "credential_id",
        "profile_id",
        "version_no",
        name="uq_model_credential_versions_scope_no",
    ),
)

model_credentials.append_constraint(
    ForeignKeyConstraint(
        [
            model_credentials.c.id,
            model_credentials.c.profile_id,
            model_credentials.c.active_version_no,
        ],
        [
            model_credential_versions.c.credential_id,
            model_credential_versions.c.profile_id,
            model_credential_versions.c.version_no,
        ],
        name="fk_model_credentials_active_version",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

model_capability_snapshots = Table(
    "model_capability_snapshots",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("endpoint_id", UUID_TYPE, ForeignKey("model_endpoints.id"), nullable=False),
    Column("protocol", Text, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("supports_text", Boolean, nullable=False),
    Column("supports_temperature", Boolean, nullable=False),
    Column("supports_reasoning_effort", Boolean, nullable=False),
    Column("supports_image", Boolean, nullable=False),
    Column("supports_stream", Boolean, nullable=False),
    Column("supports_structured_output", Boolean, nullable=False),
    Column("chat_token_limit_field", Text),
    Column("max_context_tokens", Integer, nullable=False),
    Column("max_output_tokens_limit", Integer),
    Column("max_images_per_request", Integer, nullable=False, server_default=text("10")),
    Column(
        "max_image_bytes_per_request",
        BigInteger,
        nullable=False,
        server_default=text("20971520"),
    ),
    Column("auto_image_tokens", Integer, nullable=False, server_default=text("2048")),
    Column(
        "messages_auto_detail_equivalent", Boolean, nullable=False, server_default=text("false")
    ),
    Column("supported_input_roles", JSONB, nullable=False),
    Column("embedding_dimensions", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("status", Text, nullable=False),
    Column("metadata_schema_version", SmallInteger, nullable=False, server_default=text("1")),
    Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("observed_at", UTC_TIMESTAMP, nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    CheckConstraint(
        "protocol IN ('openai_responses','openai_chat_completions',"
        "'anthropic_messages','embedding')",
        name="protocol_values",
    ),
    CheckConstraint("status IN ('valid','invalid','unreachable')", name="status_values"),
    CheckConstraint("expires_at > observed_at", name="expiry_after_observation"),
    CheckConstraint("max_context_tokens > 0", name="max_context_tokens_positive"),
    CheckConstraint(
        "max_output_tokens_limit IS NULL OR max_output_tokens_limit > 0",
        name="max_output_tokens_limit_positive",
    ),
    CheckConstraint(
        "max_images_per_request > 0 AND max_image_bytes_per_request > 0 AND auto_image_tokens > 0",
        name="image_limits_positive",
    ),
    CheckConstraint(
        "chat_token_limit_field IS NULL OR chat_token_limit_field IN "
        "('max_completion_tokens','max_tokens')",
        name="chat_token_limit_field_values",
    ),
)
Index(
    "ix_model_capability_snapshots_lookup",
    model_capability_snapshots.c.endpoint_id,
    model_capability_snapshots.c.protocol,
    model_capability_snapshots.c.model_name,
    model_capability_snapshots.c.observed_at.desc(),
)

model_config_versions = Table(
    "model_config_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("profile_id", UUID_TYPE, nullable=False),
    Column("profile_kind", Text, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("source_draft_id", UUID_TYPE),
    Column("endpoint_id", UUID_TYPE, ForeignKey("model_endpoints.id"), nullable=False),
    Column("credential_id", UUID_TYPE, nullable=False),
    Column(
        "capability_snapshot_id",
        UUID_TYPE,
        ForeignKey("model_capability_snapshots.id"),
        nullable=False,
    ),
    Column("protocol", Text, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("temperature", Numeric(5, 4)),
    Column("max_output_tokens", Integer),
    Column("timeout_seconds", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column(
        "protocol_options_schema_version", SmallInteger, nullable=False, server_default=text("1")
    ),
    Column("protocol_options", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("config_sha256", LargeBinary, nullable=False),
    Column("created_by_admin_id", BigInteger, nullable=False),
    Column("validated_at", UTC_TIMESTAMP, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["profile_id", "profile_kind"],
        ["model_profiles.id", "model_profiles.profile_kind"],
        name="fk_model_config_versions_profile_kind",
    ),
    ForeignKeyConstraint(
        ["credential_id", "profile_id"],
        ["model_credentials.id", "model_credentials.profile_id"],
        name="fk_model_config_versions_credential_scope",
    ),
    CheckConstraint("version_no >= 1", name="version_no_positive"),
    CheckConstraint(
        "protocol IN ('openai_responses','openai_chat_completions',"
        "'anthropic_messages','embedding')",
        name="protocol_values",
    ),
    CheckConstraint("timeout_seconds BETWEEN 1 AND 600", name="timeout_range"),
    CheckConstraint(
        "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
        name="temperature_range",
    ),
    CheckConstraint(
        "(profile_kind = 'generation' AND protocol <> 'embedding' AND "
        "max_output_tokens IS NOT NULL AND max_output_tokens > 0) OR "
        "(profile_kind = 'embedding' AND protocol = 'embedding' AND "
        "temperature IS NULL AND max_output_tokens IS NULL)",
        name="kind_matches_generation_fields",
    ),
    CheckConstraint("octet_length(config_sha256) = 32", name="config_hash_32_bytes"),
    UniqueConstraint("profile_id", "version_no", name="uq_model_config_versions_profile_no"),
    UniqueConstraint(
        "profile_id", "version_no", "id", name="uq_model_config_versions_profile_no_id"
    ),
)

model_profiles.append_constraint(
    ForeignKeyConstraint(
        [model_profiles.c.id, model_profiles.c.active_config_version_no],
        [model_config_versions.c.profile_id, model_config_versions.c.version_no],
        name="fk_model_profiles_active_config",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

model_config_drafts = Table(
    "model_config_drafts",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("profile_id", UUID_TYPE, ForeignKey("model_profiles.id"), nullable=False),
    Column("created_by_admin_id", BigInteger, nullable=False),
    Column("expected_profile_version", BigInteger, nullable=False),
    Column("draft_version", BigInteger, nullable=False, server_default=text("1")),
    Column("state", Text, nullable=False, server_default=text("'editing'")),
    Column("endpoint_id", UUID_TYPE, ForeignKey("model_endpoints.id")),
    Column("credential_id", UUID_TYPE),
    Column("protocol", Text),
    Column("model_name", Text),
    Column("temperature", Numeric(5, 4)),
    Column("max_output_tokens", Integer),
    Column("timeout_seconds", Integer),
    Column("enabled", Boolean),
    Column(
        "protocol_options_schema_version", SmallInteger, nullable=False, server_default=text("1")
    ),
    Column("protocol_options", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("capability_snapshot_id", UUID_TYPE, ForeignKey("model_capability_snapshots.id")),
    Column("validation_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("consumed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["credential_id", "profile_id"],
        ["model_credentials.id", "model_credentials.profile_id"],
        name="fk_model_config_drafts_credential_scope",
    ),
    CheckConstraint("expected_profile_version >= 1", name="expected_version_positive"),
    CheckConstraint("draft_version >= 1", name="draft_version_positive"),
    CheckConstraint(
        "protocol IS NULL OR protocol IN ('openai_responses','openai_chat_completions',"
        "'anthropic_messages','embedding')",
        name="protocol_values",
    ),
    CheckConstraint(
        "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
        name="temperature_range",
    ),
    CheckConstraint(
        "max_output_tokens IS NULL OR max_output_tokens > 0",
        name="max_output_tokens_positive",
    ),
    CheckConstraint(
        "timeout_seconds IS NULL OR timeout_seconds BETWEEN 1 AND 600",
        name="timeout_range",
    ),
    CheckConstraint(
        "state IN ('editing','validated','activated','expired','cancelled','conflict')",
        name="state_values",
    ),
    CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)
Index(
    "uq_model_config_drafts_open_profile_admin",
    model_config_drafts.c.profile_id,
    model_config_drafts.c.created_by_admin_id,
    unique=True,
    postgresql_where=model_config_drafts.c.state.in_(("editing", "validated")),
)
model_config_versions.append_constraint(
    ForeignKeyConstraint(
        [model_config_versions.c.source_draft_id],
        [model_config_drafts.c.id],
        name="fk_model_config_versions_source_draft",
        use_alter=True,
    )
)

prompt_versions = Table(
    "prompt_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("logical_role", Text, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("template_sha256", LargeBinary, nullable=False),
    Column("template_body", Text, nullable=False),
    Column("created_by_admin_id", BigInteger, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint(
        "logical_role IN ('main_ai','memory_agent','proactive_agent')",
        name="logical_role_values",
    ),
    CheckConstraint("version_no >= 1", name="version_no_positive"),
    CheckConstraint("octet_length(template_sha256) = 32", name="template_hash_32_bytes"),
    UniqueConstraint("logical_role", "version_no", name="uq_prompt_versions_role_no"),
)

control_input_sessions = Table(
    "control_input_sessions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("admin_telegram_user_id", BigInteger, nullable=False),
    Column("profile_id", UUID_TYPE, ForeignKey("model_profiles.id"), nullable=False),
    Column("draft_id", UUID_TYPE, ForeignKey("model_config_drafts.id"), nullable=False),
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("expected_draft_version", BigInteger, nullable=False),
    Column("pending_field", Text),
    Column("session_nonce_hash", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("consumed_at", UTC_TIMESTAMP),
    CheckConstraint("state IN ('active','consumed','expired','cancelled')", name="state_values"),
    CheckConstraint("expected_draft_version >= 1", name="draft_version_positive"),
    CheckConstraint("octet_length(session_nonce_hash) = 32", name="nonce_hash_32_bytes"),
    CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)
Index(
    "uq_control_input_sessions_active_admin",
    control_input_sessions.c.admin_telegram_user_id,
    unique=True,
    postgresql_where=control_input_sessions.c.state == "active",
)

model_key_launch_sessions = Table(
    "model_key_launch_sessions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("admin_telegram_user_id", BigInteger, nullable=False),
    Column("profile_id", UUID_TYPE, ForeignKey("model_profiles.id"), nullable=False),
    Column("allowed_action", Text, nullable=False),
    Column("deployment_version", BigInteger, nullable=False),
    Column("expected_credential_version", BigInteger, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("consumed_at", UTC_TIMESTAMP),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("octet_length(token_hash) = 32", name="token_hash_32_bytes"),
    CheckConstraint("allowed_action IN ('set','replace','delete')", name="action_values"),
    CheckConstraint("deployment_version >= 1", name="deployment_version_positive"),
    CheckConstraint("expected_credential_version >= 1", name="credential_version_positive"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)

model_key_rate_limits = Table(
    "model_key_rate_limits",
    metadata,
    Column("principal_hash", LargeBinary, primary_key=True),
    Column("window_started_at", UTC_TIMESTAMP, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("blocked_until", UTC_TIMESTAMP),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    CheckConstraint("octet_length(principal_hash) = 32", name="principal_hash_32_bytes"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    CheckConstraint("version >= 1", name="version_positive"),
)

M2_TABLES = tuple(name for name in metadata.tables if name not in M1_TABLES)

# M3 owns outbound identity and Telegram lifecycle recovery. ``model_run_id`` is
# intentionally stored now, while its FK is added by M4 when ``model_runs`` exists.
outbound_delivery_groups = Table(
    "outbound_delivery_groups",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("turn_id", UUID_TYPE),
    Column("model_run_id", UUID_TYPE),
    Column("model_role", Text),
    Column("copilot_draft_id", UUID_TYPE),
    Column("approved_draft_revision_id", UUID_TYPE),
    Column("proactive_decision_id", UUID_TYPE),
    Column("source", Text, nullable=False),
    Column("generation_no", Integer, nullable=False, server_default=text("1")),
    Column("state", Text, nullable=False, server_default=text("'planned'")),
    Column("intent_count", Integer, nullable=False),
    Column("sent_count", Integer, nullable=False, server_default=text("0")),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("mode_version", BigInteger),
    Column("content_revision", BigInteger),
    Column("account_control_version", BigInteger, nullable=False, server_default=text("1")),
    Column("logical_content_sha256", LargeBinary),
    Column("normalizer_version", Text, nullable=False, server_default=text("'normalized-text-v1'")),
    Column("splitter_version", Text, nullable=False, server_default=text("'telegram-text-v1'")),
    Column("max_delivery_chunks", Integer, nullable=False, server_default=text("16")),
    Column("send_authorized_at", UTC_TIMESTAMP),
    Column("first_side_effect_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_outbound_delivery_groups_conversation_scope",
    ),
    CheckConstraint(
        "source IN ('ai','proactive_ai','copilot_approved','system')",
        name="source_values",
    ),
    CheckConstraint(
        "(source <> 'proactive_ai' OR proactive_decision_id IS NOT NULL) AND "
        "(proactive_decision_id IS NULL OR source IN ('proactive_ai','copilot_approved'))",
        name="proactive_source",
    ),
    CheckConstraint(
        "state IN ('planned','sending','partial','sent','failed','unknown','cancelled')",
        name="state_values",
    ),
    CheckConstraint("intent_count > 0", name="intent_count_positive"),
    CheckConstraint("generation_no > 0", name="generation_no_positive"),
    CheckConstraint("account_control_version > 0", name="control_version_positive"),
    CheckConstraint("max_delivery_chunks > 0", name="max_delivery_chunks_positive"),
    CheckConstraint(
        "logical_content_sha256 IS NULL OR octet_length(logical_content_sha256) = 32",
        name="logical_content_hash_32_bytes",
    ),
    CheckConstraint("sent_count >= 0 AND sent_count <= intent_count", name="sent_count_range"),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_32_bytes"),
    CheckConstraint("mode_version IS NULL OR mode_version >= 1", name="mode_version_positive"),
    CheckConstraint(
        "content_revision IS NULL OR content_revision >= 0", name="content_revision_nonnegative"
    ),
    UniqueConstraint(
        "account_id", "idempotency_key", name="uq_outbound_delivery_groups_idempotency"
    ),
    UniqueConstraint("id", "account_id", name="uq_outbound_delivery_groups_id_account"),
    UniqueConstraint(
        "id",
        "account_id",
        "conversation_id",
        name="uq_outbound_delivery_groups_full_scope",
    ),
)
Index(
    "ix_outbound_delivery_groups_recovery",
    outbound_delivery_groups.c.state,
    outbound_delivery_groups.c.updated_at,
    postgresql_where=outbound_delivery_groups.c.state.in_(("sending", "partial", "unknown")),
)
Index(
    "uq_outbound_groups_proactive_decision",
    outbound_delivery_groups.c.proactive_decision_id,
    unique=True,
    postgresql_where=outbound_delivery_groups.c.proactive_decision_id.is_not(None),
)

outbound_intents = Table(
    "outbound_intents",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("delivery_group_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("turn_id", UUID_TYPE),
    Column("model_run_id", UUID_TYPE),
    Column("model_role", Text),
    Column("source", Text),
    Column("generation_no", Integer, nullable=False, server_default=text("1")),
    Column("account_control_version", BigInteger, nullable=False, server_default=text("1")),
    Column("mode_version", BigInteger),
    Column("content_revision", BigInteger),
    Column("idempotency_key", LargeBinary),
    Column("sequence_no", Integer, nullable=False),
    Column("chunk_count", Integer, nullable=False, server_default=text("1")),
    Column("telegram_random_id", BigInteger, nullable=False),
    Column("payload_kind", Text, nullable=False, server_default=text("'text'")),
    Column("text_content", Text, nullable=False),
    Column("payload_sha256", LargeBinary, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("telegram_message_id", BigInteger),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("next_attempt_at", UTC_TIMESTAMP),
    Column("unknown_since", UTC_TIMESTAMP),
    Column("last_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("sent_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["delivery_group_id", "account_id", "conversation_id"],
        [
            "outbound_delivery_groups.id",
            "outbound_delivery_groups.account_id",
            "outbound_delivery_groups.conversation_id",
        ],
        name="fk_outbound_intents_delivery_group_scope",
    ),
    CheckConstraint("sequence_no >= 0", name="sequence_nonnegative"),
    CheckConstraint("generation_no > 0", name="generation_no_positive"),
    CheckConstraint("account_control_version > 0", name="control_version_positive"),
    CheckConstraint("chunk_count > 0 AND sequence_no < chunk_count", name="chunk_position_valid"),
    CheckConstraint(
        "idempotency_key IS NULL OR octet_length(idempotency_key) = 32",
        name="idempotency_32_bytes",
    ),
    CheckConstraint("telegram_random_id > 0", name="random_id_positive"),
    CheckConstraint("payload_kind = 'text'", name="payload_kind_v1"),
    CheckConstraint("octet_length(payload_sha256) = 32", name="payload_hash_32_bytes"),
    CheckConstraint(
        "state IN ('pending','sending','sent','retry_wait','failed','unknown','cancelled')",
        name="state_values",
    ),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    CheckConstraint(
        "(state = 'sent' AND telegram_message_id IS NOT NULL AND sent_at IS NOT NULL) OR "
        "(state <> 'sent')",
        name="sent_has_receipt",
    ),
    CheckConstraint(
        "(state = 'unknown' AND unknown_since IS NOT NULL) OR state <> 'unknown'",
        name="unknown_has_timestamp",
    ),
    UniqueConstraint("delivery_group_id", "sequence_no", name="uq_outbound_intents_group_no"),
    UniqueConstraint("account_id", "telegram_random_id", name="uq_outbound_intents_random_id"),
    UniqueConstraint("id", "account_id", name="uq_outbound_intents_id_account"),
)
Index(
    "ix_outbound_intents_dispatch",
    outbound_intents.c.state,
    outbound_intents.c.next_attempt_at,
)
Index(
    "ix_outbound_intents_message_reconcile",
    outbound_intents.c.account_id,
    outbound_intents.c.conversation_id,
    outbound_intents.c.telegram_message_id,
    postgresql_where=outbound_intents.c.telegram_message_id.is_not(None),
)

outbound_attempts = Table(
    "outbound_attempts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("intent_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'started'")),
    Column("error_code", Text),
    Column("retry_after_seconds", Integer),
    Column("started_at", UTC_TIMESTAMP, nullable=False),
    Column("finished_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["intent_id", "account_id"],
        ["outbound_intents.id", "outbound_intents.account_id"],
        name="fk_outbound_attempts_intent_scope",
    ),
    CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
    CheckConstraint(
        "state IN ('started','succeeded','flood_wait','transient','permanent','unknown')",
        name="state_values",
    ),
    CheckConstraint(
        "retry_after_seconds IS NULL OR retry_after_seconds >= 0",
        name="retry_after_nonnegative",
    ),
    CheckConstraint(
        "(state = 'started' AND finished_at IS NULL AND error_code IS NULL AND "
        "retry_after_seconds IS NULL) OR "
        "(state <> 'started' AND finished_at IS NOT NULL AND finished_at >= started_at)",
        name="completion_matches_state",
    ),
    UniqueConstraint("intent_id", "attempt_no", name="uq_outbound_attempts_intent_no"),
)

telegram_read_states = Table(
    "telegram_read_states",
    metadata,
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("max_telegram_message_id", BigInteger, nullable=False),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_telegram_read_states_conversation_scope",
    ),
    PrimaryKeyConstraint("account_id", "conversation_id", name="pk_telegram_read_states"),
    CheckConstraint("max_telegram_message_id > 0", name="max_message_id_positive"),
)

telegram_typing_states = Table(
    "telegram_typing_states",
    metadata,
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("false")),
    Column("lease_token", UUID_TYPE),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_telegram_typing_states_conversation_scope",
    ),
    PrimaryKeyConstraint("account_id", "conversation_id", name="pk_telegram_typing_states"),
    CheckConstraint(
        "(active AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(NOT active AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="active_has_lease",
    ),
    CheckConstraint("version >= 1", name="version_positive"),
)

telegram_operations = Table(
    "telegram_operations",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("operation_kind", Text, nullable=False),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("max_telegram_message_id", BigInteger),
    Column("state", Text, nullable=False),
    Column("error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_telegram_operations_conversation_scope",
    ),
    CheckConstraint(
        "operation_kind IN ('read','typing_start','typing_refresh','typing_stop')",
        name="operation_kind_values",
    ),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_32_bytes"),
    CheckConstraint("state IN ('pending','succeeded','failed')", name="state_values"),
    CheckConstraint(
        "(operation_kind = 'read' AND max_telegram_message_id IS NOT NULL) OR "
        "(operation_kind <> 'read' AND max_telegram_message_id IS NULL)",
        name="read_has_message_id",
    ),
    UniqueConstraint("account_id", "idempotency_key", name="uq_telegram_operations_idempotency"),
)

M3_TABLES = tuple(
    name for name in metadata.tables if name not in M1_TABLES and name not in M2_TABLES
)


# M4 owns orchestration runs, immutable turn membership, grace authorization,
# reactive COPILOT drafts, and durable control command identity.
turn_messages = Table(
    "turn_messages",
    metadata,
    Column("turn_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("message_id", UUID_TYPE, nullable=False),
    Column("message_revision_no", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("inclusion_kind", Text, nullable=False, server_default=text("'incoming'")),
    Column("source_event_id", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["turn_id", "account_id", "conversation_id"],
        [
            "conversation_turns.id",
            "conversation_turns.account_id",
            "conversation_turns.conversation_id",
        ],
        name="fk_turn_messages_turn_scope",
    ),
    ForeignKeyConstraint(
        ["message_id", "account_id", "conversation_id"],
        ["messages.id", "messages.account_id", "messages.conversation_id"],
        name="fk_turn_messages_message_scope",
    ),
    ForeignKeyConstraint(
        ["message_id", "account_id", "message_revision_no"],
        [
            "message_revisions.message_id",
            "message_revisions.account_id",
            "message_revisions.revision_no",
        ],
        name="fk_turn_messages_revision_scope",
    ),
    ForeignKeyConstraint(
        ["source_event_id", "account_id", "conversation_id"],
        ["message_events.id", "message_events.account_id", "message_events.conversation_id"],
        name="fk_turn_messages_source_event_scope",
    ),
    PrimaryKeyConstraint("turn_id", "message_id", name="pk_turn_messages"),
    CheckConstraint("message_revision_no > 0", name="message_revision_positive"),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint("inclusion_kind IN ('incoming','album_member')", name="inclusion_kind_values"),
    UniqueConstraint("turn_id", "ordinal", name="uq_turn_messages_ordinal"),
)

model_runs = Table(
    "model_runs",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE),
    Column("turn_id", UUID_TYPE),
    Column("logical_role", Text, nullable=False),
    Column("model_profile_id", UUID_TYPE, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("generation_no", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("account_control_version_snapshot", BigInteger),
    Column("mode_version_snapshot", BigInteger),
    Column("content_revision_snapshot", BigInteger),
    Column("config_version_id", UUID_TYPE, nullable=False),
    Column("credential_version_id", UUID_TYPE, nullable=False),
    Column("context_manifest_id", UUID_TYPE),
    Column("prompt_version", Text, nullable=False),
    Column("prompt_bundle_sha256", LargeBinary, nullable=False),
    Column("capability_snapshot_sha256", LargeBinary, nullable=False),
    Column("input_fingerprint", LargeBinary, nullable=False),
    Column("output_fingerprint", LargeBinary),
    Column("adapter_version", Text, nullable=False),
    Column("request_schema_version", SmallInteger, nullable=False),
    Column("output_schema_version", SmallInteger, nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("finish_reason", Text),
    Column("result_kind", Text),
    Column("is_complete", Boolean),
    Column("provider_request_id", Text),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("started_at", UTC_TIMESTAMP),
    Column("completed_at", UTC_TIMESTAMP),
    Column("cancel_requested_at", UTC_TIMESTAMP),
    Column("error_code", Text),
    Column("error_detail_redacted", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_model_runs_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_model_runs_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["turn_id", "account_id", "conversation_id"],
        [
            "conversation_turns.id",
            "conversation_turns.account_id",
            "conversation_turns.conversation_id",
        ],
        name="fk_model_runs_turn_scope",
    ),
    ForeignKeyConstraint(
        ["model_profile_id", "logical_role"],
        ["model_profiles.id", "model_profiles.logical_role"],
        name="fk_model_runs_profile_role",
    ),
    ForeignKeyConstraint(
        ["config_version_id"], ["model_config_versions.id"], name="fk_model_runs_config_version"
    ),
    ForeignKeyConstraint(
        ["credential_version_id"],
        ["model_credential_versions.id"],
        name="fk_model_runs_credential_version",
    ),
    CheckConstraint(
        "logical_role IN ('main_ai','memory_agent','proactive_agent','embedding')",
        name="logical_role_values",
    ),
    CheckConstraint(
        "state IN ('created','running','output_ready','succeeded','retry_wait','superseded',"
        "'cancelled','failed')",
        name="state_values",
    ),
    CheckConstraint("generation_no > 0", name="generation_positive"),
    CheckConstraint(
        "octet_length(prompt_bundle_sha256) = 32 AND "
        "octet_length(capability_snapshot_sha256) = 32 AND "
        "octet_length(input_fingerprint) = 32 AND "
        "(output_fingerprint IS NULL OR octet_length(output_fingerprint) = 32)",
        name="fingerprints_32_bytes",
    ),
    CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"),
    CheckConstraint(
        "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
    ),
    UniqueConstraint("id", "account_id", name="uq_model_runs_id_account"),
    UniqueConstraint(
        "id", "account_id", "conversation_id", name="uq_model_runs_conversation_scope"
    ),
    UniqueConstraint(
        "id",
        "account_id",
        "conversation_id",
        "turn_id",
        "logical_role",
        name="uq_model_runs_turn_role_scope",
    ),
    UniqueConstraint("id", "account_id", "logical_role", name="uq_model_runs_id_account_role"),
)
Index(
    "uq_model_runs_turn_generation",
    model_runs.c.turn_id,
    model_runs.c.logical_role,
    model_runs.c.generation_no,
    unique=True,
    postgresql_where=model_runs.c.turn_id.is_not(None),
)

model_run_attempts = Table(
    "model_run_attempts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("model_run_id", UUID_TYPE, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("provider_request_id", Text),
    Column("started_at", UTC_TIMESTAMP, nullable=False),
    Column("completed_at", UTC_TIMESTAMP),
    Column("http_status", Integer),
    Column("error_code", Text),
    Column("retry_after_seconds", Integer),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], name="fk_model_run_attempts_run"),
    CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
    CheckConstraint(
        "state IN ('started','succeeded','retryable_failed','terminal_failed',"
        "'cancelled','unknown')",
        name="state_values",
    ),
    CheckConstraint(
        "retry_after_seconds IS NULL OR retry_after_seconds >= 0", name="retry_nonnegative"
    ),
    UniqueConstraint("model_run_id", "attempt_no", name="uq_model_run_attempts_run_no"),
)

turn_grace_authorizations = Table(
    "turn_grace_authorizations",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("turn_id", UUID_TYPE, nullable=False),
    Column("model_run_id", UUID_TYPE, nullable=False, unique=True),
    Column("model_role", Text, nullable=False, server_default=text("'main_ai'")),
    Column("run_started_at", UTC_TIMESTAMP, nullable=False),
    Column("grace_deadline_at", UTC_TIMESTAMP, nullable=False),
    Column("model_completed_at", UTC_TIMESTAMP, nullable=False),
    Column("authorized_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["model_run_id", "account_id", "conversation_id", "turn_id", "model_role"],
        [
            "model_runs.id",
            "model_runs.account_id",
            "model_runs.conversation_id",
            "model_runs.turn_id",
            "model_runs.logical_role",
        ],
        name="fk_turn_grace_authorizations_run_scope",
    ),
    CheckConstraint("model_role = 'main_ai'", name="model_role_main_ai"),
    CheckConstraint("model_completed_at <= grace_deadline_at", name="completed_within_grace"),
)

turn_grace_events = Table(
    "turn_grace_events",
    metadata,
    Column("authorization_id", UUID_TYPE, nullable=False),
    Column("message_event_id", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["authorization_id"],
        ["turn_grace_authorizations.id"],
        name="fk_turn_grace_events_authorization",
    ),
    ForeignKeyConstraint(
        ["message_event_id"], ["message_events.id"], name="fk_turn_grace_events_message_event"
    ),
    PrimaryKeyConstraint("authorization_id", "message_event_id", name="pk_turn_grace_events"),
)

operational_blocks = Table(
    "operational_blocks",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE),
    Column("model_profile_id", UUID_TYPE),
    Column("reason_code", Text, nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("retry_after", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("cleared_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_operational_blocks_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_operational_blocks_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["model_profile_id"], ["model_profiles.id"], name="fk_operational_blocks_profile"
    ),
    CheckConstraint("version > 0", name="version_positive"),
    CheckConstraint("active = (cleared_at IS NULL)", name="active_matches_cleared"),
)
Index(
    "ix_operational_blocks_active",
    operational_blocks.c.account_id,
    operational_blocks.c.conversation_id,
    unique=False,
    postgresql_where=operational_blocks.c.active.is_(True),
)

copilot_drafts = Table(
    "copilot_drafts",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("turn_id", UUID_TYPE, nullable=False),
    Column("model_run_id", UUID_TYPE),
    Column("model_role", Text),
    Column("proactive_decision_id", UUID_TYPE),
    Column("draft_kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("current_revision_no", Integer),
    Column("account_control_version_snapshot", BigInteger, nullable=False),
    Column("mode_version_snapshot", BigInteger, nullable=False),
    Column("content_revision_snapshot", BigInteger, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("approved_by", Text),
    Column("requested_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("ready_at", UTC_TIMESTAMP),
    Column("approved_at", UTC_TIMESTAMP),
    Column("expires_at", UTC_TIMESTAMP),
    Column("terminal_at", UTC_TIMESTAMP),
    Column("terminal_reason", Text),
    ForeignKeyConstraint(
        ["conversation_id", "account_id", "contact_id"],
        ["conversations.id", "conversations.account_id", "conversations.contact_id"],
        name="fk_copilot_drafts_conversation_contact_scope",
    ),
    ForeignKeyConstraint(
        ["turn_id", "account_id", "conversation_id"],
        [
            "conversation_turns.id",
            "conversation_turns.account_id",
            "conversation_turns.conversation_id",
        ],
        name="fk_copilot_drafts_turn_scope",
    ),
    ForeignKeyConstraint(
        ["model_run_id", "account_id", "conversation_id", "turn_id", "model_role"],
        [
            "model_runs.id",
            "model_runs.account_id",
            "model_runs.conversation_id",
            "model_runs.turn_id",
            "model_runs.logical_role",
        ],
        name="fk_copilot_drafts_model_run_scope",
    ),
    CheckConstraint(
        "(draft_kind = 'reactive' AND proactive_decision_id IS NULL) OR "
        "(draft_kind = 'proactive' AND proactive_decision_id IS NOT NULL)",
        name="draft_kind_provenance",
    ),
    CheckConstraint(
        "state IN ('requested','collecting','generating','ready','editing','approved',"
        "'send_queued','send_unknown','sent','ignored','expired','invalidated','failed')",
        name="state_values",
    ),
    CheckConstraint(
        "(model_run_id IS NULL AND model_role IS NULL) OR "
        "(model_run_id IS NOT NULL AND model_role = 'main_ai')",
        name="model_run_role_match",
    ),
    CheckConstraint(
        "current_revision_no IS NULL OR current_revision_no > 0", name="revision_positive"
    ),
    UniqueConstraint("id", "account_id", "conversation_id", name="uq_copilot_drafts_scope"),
)
Index(
    "uq_copilot_drafts_active_conversation",
    copilot_drafts.c.conversation_id,
    unique=True,
    postgresql_where=copilot_drafts.c.state.in_(
        (
            "requested",
            "collecting",
            "generating",
            "ready",
            "editing",
            "approved",
            "send_queued",
            "send_unknown",
        )
    ),
)
Index(
    "uq_copilot_drafts_proactive_decision",
    copilot_drafts.c.proactive_decision_id,
    unique=True,
    postgresql_where=copilot_drafts.c.proactive_decision_id.is_not(None),
)
Index(
    "ix_copilot_drafts_expiry",
    copilot_drafts.c.expires_at,
    postgresql_where=copilot_drafts.c.state.in_(("ready", "editing")),
)

copilot_draft_revisions = Table(
    "copilot_draft_revisions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("draft_id", UUID_TYPE, nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("author_type", Text, nullable=False),
    Column("content_text", Text),
    Column("content_sha256", LargeBinary),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("redacted_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["draft_id", "account_id", "conversation_id"],
        ["copilot_drafts.id", "copilot_drafts.account_id", "copilot_drafts.conversation_id"],
        name="fk_copilot_draft_revisions_draft_scope",
    ),
    CheckConstraint("revision_no > 0", name="revision_no_positive"),
    CheckConstraint("author_type IN ('model','admin_edit')", name="author_type_values"),
    CheckConstraint(
        "(redacted_at IS NULL AND content_text IS NOT NULL AND "
        "octet_length(content_sha256) = 32) OR "
        "(redacted_at IS NOT NULL AND content_text IS NULL AND content_sha256 IS NULL)",
        name="redaction_matches_content",
    ),
    UniqueConstraint("draft_id", "revision_no", name="uq_copilot_draft_revisions_no"),
    UniqueConstraint(
        "draft_id",
        "account_id",
        "revision_no",
        name="uq_copilot_draft_revisions_account_no",
    ),
    UniqueConstraint("id", "account_id", "draft_id", name="uq_copilot_draft_revisions_scope"),
)

copilot_drafts.append_constraint(
    ForeignKeyConstraint(
        [copilot_drafts.c.id, copilot_drafts.c.account_id, copilot_drafts.c.current_revision_no],
        [
            copilot_draft_revisions.c.draft_id,
            copilot_draft_revisions.c.account_id,
            copilot_draft_revisions.c.revision_no,
        ],
        name="fk_copilot_drafts_current_revision",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

copilot_action_tokens = Table(
    "copilot_action_tokens",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("token_sha256", LargeBinary, nullable=False, unique=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("draft_id", UUID_TYPE, nullable=False),
    Column("draft_revision_no", Integer, nullable=False),
    Column("admin_telegram_user_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("used_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["draft_id", "account_id", "conversation_id"],
        ["copilot_drafts.id", "copilot_drafts.account_id", "copilot_drafts.conversation_id"],
        name="fk_copilot_action_tokens_draft_scope",
    ),
    CheckConstraint("octet_length(token_sha256) = 32", name="token_hash_32_bytes"),
    CheckConstraint("draft_revision_no > 0", name="draft_revision_positive"),
    CheckConstraint("purpose IN ('send','edit','ignore')", name="purpose_values"),
)
Index(
    "ix_copilot_action_tokens_expiry",
    copilot_action_tokens.c.expires_at,
    postgresql_where=copilot_action_tokens.c.used_at.is_(None),
)

copilot_edit_sessions = Table(
    "copilot_edit_sessions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("draft_id", UUID_TYPE, nullable=False),
    Column("admin_telegram_user_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("force_reply_message_id", BigInteger, nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("completed_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["draft_id", "account_id", "conversation_id"],
        ["copilot_drafts.id", "copilot_drafts.account_id", "copilot_drafts.conversation_id"],
        name="fk_copilot_edit_sessions_draft_scope",
    ),
    UniqueConstraint(
        "bot_chat_id",
        "admin_telegram_user_id",
        "force_reply_message_id",
        name="uq_copilot_edit_sessions_reply",
    ),
)

control_commands = Table(
    "control_commands",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE),
    Column("bot_identity", Text, nullable=False),
    Column("telegram_update_id", BigInteger, nullable=False),
    Column("admin_telegram_user_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("command_kind", Text, nullable=False),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("expected_control_version", BigInteger),
    Column("expected_mode_version", BigInteger),
    Column("result_control_version", BigInteger),
    Column("result_mode_version", BigInteger),
    Column("state", Text, nullable=False),
    Column("result_code", Text),
    Column("result_changed", Boolean),
    Column("result_payload", JSONB),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_control_commands_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_control_commands_conversation_scope",
    ),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_key_32_bytes"),
    CheckConstraint("state IN ('pending','applied','rejected')", name="state_values"),
    CheckConstraint(
        "result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'",
        name="result_payload_object",
    ),
    CheckConstraint(
        "(state = 'pending' AND result_code IS NULL AND result_changed IS NULL "
        "AND result_control_version IS NULL AND result_mode_version IS NULL "
        "AND result_payload IS NULL "
        "AND completed_at IS NULL) OR "
        "(state IN ('applied','rejected') AND result_code IS NOT NULL "
        "AND result_changed IS NOT NULL AND completed_at IS NOT NULL)",
        name="terminal_result_complete",
    ),
    UniqueConstraint("bot_identity", "telegram_update_id", name="uq_control_commands_bot_update"),
    UniqueConstraint("account_id", "idempotency_key", name="uq_control_commands_idempotency"),
    UniqueConstraint("id", "account_id", name="uq_control_commands_id_account"),
)

M4_TABLES = tuple(
    name
    for name in metadata.tables
    if name not in M1_TABLES and name not in M2_TABLES and name not in M3_TABLES
)


# M5 owns immutable context policy/version registries, provider-independent
# manifests, and metadata-only Control Bot preview lifecycle records.
context_policies = Table(
    "context_policies",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("logical_role", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("active_version_id", UUID_TYPE),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint(
        "logical_role IN ('main_ai','memory_agent','proactive_agent')",
        name="logical_role_values",
    ),
    CheckConstraint("version > 0", name="version_positive"),
    UniqueConstraint("logical_role", "purpose", name="uq_context_policies_role_purpose"),
    UniqueConstraint("id", "active_version_id", name="uq_context_policies_active_scope"),
)

context_policy_versions = Table(
    "context_policy_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("policy_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("max_input_tokens", Integer, nullable=False),
    Column("safety_reserve_basis_points", Integer, nullable=False),
    Column("minimum_safety_reserve_tokens", Integer, nullable=False),
    Column("current_budget_basis_points", Integer, nullable=False),
    Column("recent_budget_basis_points", Integer, nullable=False),
    Column("profile_budget_basis_points", Integer, nullable=False),
    Column("structured_budget_basis_points", Integer, nullable=False),
    Column("semantic_budget_basis_points", Integer, nullable=False),
    Column("summary_budget_basis_points", Integer, nullable=False),
    Column("structured_limit", Integer, nullable=False),
    Column("semantic_limit", Integer, nullable=False),
    Column("ann_candidate_limit", Integer, nullable=False),
    Column("current_image_limit", Integer, nullable=False),
    Column("fallback_auto_image_tokens", Integer, nullable=False),
    Column("token_estimator_policy", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("activated_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["policy_id"], ["context_policies.id"], name="fk_context_policy_versions_policy"
    ),
    CheckConstraint("version_no > 0", name="version_no_positive"),
    CheckConstraint("status IN ('validated','active','retired')", name="status_values"),
    CheckConstraint(
        "max_input_tokens > 0 AND minimum_safety_reserve_tokens > 0 "
        "AND structured_limit > 0 AND semantic_limit > 0 AND ann_candidate_limit > 0 "
        "AND current_image_limit > 0 AND fallback_auto_image_tokens > 0",
        name="limits_positive",
    ),
    CheckConstraint(
        "safety_reserve_basis_points BETWEEN 0 AND 10000",
        name="safety_reserve_range",
    ),
    CheckConstraint(
        "current_budget_basis_points + recent_budget_basis_points + "
        "profile_budget_basis_points + structured_budget_basis_points + "
        "semantic_budget_basis_points + summary_budget_basis_points = 10000",
        name="budget_sum",
    ),
    UniqueConstraint("policy_id", "version_no", name="uq_context_policy_versions_no"),
    UniqueConstraint("id", "policy_id", name="uq_context_policy_versions_scope"),
)
Index(
    "uq_context_policy_versions_active",
    context_policy_versions.c.policy_id,
    unique=True,
    postgresql_where=context_policy_versions.c.status == "active",
)
context_policies.append_constraint(
    ForeignKeyConstraint(
        [context_policies.c.active_version_id, context_policies.c.id],
        [context_policy_versions.c.id, context_policy_versions.c.policy_id],
        name="fk_context_policies_active_version",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

retrieval_policies = Table(
    "retrieval_policies",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("policy_name", Text, nullable=False, unique=True),
    Column("active_version_id", UUID_TYPE),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint("version > 0", name="version_positive"),
)

retrieval_policy_versions = Table(
    "retrieval_policy_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("policy_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("structured_weights", JSONB, nullable=False),
    Column("semantic_weights", JSONB, nullable=False),
    Column("half_life_schema_version", SmallInteger, nullable=False),
    Column("half_life_policy", JSONB, nullable=False),
    Column("tie_break_version", Text, nullable=False),
    Column("source_default_schema_version", SmallInteger, nullable=False),
    Column("source_defaults", JSONB, nullable=False),
    Column("content_sha256", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("activated_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["policy_id"], ["retrieval_policies.id"], name="fk_retrieval_policy_versions_policy"
    ),
    CheckConstraint("version_no > 0", name="version_no_positive"),
    CheckConstraint("status IN ('validated','active','retired')", name="status_values"),
    CheckConstraint("octet_length(content_sha256) = 32", name="content_hash_32_bytes"),
    UniqueConstraint("policy_id", "version_no", name="uq_retrieval_policy_versions_no"),
    UniqueConstraint("id", "policy_id", name="uq_retrieval_policy_versions_scope"),
)
Index(
    "uq_retrieval_policy_versions_active",
    retrieval_policy_versions.c.policy_id,
    unique=True,
    postgresql_where=retrieval_policy_versions.c.status == "active",
)
retrieval_policies.append_constraint(
    ForeignKeyConstraint(
        [retrieval_policies.c.active_version_id, retrieval_policies.c.id],
        [retrieval_policy_versions.c.id, retrieval_policy_versions.c.policy_id],
        name="fk_retrieval_policies_active_version",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

context_manifests = Table(
    "context_manifests",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE),
    Column("owner_kind", Text, nullable=False),
    Column("turn_id", UUID_TYPE),
    Column("background_job_id", UUID_TYPE),
    Column("purpose", Text, nullable=False),
    Column("logical_role", Text, nullable=False),
    Column("builder_version", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("prompt_bundle_sha256", LargeBinary, nullable=False),
    Column("context_policy_version_id", UUID_TYPE, nullable=False),
    Column("retrieval_policy_version_id", UUID_TYPE, nullable=False),
    Column("retrieval_policy_version", Text, nullable=False),
    Column("token_policy_version", Text, nullable=False),
    Column("token_estimator_version", Text, nullable=False),
    Column("capability_snapshot_sha256", LargeBinary, nullable=False),
    # The account-scoped FK is added by 0011 after M6 embedding spaces exist.
    Column("embedding_space_id", UUID_TYPE),
    Column("memory_freshness", Text, nullable=False),
    Column("effective_input_budget", Integer, nullable=False),
    Column("safety_reserve_tokens", Integer, nullable=False),
    Column("estimated_instruction_tokens", Integer, nullable=False),
    Column("estimated_text_tokens", Integer, nullable=False),
    Column("estimated_image_tokens", Integer, nullable=False),
    Column("estimated_structural_tokens", Integer, nullable=False),
    Column("input_token_estimate", Integer, nullable=False),
    Column("image_count", Integer, nullable=False),
    Column("omission_count", Integer, nullable=False),
    Column("source_revision_vector_sha256", LargeBinary, nullable=False),
    Column("manifest_sha256", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_context_manifests_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_context_manifests_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["turn_id", "account_id", "conversation_id"],
        [
            "conversation_turns.id",
            "conversation_turns.account_id",
            "conversation_turns.conversation_id",
        ],
        name="fk_context_manifests_turn_scope",
    ),
    ForeignKeyConstraint(
        ["background_job_id", "account_id"],
        ["background_jobs.id", "background_jobs.account_id"],
        name="fk_context_manifests_job_scope",
    ),
    ForeignKeyConstraint(
        ["context_policy_version_id"],
        ["context_policy_versions.id"],
        name="fk_context_manifests_context_policy",
    ),
    ForeignKeyConstraint(
        ["retrieval_policy_version_id"],
        ["retrieval_policy_versions.id"],
        name="fk_context_manifests_retrieval_policy",
    ),
    CheckConstraint("owner_kind IN ('turn','background_job')", name="owner_kind_values"),
    CheckConstraint(
        "(owner_kind = 'turn' AND turn_id IS NOT NULL AND background_job_id IS NULL "
        "AND conversation_id IS NOT NULL) OR "
        "(owner_kind = 'background_job' AND turn_id IS NULL AND background_job_id IS NOT NULL)",
        name="owner_fields_match",
    ),
    CheckConstraint(
        "logical_role IN ('main_ai','memory_agent','proactive_agent')",
        name="logical_role_values",
    ),
    CheckConstraint("memory_freshness IN ('fresh','degraded','stale')", name="freshness_values"),
    CheckConstraint(
        "effective_input_budget > 0 AND safety_reserve_tokens >= 0 "
        "AND estimated_instruction_tokens >= 0 AND estimated_text_tokens >= 0 "
        "AND estimated_image_tokens >= 0 AND estimated_structural_tokens >= 0 "
        "AND image_count >= 0 AND omission_count >= 0",
        name="budget_values",
    ),
    CheckConstraint(
        "input_token_estimate = estimated_instruction_tokens + estimated_text_tokens + "
        "estimated_image_tokens + estimated_structural_tokens",
        name="token_estimate_sum",
    ),
    CheckConstraint(
        "octet_length(prompt_bundle_sha256) = 32 AND "
        "octet_length(capability_snapshot_sha256) = 32 AND "
        "octet_length(source_revision_vector_sha256) = 32 AND "
        "octet_length(manifest_sha256) = 32",
        name="hashes_32_bytes",
    ),
    UniqueConstraint("id", "account_id", name="uq_context_manifests_id_account"),
)
Index(
    "uq_context_manifests_turn_build",
    context_manifests.c.turn_id,
    context_manifests.c.logical_role,
    context_manifests.c.builder_version,
    context_manifests.c.manifest_sha256,
    unique=True,
    postgresql_where=context_manifests.c.turn_id.is_not(None),
)
Index(
    "uq_context_manifests_job_build",
    context_manifests.c.background_job_id,
    context_manifests.c.logical_role,
    context_manifests.c.builder_version,
    context_manifests.c.manifest_sha256,
    unique=True,
    postgresql_where=context_manifests.c.background_job_id.is_not(None),
)

context_manifest_items = Table(
    "context_manifest_items",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("manifest_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("layer", Text, nullable=False),
    Column("canonical_role", Text, nullable=False),
    Column("source_actor", Text, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("source_id", UUID_TYPE, nullable=False),
    Column("source_revision", Text, nullable=False),
    Column("prompt_version_id", UUID_TYPE, ForeignKey("prompt_versions.id")),
    Column("message_revision_id", UUID_TYPE),
    Column("media_object_id", UUID_TYPE),
    # M6 adds the foreign keys after the derived-source tables exist.
    Column("memory_version_id", UUID_TYPE),
    Column("summary_version_id", UUID_TYPE),
    Column("trust_level", Text, nullable=False),
    Column("rank_position", Integer),
    Column("base_score", Numeric(8, 7)),
    Column("final_score", Numeric(8, 7)),
    Column("score_features_schema_version", SmallInteger),
    Column("score_features", JSONB),
    Column("source_slice_start", Integer),
    Column("source_slice_end", Integer),
    Column("image_detail", Text),
    Column("token_estimate", Integer, nullable=False),
    Column("estimated_image_tokens", Integer, nullable=False),
    Column("content_sha256", LargeBinary, nullable=False),
    Column("rendered_part_sha256", LargeBinary, nullable=False),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint(
        "layer IN ('instruction','identity','personality','relationship_time',"
        "'structured_memory','semantic_memory','summary','recent','current')",
        name="layer_values",
    ),
    CheckConstraint(
        "canonical_role IN ('system','developer','user','assistant')",
        name="canonical_role_values",
    ),
    CheckConstraint(
        "source_type IN ('trusted_instruction','message_revision','media_object',"
        "'memory_version','summary_version')",
        name="source_type_values",
    ),
    CheckConstraint(
        "(source_type = 'trusted_instruction' AND layer = 'instruction') OR "
        "(source_type = 'message_revision' AND layer IN "
        "('relationship_time','recent','current')) OR "
        "(source_type = 'media_object' AND layer = 'current') OR "
        "(source_type = 'memory_version' AND layer IN "
        "('identity','personality','relationship_time','structured_memory','semantic_memory')) OR "
        "(source_type = 'summary_version' AND layer = 'summary')",
        name="source_type_layer_matches",
    ),
    CheckConstraint(
        "(source_type = 'trusted_instruction' AND prompt_version_id IS NOT NULL "
        "AND message_revision_id IS NULL AND media_object_id IS NULL "
        "AND memory_version_id IS NULL AND summary_version_id IS NULL "
        "AND source_id = prompt_version_id) OR "
        "(source_type = 'message_revision' AND prompt_version_id IS NULL "
        "AND message_revision_id IS NOT NULL AND media_object_id IS NULL "
        "AND memory_version_id IS NULL AND summary_version_id IS NULL "
        "AND source_id = message_revision_id) OR "
        "(source_type = 'media_object' AND prompt_version_id IS NULL "
        "AND media_object_id IS NOT NULL AND message_revision_id IS NULL "
        "AND memory_version_id IS NULL AND summary_version_id IS NULL "
        "AND source_id = media_object_id) OR "
        "(source_type = 'memory_version' AND prompt_version_id IS NULL "
        "AND message_revision_id IS NULL AND media_object_id IS NULL "
        "AND memory_version_id IS NOT NULL AND summary_version_id IS NULL "
        "AND source_id = memory_version_id) OR "
        "(source_type = 'summary_version' AND prompt_version_id IS NULL "
        "AND message_revision_id IS NULL AND media_object_id IS NULL "
        "AND memory_version_id IS NULL AND summary_version_id IS NOT NULL "
        "AND source_id = summary_version_id)",
        name="typed_source_matches",
    ),
    CheckConstraint("rank_position IS NULL OR rank_position > 0", name="rank_positive"),
    CheckConstraint("base_score IS NULL OR base_score BETWEEN 0 AND 1", name="base_score_range"),
    CheckConstraint("final_score IS NULL OR final_score BETWEEN 0 AND 1", name="final_score_range"),
    CheckConstraint(
        "(source_slice_start IS NULL AND source_slice_end IS NULL) OR "
        "(source_slice_start >= 0 AND source_slice_end > source_slice_start)",
        name="slice_range",
    ),
    CheckConstraint("image_detail IS NULL OR image_detail = 'auto'", name="image_detail_auto"),
    CheckConstraint(
        "token_estimate >= 0 AND estimated_image_tokens >= 0",
        name="token_estimates_nonnegative",
    ),
    CheckConstraint(
        "octet_length(content_sha256) = 32 AND octet_length(rendered_part_sha256) = 32",
        name="content_hashes_32_bytes",
    ),
    UniqueConstraint("manifest_id", "ordinal", name="uq_context_manifest_items_ordinal"),
    ForeignKeyConstraint(
        ["manifest_id", "account_id"],
        ["context_manifests.id", "context_manifests.account_id"],
        name="fk_context_manifest_items_manifest_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_context_manifest_items_message_revision_scope",
    ),
    ForeignKeyConstraint(
        ["media_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_context_manifest_items_media_scope",
    ),
)

context_manifest_item_reasons = Table(
    "context_manifest_item_reasons",
    metadata,
    Column("manifest_item_id", BigInteger, ForeignKey("context_manifest_items.id"), nullable=False),
    Column("reason_ordinal", SmallInteger, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("related_source_type", Text),
    Column("related_source_id", UUID_TYPE),
    PrimaryKeyConstraint("manifest_item_id", "reason_ordinal", name="pk_context_item_reasons"),
    CheckConstraint("reason_ordinal > 0", name="reason_ordinal_positive"),
)

context_manifest_omissions = Table(
    "context_manifest_omissions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("manifest_id", UUID_TYPE, ForeignKey("context_manifests.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("layer", Text, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("source_type", Text),
    Column("source_id", UUID_TYPE),
    Column("range_start_event_id", BigInteger),
    Column("range_end_event_id", BigInteger),
    Column("omitted_count", Integer),
    Column("estimated_tokens", Integer),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    CheckConstraint(
        "omitted_count IS NULL OR omitted_count >= 0", name="omitted_count_nonnegative"
    ),
    CheckConstraint("estimated_tokens IS NULL OR estimated_tokens >= 0", name="tokens_nonnegative"),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    UniqueConstraint("manifest_id", "ordinal", name="uq_context_manifest_omissions_ordinal"),
)

context_preview_requests = Table(
    "context_preview_requests",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("control_command_id", UUID_TYPE, unique=True),
    Column("bot_identity", Text, nullable=False),
    Column("admin_user_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("context_manifest_id", UUID_TYPE, nullable=False),
    Column("manifest_sha256", LargeBinary, nullable=False),
    Column("source_revision_vector_sha256", LargeBinary, nullable=False),
    Column("state", Text, nullable=False),
    Column("chunk_count", Integer),
    Column("delivered_chunk_count", Integer, nullable=False, server_default=text("0")),
    Column("token_expires_at", UTC_TIMESTAMP, nullable=False),
    Column("confirmed_at", UTC_TIMESTAMP),
    Column("delivered_at", UTC_TIMESTAMP),
    Column("delete_after", UTC_TIMESTAMP),
    Column("completed_at", UTC_TIMESTAMP),
    Column("last_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["account_id"], ["accounts.id"], name="fk_context_preview_requests_account"
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_context_preview_requests_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["context_manifest_id", "account_id"],
        ["context_manifests.id", "context_manifests.account_id"],
        name="fk_context_preview_requests_manifest_scope",
    ),
    ForeignKeyConstraint(
        ["control_command_id", "account_id"],
        ["control_commands.id", "control_commands.account_id"],
        name="fk_context_preview_requests_control_scope",
    ),
    CheckConstraint(
        "state IN ('pending_confirmation','confirmed','delivering','delivered','send_unknown',"
        "'delete_pending','deleted','delete_partial','expired','cancelled','failed')",
        name="state_values",
    ),
    CheckConstraint(
        "delivered_chunk_count >= 0 AND (chunk_count IS NULL OR "
        "(chunk_count >= 0 AND delivered_chunk_count <= chunk_count))",
        name="chunk_counts_valid",
    ),
    CheckConstraint(
        "octet_length(manifest_sha256) = 32 AND octet_length(source_revision_vector_sha256) = 32",
        name="hashes_32_bytes",
    ),
    UniqueConstraint("id", "admin_user_id", "bot_chat_id", name="uq_context_preview_admin_scope"),
    UniqueConstraint("id", "bot_identity", "bot_chat_id", name="uq_context_preview_bot_scope"),
)

context_preview_tokens = Table(
    "context_preview_tokens",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("request_id", UUID_TYPE, nullable=False, unique=True),
    Column("admin_user_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("used_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["request_id", "admin_user_id", "bot_chat_id"],
        [
            "context_preview_requests.id",
            "context_preview_requests.admin_user_id",
            "context_preview_requests.bot_chat_id",
        ],
        name="fk_context_preview_tokens_request_scope",
    ),
    CheckConstraint("purpose = 'context_preview_confirm'", name="purpose_v1"),
    CheckConstraint("octet_length(token_hash) = 32", name="token_hash_32_bytes"),
)

context_preview_deliveries = Table(
    "context_preview_deliveries",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("request_id", UUID_TYPE, nullable=False),
    Column("bot_identity", Text, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("bot_message_id", BigInteger),
    Column("sent_at", UTC_TIMESTAMP),
    Column("delete_after", UTC_TIMESTAMP),
    Column("delete_claimed_at", UTC_TIMESTAMP),
    Column("delete_lease_expires_at", UTC_TIMESTAMP),
    Column("delete_fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("deleted_at", UTC_TIMESTAMP),
    Column("last_error_code", Text),
    ForeignKeyConstraint(
        ["request_id", "bot_identity", "bot_chat_id"],
        [
            "context_preview_requests.id",
            "context_preview_requests.bot_identity",
            "context_preview_requests.bot_chat_id",
        ],
        name="fk_context_preview_deliveries_request_scope",
    ),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint(
        "state IN ('pending','sending','sent','send_unknown','delete_pending',"
        "'deleted','delete_failed')",
        name="state_values",
    ),
    CheckConstraint("delete_fencing_token >= 0", name="delete_fencing_token_nonnegative"),
    CheckConstraint(
        "(state = 'delete_pending' AND delete_claimed_at IS NOT NULL AND "
        "delete_lease_expires_at IS NOT NULL AND delete_fencing_token > 0) OR "
        "(state <> 'delete_pending' AND delete_claimed_at IS NULL AND "
        "delete_lease_expires_at IS NULL)",
        name="delete_lease_state_match",
    ),
    UniqueConstraint("request_id", "ordinal", name="uq_context_preview_deliveries_ordinal"),
)
Index(
    "uq_context_preview_deliveries_bot_message",
    context_preview_deliveries.c.bot_identity,
    context_preview_deliveries.c.bot_chat_id,
    context_preview_deliveries.c.bot_message_id,
    unique=True,
    postgresql_where=context_preview_deliveries.c.bot_message_id.is_not(None),
)

# This cross-milestone foreign key is created explicitly by migration 0006.
# Keeping it attached to the shared MetaData would make the historical 0004
# ``create_all(tables=M4_TABLES)`` try to reference an M5 table before it exists.

M5_TABLES = tuple(
    name
    for name in metadata.tables
    if name not in M1_TABLES
    and name not in M2_TABLES
    and name not in M3_TABLES
    and name not in M4_TABLES
)

# M6 owns the asynchronous memory, summary, review, and embedding state.  These
# definitions intentionally follow the M5 snapshot so historical migrations do
# not attempt to create future tables.
memory_jobs = Table(
    "memory_jobs",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("job_kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("job_version", BigInteger, nullable=False, server_default=text("1")),
    Column("range_start_event_id", BigInteger, nullable=False),
    Column("range_end_event_id", BigInteger, nullable=False),
    Column("eligible_revision_count", Integer, nullable=False, server_default=text("0")),
    Column("estimated_input_tokens", Integer, nullable=False, server_default=text("0")),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("completed_turn_watermark", UUID_TYPE),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("quiet_until", UTC_TIMESTAMP, nullable=False),
    Column("hard_due_at", UTC_TIMESTAMP, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("input_schema_version", SmallInteger, nullable=False),
    Column("output_schema_version", SmallInteger, nullable=False),
    Column("input_manifest_id", UUID_TYPE),
    Column("sealed_at", UTC_TIMESTAMP),
    Column("background_job_id", UUID_TYPE),
    Column("lease_owner", UUID_TYPE),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_jobs_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memory_jobs_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["background_job_id", "account_id"],
        ["background_jobs.id", "background_jobs.account_id"],
        name="fk_memory_jobs_background_job_scope",
    ),
    CheckConstraint(
        "job_kind IN ('episode','rolling_summary','consolidation','reconciliation')",
        name="job_kind_values",
    ),
    CheckConstraint(
        "state IN ('pending','leased','running','succeeded','retry_wait','dead_letter',"
        "'cancelled')",
        name="state_values",
    ),
    CheckConstraint("generation > 0 AND job_version > 0", name="generation_positive"),
    CheckConstraint("range_end_event_id >= range_start_event_id", name="range_values"),
    CheckConstraint(
        "eligible_revision_count >= 0 AND estimated_input_tokens >= 0 AND attempt_count >= 0",
        name="estimates_nonnegative",
    ),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_key_32_bytes"),
    CheckConstraint(
        "input_schema_version > 0 AND output_schema_version > 0",
        name="schema_versions_positive",
    ),
    CheckConstraint(
        "(sealed_at IS NULL AND input_manifest_id IS NULL) OR "
        "(sealed_at IS NOT NULL AND input_manifest_id IS NOT NULL)",
        name="seal_pointer_matches",
    ),
    CheckConstraint(
        "(state IN ('leased','running') AND lease_owner IS NOT NULL AND "
        "lease_expires_at IS NOT NULL) OR "
        "(state NOT IN ('leased','running') AND lease_owner IS NULL AND lease_expires_at IS NULL)",
        name="lease_fields_match",
    ),
    UniqueConstraint("account_id", "idempotency_key", name="uq_memory_jobs_idempotency"),
    UniqueConstraint("conversation_id", "job_kind", "generation", name="uq_memory_jobs_generation"),
    UniqueConstraint("id", "account_id", name="uq_memory_jobs_id_account"),
)
Index(
    "ix_memory_jobs_pending_due",
    memory_jobs.c.conversation_id,
    memory_jobs.c.job_kind,
    memory_jobs.c.state,
    memory_jobs.c.quiet_until,
    memory_jobs.c.hard_due_at,
    postgresql_where=memory_jobs.c.state.in_(("pending", "retry_wait")),
)
Index(
    "uq_memory_jobs_pending",
    memory_jobs.c.conversation_id,
    memory_jobs.c.job_kind,
    unique=True,
    postgresql_where=memory_jobs.c.state == "pending",
)

memory_input_manifests = Table(
    "memory_input_manifests",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("memory_job_id", UUID_TYPE, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("manifest_kind", Text, nullable=False),
    Column("range_start_event_id", BigInteger, nullable=False),
    Column("range_end_event_id", BigInteger, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("input_schema_version", SmallInteger, nullable=False),
    Column("output_schema_version", SmallInteger, nullable=False),
    Column("model_config_version_id", UUID_TYPE),
    Column("credential_version_id", UUID_TYPE),
    Column("timezone_snapshot", Text),
    Column("input_token_estimate", Integer, nullable=False),
    Column("image_count", Integer, nullable=False),
    Column("manifest_sha256", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_input_manifests_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memory_input_manifests_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["memory_job_id", "account_id"],
        ["memory_jobs.id", "memory_jobs.account_id"],
        name="fk_memory_input_manifests_job_scope",
    ),
    ForeignKeyConstraint(
        ["model_config_version_id"],
        ["model_config_versions.id"],
        name="fk_memory_input_manifests_model_config",
    ),
    ForeignKeyConstraint(
        ["credential_version_id"],
        ["model_credential_versions.id"],
        name="fk_memory_input_manifests_credential",
    ),
    CheckConstraint(
        "manifest_kind IN ('episode','rolling_summary','consolidation','reconciliation')",
        name="manifest_kind_values",
    ),
    CheckConstraint(
        "generation > 0 AND range_end_event_id >= range_start_event_id", name="range_values"
    ),
    CheckConstraint(
        "input_schema_version > 0 AND output_schema_version > 0 AND "
        "input_token_estimate >= 0 AND image_count >= 0",
        name="manifest_estimates_valid",
    ),
    CheckConstraint("octet_length(manifest_sha256) = 32", name="manifest_hash_32_bytes"),
    UniqueConstraint("memory_job_id", "generation", name="uq_memory_input_manifests_generation"),
    UniqueConstraint("id", "account_id", name="uq_memory_input_manifests_id_account"),
    UniqueConstraint(
        "account_id",
        "manifest_sha256",
        "pipeline_version",
        "prompt_version",
        "output_schema_version",
        name="uq_memory_input_manifests_identity",
    ),
)
memory_jobs.append_constraint(
    ForeignKeyConstraint(
        ["input_manifest_id", "account_id"],
        ["memory_input_manifests.id", "memory_input_manifests.account_id"],
        name="fk_memory_jobs_input_manifest_scope",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

memory_input_manifest_items = Table(
    "memory_input_manifest_items",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("manifest_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("message_revision_id", UUID_TYPE),
    Column("media_object_id", UUID_TYPE),
    Column("memory_version_id", UUID_TYPE),
    Column("summary_version_id", UUID_TYPE),
    Column("inclusion_role", Text, nullable=False),
    Column("trust_class", Text, nullable=False),
    Column("source_content_sha256", LargeBinary, nullable=False),
    Column("selection_reason_code", Text, nullable=False),
    ForeignKeyConstraint(
        ["manifest_id", "account_id"],
        ["memory_input_manifests.id", "memory_input_manifests.account_id"],
        name="fk_memory_manifest_items_manifest_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_memory_manifest_items_message_revision_scope",
    ),
    ForeignKeyConstraint(
        ["media_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_memory_manifest_items_media_scope",
    ),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint(
        "source_type IN ('message_revision','media_object','memory_version','summary_version')",
        name="source_type_values",
    ),
    CheckConstraint(
        "inclusion_role IN ('episode','supporting','related_current','prior_summary','profile')",
        name="inclusion_role_values",
    ),
    CheckConstraint(
        "((source_type = 'message_revision' AND message_revision_id IS NOT NULL AND "
        "media_object_id IS NULL AND memory_version_id IS NULL AND summary_version_id IS NULL) OR "
        "(source_type = 'media_object' AND media_object_id IS NOT NULL AND "
        "message_revision_id IS NULL AND memory_version_id IS NULL AND "
        "summary_version_id IS NULL) OR "
        "(source_type = 'memory_version' AND memory_version_id IS NOT NULL AND "
        "message_revision_id IS NULL AND media_object_id IS NULL AND "
        "summary_version_id IS NULL) OR "
        "(source_type = 'summary_version' AND summary_version_id IS NOT NULL AND "
        "message_revision_id IS NULL AND media_object_id IS NULL AND memory_version_id IS NULL))",
        name="typed_source_matches",
    ),
    CheckConstraint("octet_length(source_content_sha256) = 32", name="source_hash_32_bytes"),
    UniqueConstraint("manifest_id", "ordinal", name="uq_memory_manifest_items_ordinal"),
)
Index(
    "ix_memory_manifest_items_message_revision", memory_input_manifest_items.c.message_revision_id
)
Index(
    "ix_memory_manifest_items_memory_version",
    memory_input_manifest_items.c.memory_version_id,
    postgresql_where=memory_input_manifest_items.c.memory_version_id.is_not(None),
)

memory_watermarks = Table(
    "memory_watermarks",
    metadata,
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("watermark_kind", Text, nullable=False),
    Column("last_scanned_event_id", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "last_contiguous_decided_event_id", BigInteger, nullable=False, server_default=text("0")
    ),
    Column("last_succeeded_job_id", UUID_TYPE),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint("conversation_id", "watermark_kind", name="pk_memory_watermarks"),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_watermarks_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memory_watermarks_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["last_succeeded_job_id", "account_id"],
        ["memory_jobs.id", "memory_jobs.account_id"],
        name="fk_memory_watermarks_job_scope",
    ),
    CheckConstraint("watermark_kind IN ('episode','reconciliation')", name="kind_values"),
    CheckConstraint(
        "last_scanned_event_id >= 0 AND last_contiguous_decided_event_id >= 0 AND "
        "last_contiguous_decided_event_id <= last_scanned_event_id AND version > 0",
        name="watermark_values",
    ),
)

memories = Table(
    "memories",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE),
    Column("conversation_id", UUID_TYPE),
    Column("memory_type", Text, nullable=False),
    Column("semantic_key_hash", LargeBinary, nullable=False),
    Column("status", Text, nullable=False),
    Column("current_version_no", Integer, nullable=False),
    Column("superseded_by_memory_id", UUID_TYPE),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("forgotten_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memories_account"),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_memories_contact_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memories_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["superseded_by_memory_id", "account_id"],
        ["memories.id", "memories.account_id"],
        name="fk_memories_superseded_by_scope",
    ),
    CheckConstraint(
        "memory_type IN ('identity','relationship','fact','preference','event',"
        "'intention','style')",
        name="memory_type_values",
    ),
    CheckConstraint(
        "status IN ('active','superseded','invalidated','forgotten')", name="status_values"
    ),
    CheckConstraint("current_version_no > 0", name="version_positive"),
    CheckConstraint("octet_length(semantic_key_hash) = 32", name="semantic_hash_32_bytes"),
    CheckConstraint("contact_id IS NOT NULL OR conversation_id IS NULL", name="scope_values"),
    UniqueConstraint("id", "account_id", name="uq_memories_id_account"),
    UniqueConstraint(
        "id", "account_id", "conversation_id", name="uq_memories_id_conversation_scope"
    ),
)
Index(
    "ix_memories_active_semantic_key",
    memories.c.account_id,
    memories.c.contact_id,
    memories.c.memory_type,
    memories.c.semantic_key_hash,
    postgresql_where=memories.c.status == "active",
)

memory_versions = Table(
    "memory_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("memory_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("operation", Text, nullable=False),
    Column("payload_schema_version", SmallInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("rendered_text", Text),
    Column("importance", Numeric(5, 4), nullable=False),
    Column("confidence", Numeric(5, 4), nullable=False),
    Column("observed_at", UTC_TIMESTAMP),
    Column("valid_from", UTC_TIMESTAMP),
    Column("valid_to", UTC_TIMESTAMP),
    Column("time_precision", Text, nullable=False),
    Column("timezone", Text),
    Column("model_run_id", UUID_TYPE),
    Column("model_role", Text),
    Column("prompt_version", Text),
    Column("validator_policy_version", Text, nullable=False),
    Column("acceptance_kind", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("redacted_at", UTC_TIMESTAMP),
    Column("redaction_reason", Text),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_versions_account"),
    ForeignKeyConstraint(
        ["memory_id", "account_id"],
        ["memories.id", "memories.account_id"],
        name="fk_memory_versions_memory_scope",
    ),
    ForeignKeyConstraint(
        ["model_run_id", "account_id", "model_role"],
        ["model_runs.id", "model_runs.account_id", "model_runs.logical_role"],
        name="fk_memory_versions_model_run_scope",
    ),
    CheckConstraint(
        "operation IN ('create','update','merge','supersede','invalidate')", name="operation_values"
    ),
    CheckConstraint("payload_schema_version > 0", name="payload_schema_positive"),
    CheckConstraint(
        "importance BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1", name="scores_range"
    ),
    CheckConstraint(
        "time_precision IN ('exact','day','week','month','relative','unknown')",
        name="time_precision_values",
    ),
    CheckConstraint(
        "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from", name="valid_interval"
    ),
    CheckConstraint(
        "(model_run_id IS NULL AND model_role IS NULL) OR "
        "(model_run_id IS NOT NULL AND model_role = 'memory_agent')",
        name="model_role_values",
    ),
    CheckConstraint(
        "acceptance_kind IN ('automatic','manual','reconciliation','migration')",
        name="acceptance_values",
    ),
    UniqueConstraint("memory_id", "version_no", name="uq_memory_versions_no"),
    UniqueConstraint(
        "memory_id",
        "account_id",
        "version_no",
        name="uq_memory_versions_account_no",
    ),
    UniqueConstraint("id", "account_id", name="uq_memory_versions_id_account"),
)
memories.append_constraint(
    ForeignKeyConstraint(
        ["id", "account_id", "current_version_no"],
        [
            "memory_versions.memory_id",
            "memory_versions.account_id",
            "memory_versions.version_no",
        ],
        name="fk_memories_current_version",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

memory_proposals = Table(
    "memory_proposals",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE),
    Column("conversation_id", UUID_TYPE),
    Column("memory_job_id", UUID_TYPE, nullable=False),
    Column("model_run_id", UUID_TYPE, nullable=False),
    Column("model_role", Text, nullable=False, server_default=text("'memory_agent'")),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("proposal_ordinal", Integer, nullable=False),
    Column("operation", Text, nullable=False),
    Column("memory_type", Text, nullable=False),
    Column("semantic_key_hash", LargeBinary, nullable=False),
    Column("payload_schema_version", SmallInteger, nullable=False),
    Column("proposed_payload", JSONB, nullable=False),
    Column("proposed_text", Text),
    Column("proposed_confidence", Numeric(5, 4), nullable=False),
    Column("proposed_importance", Numeric(5, 4), nullable=False),
    Column("proposed_valid_from", UTC_TIMESTAMP),
    Column("proposed_valid_to", UTC_TIMESTAMP),
    Column("visual_only", Boolean, nullable=False, server_default=text("false")),
    Column("state", Text, nullable=False),
    Column("review_version", Integer, nullable=False, server_default=text("1")),
    Column("validation_code", Text),
    Column("validator_policy_version", Text, nullable=False),
    Column("accepted_memory_version_id", UUID_TYPE),
    Column("decision_actor_type", Text),
    Column("decision_actor_id", Text),
    Column("decision_reason_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("decided_at", UTC_TIMESTAMP),
    Column("retention_class", Text, nullable=False),
    Column("expires_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_proposals_account"),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_memory_proposals_contact_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memory_proposals_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["memory_job_id", "account_id"],
        ["memory_jobs.id", "memory_jobs.account_id"],
        name="fk_memory_proposals_job_scope",
    ),
    ForeignKeyConstraint(
        ["model_run_id", "account_id", "model_role"],
        ["model_runs.id", "model_runs.account_id", "model_runs.logical_role"],
        name="fk_memory_proposals_model_run_scope",
    ),
    ForeignKeyConstraint(
        ["accepted_memory_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_proposals_accepted_version_scope",
    ),
    CheckConstraint("model_role = 'memory_agent'", name="model_role_values"),
    CheckConstraint("proposal_ordinal >= 0", name="proposal_ordinal_nonnegative"),
    CheckConstraint(
        "octet_length(idempotency_key) = 32 AND octet_length(semantic_key_hash) = 32",
        name="proposal_hashes_32_bytes",
    ),
    CheckConstraint(
        "proposed_confidence BETWEEN 0 AND 1 AND proposed_importance BETWEEN 0 AND 1",
        name="scores_range",
    ),
    CheckConstraint(
        "state IN ('received','validating','accepted','rejected','candidate','error',"
        "'invalidated','expired')",
        name="state_values",
    ),
    CheckConstraint("review_version > 0", name="review_version_positive"),
    CheckConstraint(
        "operation IN ('create','update','merge','supersede','invalidate')", name="operation_values"
    ),
    CheckConstraint(
        "memory_type IN "
        "('identity','relationship','fact','preference','event','intention','style')",
        name="memory_type_values",
    ),
    UniqueConstraint("account_id", "idempotency_key", name="uq_memory_proposals_idempotency"),
    UniqueConstraint("model_run_id", "proposal_ordinal", name="uq_memory_proposals_run_ordinal"),
    UniqueConstraint("id", "account_id", name="uq_memory_proposals_id_account"),
    UniqueConstraint(
        "id",
        "account_id",
        "conversation_id",
        name="uq_memory_proposals_id_conversation_scope",
    ),
)
Index(
    "ix_memory_proposals_state_expires",
    memory_proposals.c.state,
    memory_proposals.c.expires_at,
)

memory_proposal_targets = Table(
    "memory_proposal_targets",
    metadata,
    Column("proposal_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("target_memory_id", UUID_TYPE, nullable=False),
    Column("target_version_no_snapshot", Integer, nullable=False),
    Column("target_role", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint(
        "proposal_id", "target_memory_id", "target_role", name="pk_memory_proposal_targets"
    ),
    ForeignKeyConstraint(
        ["proposal_id", "account_id"],
        ["memory_proposals.id", "memory_proposals.account_id"],
        name="fk_memory_proposal_targets_proposal_scope",
    ),
    ForeignKeyConstraint(
        ["target_memory_id", "account_id"],
        ["memories.id", "memories.account_id"],
        name="fk_memory_proposal_targets_memory_scope",
    ),
    CheckConstraint("target_version_no_snapshot > 0", name="target_version_positive"),
    CheckConstraint(
        "target_role IN ('primary','merge_source','superseded','invalidated')",
        name="target_role_values",
    ),
)
Index("ix_memory_proposal_targets_memory", memory_proposal_targets.c.target_memory_id)

memory_proposal_evidence = Table(
    "memory_proposal_evidence",
    metadata,
    Column("proposal_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("message_revision_id", UUID_TYPE, nullable=False),
    Column("media_object_id", UUID_TYPE),
    Column("evidence_role", Text, nullable=False),
    Column("quoted_span_start", Integer),
    Column("quoted_span_end", Integer),
    Column("source_content_sha256", LargeBinary, nullable=False),
    Column("source_normalization_version", Text, nullable=False),
    Column("trust_class", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint(
        "proposal_id", "message_revision_id", "evidence_role", name="pk_memory_proposal_evidence"
    ),
    ForeignKeyConstraint(
        ["proposal_id", "account_id"],
        ["memory_proposals.id", "memory_proposals.account_id"],
        name="fk_memory_proposal_evidence_proposal_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_memory_proposal_evidence_revision_scope",
    ),
    ForeignKeyConstraint(
        ["media_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_memory_proposal_evidence_media_scope",
    ),
    CheckConstraint(
        "quoted_span_start IS NULL OR quoted_span_start >= 0", name="span_start_nonnegative"
    ),
    CheckConstraint(
        "quoted_span_end IS NULL OR quoted_span_end > quoted_span_start", name="span_end_valid"
    ),
    CheckConstraint("octet_length(source_content_sha256) = 32", name="source_hash_32_bytes"),
)
Index("ix_memory_proposal_evidence_revision", memory_proposal_evidence.c.message_revision_id)

memory_evidence = Table(
    "memory_evidence",
    metadata,
    Column("memory_version_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("message_revision_id", UUID_TYPE),
    Column("summary_version_id", UUID_TYPE),
    Column("other_memory_version_id", UUID_TYPE),
    Column("media_object_id", UUID_TYPE),
    Column("evidence_role", Text, nullable=False),
    Column("trust_class", Text, nullable=False),
    Column("source_content_sha256", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint(
        "memory_version_id", "evidence_role", "source_content_sha256", name="pk_memory_evidence"
    ),
    ForeignKeyConstraint(
        ["memory_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_evidence_version_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_memory_evidence_revision_scope",
    ),
    ForeignKeyConstraint(
        ["summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_memory_evidence_summary_scope",
    ),
    ForeignKeyConstraint(
        ["other_memory_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_evidence_other_version_scope",
    ),
    ForeignKeyConstraint(
        ["media_object_id", "account_id"],
        ["media_objects.id", "media_objects.account_id"],
        name="fk_memory_evidence_media_scope",
    ),
    CheckConstraint(
        "num_nonnulls(message_revision_id, summary_version_id, other_memory_version_id) = 1",
        name="exactly_one_source",
    ),
    CheckConstraint(
        "media_object_id IS NULL OR message_revision_id IS NOT NULL", name="media_requires_message"
    ),
    CheckConstraint("octet_length(source_content_sha256) = 32", name="source_hash_32_bytes"),
)
Index(
    "ix_memory_evidence_message_revision",
    memory_evidence.c.message_revision_id,
    postgresql_where=memory_evidence.c.message_revision_id.is_not(None),
)
Index(
    "ix_memory_evidence_media",
    memory_evidence.c.media_object_id,
    postgresql_where=memory_evidence.c.media_object_id.is_not(None),
)

memory_relations = Table(
    "memory_relations",
    metadata,
    Column("from_version_id", UUID_TYPE, nullable=False),
    Column("to_version_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("relation_type", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint(
        "from_version_id", "to_version_id", "relation_type", name="pk_memory_relations"
    ),
    ForeignKeyConstraint(
        ["from_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_relations_from_scope",
    ),
    ForeignKeyConstraint(
        ["to_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_relations_to_scope",
    ),
    CheckConstraint("from_version_id <> to_version_id", name="not_self_relation"),
    CheckConstraint(
        "relation_type IN ('supports','contradicts','derived_from','merges','supersedes')",
        name="relation_values",
    ),
)

summaries = Table(
    "summaries",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("summary_kind", Text, nullable=False),
    Column("period_key", Text),
    Column("timezone_snapshot", Text),
    Column("period_start_at", UTC_TIMESTAMP),
    Column("period_end_at", UTC_TIMESTAMP),
    Column("status", Text, nullable=False),
    Column("current_version_no", Integer, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_summaries_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_summaries_conversation_scope",
    ),
    CheckConstraint(
        "summary_kind IN ('rolling','daily','weekly','consolidated')", name="kind_values"
    ),
    CheckConstraint("status IN ('active','quarantined','invalidated')", name="status_values"),
    CheckConstraint("current_version_no > 0", name="version_positive"),
    UniqueConstraint("conversation_id", "summary_kind", "period_key", name="uq_summaries_identity"),
    UniqueConstraint("id", "account_id", name="uq_summaries_id_account"),
)

summary_versions = Table(
    "summary_versions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("summary_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("range_start_event_id", BigInteger, nullable=False),
    Column("range_end_event_id", BigInteger, nullable=False),
    Column("period_start_at", UTC_TIMESTAMP),
    Column("period_end_at", UTC_TIMESTAMP),
    Column("timezone_snapshot", Text),
    Column("content_text", Text),
    Column("content_sha256", LargeBinary),
    Column("model_run_id", UUID_TYPE),
    Column("model_role", Text),
    Column("prompt_version", Text),
    Column("pipeline_version", Text, nullable=False),
    Column("output_schema_version", SmallInteger, nullable=False),
    Column("manifest_sha256", LargeBinary, nullable=False),
    Column("invalidation_state", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("redacted_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_summary_versions_account"),
    ForeignKeyConstraint(
        ["summary_id", "account_id"],
        ["summaries.id", "summaries.account_id"],
        name="fk_summary_versions_summary_scope",
    ),
    ForeignKeyConstraint(
        ["model_run_id", "account_id", "model_role"],
        ["model_runs.id", "model_runs.account_id", "model_runs.logical_role"],
        name="fk_summary_versions_model_run_scope",
    ),
    CheckConstraint(
        "version_no > 0 AND range_end_event_id >= range_start_event_id", name="version_range_values"
    ),
    CheckConstraint(
        "model_run_id IS NULL OR model_role = 'memory_agent'", name="model_role_values"
    ),
    CheckConstraint(
        "invalidation_state IN ('active','quarantined','invalidated')", name="invalidation_values"
    ),
    CheckConstraint(
        "content_text IS NULL OR octet_length(content_sha256) = 32", name="content_hash_matches"
    ),
    CheckConstraint("octet_length(manifest_sha256) = 32", name="manifest_hash_32_bytes"),
    UniqueConstraint("summary_id", "version_no", name="uq_summary_versions_no"),
    UniqueConstraint(
        "summary_id",
        "account_id",
        "version_no",
        name="uq_summary_versions_account_no",
    ),
    UniqueConstraint("id", "account_id", name="uq_summary_versions_id_account"),
)
summaries.append_constraint(
    ForeignKeyConstraint(
        ["id", "account_id", "current_version_no"],
        [
            "summary_versions.summary_id",
            "summary_versions.account_id",
            "summary_versions.version_no",
        ],
        name="fk_summaries_current_version",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

summary_version_sources = Table(
    "summary_version_sources",
    metadata,
    Column("summary_version_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("message_revision_id", UUID_TYPE),
    Column("prior_summary_version_id", UUID_TYPE),
    Column("inclusion_role", Text, nullable=False),
    Column("source_content_sha256", LargeBinary, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint("summary_version_id", "ordinal", name="pk_summary_version_sources"),
    ForeignKeyConstraint(
        ["summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_summary_sources_version_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_summary_sources_revision_scope",
    ),
    ForeignKeyConstraint(
        ["prior_summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_summary_sources_prior_scope",
    ),
    CheckConstraint(
        "num_nonnulls(message_revision_id, prior_summary_version_id) = 1", name="exactly_one_source"
    ),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint("octet_length(source_content_sha256) = 32", name="source_hash_32_bytes"),
)
Index("ix_summary_version_sources_revision", summary_version_sources.c.message_revision_id)
Index("ix_summary_version_sources_prior", summary_version_sources.c.prior_summary_version_id)

summary_watermarks = Table(
    "summary_watermarks",
    metadata,
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("summary_kind", Text, nullable=False),
    Column("last_included_event_id", BigInteger, nullable=False, server_default=text("0")),
    Column("last_summary_version_id", UUID_TYPE),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    PrimaryKeyConstraint("conversation_id", "summary_kind", name="pk_summary_watermarks"),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_summary_watermarks_account"),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_summary_watermarks_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["last_summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_summary_watermarks_version_scope",
    ),
    CheckConstraint(
        "summary_kind IN ('rolling','daily','weekly','consolidated')", name="kind_values"
    ),
    CheckConstraint("last_included_event_id >= 0 AND version > 0", name="watermark_values"),
)

memory_input_manifest_items.append_constraint(
    ForeignKeyConstraint(
        ["memory_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_memory_manifest_items_memory_version_scope",
        use_alter=True,
    )
)
memory_input_manifest_items.append_constraint(
    ForeignKeyConstraint(
        ["summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_memory_manifest_items_summary_version_scope",
        use_alter=True,
    )
)

embedding_spaces = Table(
    "embedding_spaces",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE),
    Column("model_profile_id", UUID_TYPE, nullable=False),
    Column("profile_kind", Text, nullable=False, server_default=text("'embedding'")),
    Column("config_version_id", UUID_TYPE, nullable=False),
    Column("model_name_snapshot", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("distance_metric", Text, nullable=False),
    Column("normalization", Text, nullable=False),
    Column("chunker_version", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("activated_at", UTC_TIMESTAMP),
    Column("retired_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_embedding_spaces_account"),
    ForeignKeyConstraint(
        ["model_profile_id", "profile_kind"],
        ["model_profiles.id", "model_profiles.profile_kind"],
        name="fk_embedding_spaces_profile_kind",
    ),
    ForeignKeyConstraint(
        ["config_version_id", "model_profile_id"],
        ["model_config_versions.id", "model_config_versions.profile_id"],
        name="fk_embedding_spaces_config_profile",
    ),
    CheckConstraint("profile_kind = 'embedding'", name="profile_kind_values"),
    CheckConstraint("dimensions > 0 AND generation > 0", name="space_positive"),
    CheckConstraint("distance_metric IN ('cosine','inner_product','l2')", name="distance_values"),
    CheckConstraint("normalization IN ('none','l2')", name="normalization_values"),
    CheckConstraint("state IN ('building','active','retired','failed')", name="state_values"),
    UniqueConstraint("id", "dimensions", name="uq_embedding_spaces_id_dimensions"),
    UniqueConstraint("id", "account_id", name="uq_embedding_spaces_id_account"),
    UniqueConstraint(
        "id",
        "account_id",
        "dimensions",
        name="uq_embedding_spaces_account_dimensions",
    ),
)
Index(
    "uq_embedding_spaces_active",
    embedding_spaces.c.model_profile_id,
    embedding_spaces.c.account_id,
    unique=True,
    postgresql_where=embedding_spaces.c.state == "active",
    postgresql_nulls_not_distinct=True,
)

embedding_records = Table(
    "embedding_records",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("embedding_space_id", UUID_TYPE, nullable=False),
    Column("memory_version_id", UUID_TYPE),
    Column("summary_version_id", UUID_TYPE),
    Column("message_revision_id", UUID_TYPE),
    Column("chunk_index", Integer, nullable=False),
    Column("chunker_version", Text, nullable=False),
    Column("source_sha256", LargeBinary, nullable=False),
    Column("vector_payload", JSONB, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("invalidated_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_embedding_records_account"),
    ForeignKeyConstraint(
        ["embedding_space_id", "account_id", "dimensions"],
        ["embedding_spaces.id", "embedding_spaces.account_id", "embedding_spaces.dimensions"],
        name="fk_embedding_records_space_scope",
    ),
    ForeignKeyConstraint(
        ["memory_version_id", "account_id"],
        ["memory_versions.id", "memory_versions.account_id"],
        name="fk_embedding_records_memory_version_scope",
    ),
    ForeignKeyConstraint(
        ["summary_version_id", "account_id"],
        ["summary_versions.id", "summary_versions.account_id"],
        name="fk_embedding_records_summary_version_scope",
    ),
    ForeignKeyConstraint(
        ["message_revision_id", "account_id"],
        ["message_revisions.id", "message_revisions.account_id"],
        name="fk_embedding_records_message_revision_scope",
    ),
    CheckConstraint(
        "num_nonnulls(memory_version_id, summary_version_id, message_revision_id) = 1",
        name="exactly_one_target",
    ),
    CheckConstraint("chunk_index >= 0 AND dimensions > 0", name="chunk_values"),
    CheckConstraint("octet_length(source_sha256) = 32", name="source_hash_32_bytes"),
    CheckConstraint("state IN ('pending','ready','invalidated','failed')", name="state_values"),
)
Index(
    "uq_embedding_records_memory_chunk",
    embedding_records.c.embedding_space_id,
    embedding_records.c.memory_version_id,
    embedding_records.c.chunk_index,
    unique=True,
    postgresql_where=embedding_records.c.memory_version_id.is_not(None),
)
Index(
    "uq_embedding_records_summary_chunk",
    embedding_records.c.embedding_space_id,
    embedding_records.c.summary_version_id,
    embedding_records.c.chunk_index,
    unique=True,
    postgresql_where=embedding_records.c.summary_version_id.is_not(None),
)
Index(
    "uq_embedding_records_message_chunk",
    embedding_records.c.embedding_space_id,
    embedding_records.c.message_revision_id,
    embedding_records.c.chunk_index,
    unique=True,
    postgresql_where=embedding_records.c.message_revision_id.is_not(None),
)
Index(
    "ix_embedding_records_active_target",
    embedding_records.c.embedding_space_id,
    embedding_records.c.state,
    postgresql_where=embedding_records.c.state == "ready",
)

memory_review_actions = Table(
    "memory_review_actions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("action", Text, nullable=False),
    Column("proposal_id", UUID_TYPE),
    Column("memory_id", UUID_TYPE),
    Column("expected_proposal_version", Integer),
    Column("expected_memory_version", Integer),
    Column("admin_actor_id", BigInteger, nullable=False),
    Column("bot_chat_id", BigInteger, nullable=False),
    Column("action_token_hash", LargeBinary, nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("used_at", UTC_TIMESTAMP),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("decided_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_memory_review_actions_account"),
    ForeignKeyConstraint(
        ["proposal_id", "account_id", "conversation_id"],
        [
            "memory_proposals.id",
            "memory_proposals.account_id",
            "memory_proposals.conversation_id",
        ],
        name="fk_memory_review_actions_proposal_scope",
    ),
    ForeignKeyConstraint(
        ["memory_id", "account_id", "conversation_id"],
        ["memories.id", "memories.account_id", "memories.conversation_id"],
        name="fk_memory_review_actions_memory_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_memory_review_actions_conversation_scope",
    ),
    CheckConstraint("action IN ('accept','reject','forget')", name="action_values"),
    CheckConstraint(
        "state IN ('pending','confirmed','applied','rejected','expired')", name="state_values"
    ),
    CheckConstraint("octet_length(action_token_hash) = 32", name="action_token_hash_32_bytes"),
    CheckConstraint(
        "(action IN ('accept','reject') AND proposal_id IS NOT NULL AND memory_id IS NULL "
        "AND expected_proposal_version IS NOT NULL AND expected_memory_version IS NULL) OR "
        "(action = 'forget' AND proposal_id IS NULL AND memory_id IS NOT NULL "
        "AND expected_proposal_version IS NULL AND expected_memory_version IS NOT NULL)",
        name="target_matches_action",
    ),
    UniqueConstraint("action_token_hash", name="uq_memory_review_actions_token"),
)
Index(
    "ix_memory_review_actions_open_expiry",
    memory_review_actions.c.expires_at,
    postgresql_where=memory_review_actions.c.used_at.is_(None),
)

M6_TABLES = (
    "embedding_spaces",
    "memories",
    "memory_jobs",
    "summaries",
    "memory_input_manifests",
    "memory_watermarks",
    "memory_input_manifest_items",
    "memory_versions",
    "summary_versions",
    "embedding_records",
    "memory_evidence",
    "memory_proposals",
    "memory_relations",
    "summary_version_sources",
    "summary_watermarks",
    "memory_proposal_evidence",
    "memory_proposal_targets",
    "memory_review_actions",
)


proactive_policies = Table(
    "proactive_policies",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("timezone_name", Text, nullable=False, server_default=text("'UTC'")),
    Column("quiet_start_local", Text, nullable=False, server_default=text("'22:00'")),
    Column("quiet_end_local", Text, nullable=False, server_default=text("'08:00'")),
    Column("absolute_no_send_start_local", Text, nullable=False, server_default=text("'00:00'")),
    Column("absolute_no_send_end_local", Text, nullable=False, server_default=text("'07:00'")),
    Column("account_daily_limit", Integer, nullable=False, server_default=text("10")),
    Column("contact_bypass_daily_limit", Integer, nullable=False, server_default=text("1")),
    Column("activity_suppression_seconds", Integer, nullable=False, server_default=text("1800")),
    Column("settings_json", JSONB, nullable=False, server_default=EMPTY_JSON),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_policies_account"),
    CheckConstraint("version_no > 0", name="version_positive"),
    CheckConstraint("account_daily_limit >= 0", name="account_limit_nonnegative"),
    CheckConstraint("contact_bypass_daily_limit IN (0, 1)", name="bypass_limit_values"),
    CheckConstraint("activity_suppression_seconds > 0", name="activity_suppression_positive"),
    CheckConstraint("absolute_no_send_start_local = '00:00'", name="absolute_start_fixed"),
    CheckConstraint("absolute_no_send_end_local = '07:00'", name="absolute_end_fixed"),
    UniqueConstraint("account_id", "version_no", name="uq_proactive_policies_account_version"),
    UniqueConstraint("id", "account_id", name="uq_proactive_policies_id_account"),
)

proactive_contact_settings = Table(
    "proactive_contact_settings",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("relationship_level", Text, nullable=False, server_default=text("'unknown'")),
    Column("timezone_name", Text),
    Column("daily_limit", Integer),
    Column("minimum_interval_seconds", Integer),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_contact_settings_contact_scope",
    ),
    CheckConstraint("version_no > 0", name="version_positive"),
    CheckConstraint(
        "relationship_level IN ('close','friend','acquaintance','unknown')",
        name="relationship_values",
    ),
    CheckConstraint("daily_limit IS NULL OR daily_limit >= 0", name="daily_limit_nonnegative"),
    CheckConstraint(
        "minimum_interval_seconds IS NULL OR minimum_interval_seconds > 0",
        name="minimum_interval_positive",
    ),
    UniqueConstraint(
        "account_id", "contact_id", "version_no", name="uq_proactive_contact_settings_version"
    ),
    UniqueConstraint("id", "account_id", name="uq_proactive_contact_settings_id_account"),
)


def _proactive_projection_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("id", UUID_TYPE, primary_key=True),
        Column("account_id", UUID_TYPE, nullable=False),
        Column("contact_id", UUID_TYPE, nullable=False),
        Column("conversation_id", UUID_TYPE, nullable=False),
        Column("version_no", Integer, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'active'")),
        Column("timezone_name", Text, nullable=False),
        Column("importance", Numeric(5, 4), nullable=False),
        Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        Column("source_hash", LargeBinary, nullable=False),
        Column("valid_from_at", UTC_TIMESTAMP),
        Column("valid_until_at", UTC_TIMESTAMP),
        Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
        ForeignKeyConstraint(["account_id"], ["accounts.id"], name=f"fk_{name}_account"),
        ForeignKeyConstraint(
            ["contact_id", "account_id"],
            ["contacts.id", "contacts.account_id"],
            name=f"fk_{name}_contact_scope",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "account_id"],
            ["conversations.id", "conversations.account_id"],
            name=f"fk_{name}_conversation_scope",
        ),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint("status IN ('active','invalidated','expired')", name="status_values"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_bounded"),
        CheckConstraint("octet_length(source_hash) = 32", name="source_hash_32_bytes"),
        CheckConstraint(
            "valid_until_at IS NULL OR valid_from_at IS NULL OR valid_until_at > valid_from_at",
            name="valid_range",
        ),
        UniqueConstraint("id", "account_id", name=f"uq_{name}_id_account"),
        UniqueConstraint("account_id", "id", "version_no", name=f"uq_{name}_version"),
    )


proactive_life_events = _proactive_projection_table("proactive_life_events")
proactive_intentions = _proactive_projection_table("proactive_intentions")
proactive_relationships = _proactive_projection_table("proactive_relationships")

proactive_occurrences = Table(
    "proactive_occurrences",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("occurrence_key", LargeBinary, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("window_start_at", UTC_TIMESTAMP, nullable=False),
    Column("window_end_at", UTC_TIMESTAMP, nullable=False),
    Column("hard_deadline_at", UTC_TIMESTAMP, nullable=False),
    Column("timezone_name", Text, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("importance", Numeric(5, 4), nullable=False),
    Column("source_type", Text, nullable=False),
    Column("source_id", UUID_TYPE, nullable=False),
    Column("source_version", Text, nullable=False),
    Column("policy_version_id", UUID_TYPE),
    Column("quiet_bypass_possible", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_occurrences_account"),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_occurrences_contact_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_proactive_occurrences_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["policy_version_id", "account_id"],
        ["proactive_policies.id", "proactive_policies.account_id"],
        name="fk_proactive_occurrences_policy_scope",
    ),
    CheckConstraint("octet_length(occurrence_key) = 32", name="occurrence_key_32_bytes"),
    CheckConstraint("generation > 0", name="generation_positive"),
    CheckConstraint(
        "reason IN ('promise_due','event_upcoming','event_followup',"
        "'relationship_reconnect','explicit_followup')",
        name="reason_values",
    ),
    CheckConstraint(
        "state IN ('scheduled','eligible','grouped','evaluated','suppressed',"
        "'invalidated','expired')",
        name="state_values",
    ),
    CheckConstraint(
        "window_start_at < window_end_at AND hard_deadline_at <= window_end_at",
        name="window_values",
    ),
    CheckConstraint("importance >= 0 AND importance <= 1", name="importance_bounded"),
    UniqueConstraint("occurrence_key", name="uq_proactive_occurrences_key"),
    UniqueConstraint("id", "account_id", name="uq_proactive_occurrences_id_account"),
)
Index(
    "ix_proactive_occurrences_due",
    proactive_occurrences.c.state,
    proactive_occurrences.c.window_start_at,
)

proactive_occurrence_evidence = Table(
    "proactive_occurrence_evidence",
    metadata,
    Column("occurrence_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("source_id", UUID_TYPE, nullable=False),
    Column("source_version", Text, nullable=False),
    Column("source_hash", LargeBinary, nullable=False),
    Column("summary", Text, nullable=False),
    Column("current", Boolean, nullable=False, server_default=text("true")),
    Column("explicit", Boolean, nullable=False, server_default=text("true")),
    ForeignKeyConstraint(
        ["occurrence_id", "account_id"],
        ["proactive_occurrences.id", "proactive_occurrences.account_id"],
        name="fk_proactive_occurrence_evidence_occurrence_scope",
    ),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    CheckConstraint("octet_length(source_hash) = 32", name="source_hash_32_bytes"),
    CheckConstraint("length(summary) BETWEEN 1 AND 500", name="summary_length"),
    PrimaryKeyConstraint("occurrence_id", "ordinal", name="pk_proactive_occurrence_evidence"),
)

proactive_candidates = Table(
    "proactive_candidates",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("candidate_key", LargeBinary, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("membership_hash", LargeBinary, nullable=False),
    Column("state", Text, nullable=False),
    Column("window_start_at", UTC_TIMESTAMP, nullable=False),
    Column("window_end_at", UTC_TIMESTAMP, nullable=False),
    Column("due_at", UTC_TIMESTAMP, nullable=False),
    Column("policy_version_id", UUID_TYPE),
    Column("timezone_name", Text, nullable=False),
    Column("mode_version", BigInteger, nullable=False),
    Column("content_revision", BigInteger, nullable=False),
    Column("activity_revision", BigInteger, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_candidates_account"),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_candidates_contact_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_proactive_candidates_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["policy_version_id", "account_id"],
        ["proactive_policies.id", "proactive_policies.account_id"],
        name="fk_proactive_candidates_policy_scope",
    ),
    CheckConstraint("octet_length(candidate_key) = 32", name="candidate_key_32_bytes"),
    CheckConstraint("octet_length(membership_hash) = 32", name="membership_hash_32_bytes"),
    CheckConstraint(
        "generation > 0 AND mode_version > 0 AND content_revision >= 0 AND activity_revision >= 0",
        name="snapshot_values",
    ),
    CheckConstraint(
        "state IN ('open','evaluating','send_selected','deferred_once',"
        "'evaluated_none','failed_model','superseded','expired')",
        name="state_values",
    ),
    CheckConstraint(
        "window_start_at < window_end_at AND due_at <= window_end_at", name="window_values"
    ),
    UniqueConstraint("candidate_key", name="uq_proactive_candidates_key"),
    UniqueConstraint("id", "account_id", name="uq_proactive_candidates_id_account"),
)
Index("ix_proactive_candidates_due", proactive_candidates.c.state, proactive_candidates.c.due_at)

proactive_candidate_memberships = Table(
    "proactive_candidate_memberships",
    metadata,
    Column("candidate_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("occurrence_id", UUID_TYPE, nullable=False),
    Column("occurrence_generation", Integer, nullable=False),
    Column("occurrence_key", LargeBinary, nullable=False),
    ForeignKeyConstraint(
        ["candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_candidate_memberships_candidate_scope",
    ),
    ForeignKeyConstraint(
        ["occurrence_id", "account_id"],
        ["proactive_occurrences.id", "proactive_occurrences.account_id"],
        name="fk_proactive_candidate_memberships_occurrence_scope",
    ),
    CheckConstraint("ordinal > 0 AND occurrence_generation > 0", name="membership_values"),
    CheckConstraint("octet_length(occurrence_key) = 32", name="occurrence_key_32_bytes"),
    PrimaryKeyConstraint("candidate_id", "ordinal", name="pk_proactive_candidate_memberships"),
    UniqueConstraint(
        "candidate_id", "occurrence_id", name="uq_proactive_candidate_memberships_occurrence"
    ),
)

proactive_jobs = Table(
    "proactive_jobs",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("candidate_id", UUID_TYPE),
    Column("job_kind", Text, nullable=False),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("available_at", UTC_TIMESTAMP, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("lease_owner", UUID_TYPE),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("completed_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_jobs_account"),
    ForeignKeyConstraint(
        ["candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_jobs_candidate_scope",
    ),
    CheckConstraint("octet_length(idempotency_key) = 32", name="idempotency_key_32_bytes"),
    CheckConstraint(
        "job_kind IN ('candidate_due','compensation_scan','budget_reaper')", name="job_kind_values"
    ),
    CheckConstraint(
        "state IN ('pending','leased','retry_wait','succeeded','expired','dead_letter')",
        name="state_values",
    ),
    CheckConstraint("attempt_count >= 0", name="attempt_nonnegative"),
    CheckConstraint("fencing_token >= 0", name="fencing_token_nonnegative"),
    CheckConstraint(
        "(state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
        name="lease_fields_match",
    ),
    UniqueConstraint("idempotency_key", name="uq_proactive_jobs_idempotency"),
)
Index("ix_proactive_jobs_due", proactive_jobs.c.state, proactive_jobs.c.available_at)

proactive_budget_buckets = Table(
    "proactive_budget_buckets",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE),
    Column("scope", Text, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("timezone_name_snapshot", Text, nullable=False),
    Column("starts_at", UTC_TIMESTAMP, nullable=False),
    Column("ends_at", UTC_TIMESTAMP, nullable=False),
    Column("limit_value", Integer, nullable=False),
    Column("held_count", Integer, nullable=False, server_default=text("0")),
    Column("committed_count", Integer, nullable=False, server_default=text("0")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["account_id"], ["accounts.id"], name="fk_proactive_budget_buckets_account"
    ),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_budget_buckets_contact_scope",
    ),
    CheckConstraint(
        "scope IN ('account_daily','contact_daily','contact_bypass')", name="scope_values"
    ),
    CheckConstraint(
        "(scope = 'account_daily' AND contact_id IS NULL) OR "
        "(scope IN ('contact_daily','contact_bypass') AND contact_id IS NOT NULL)",
        name="scope_contact_match",
    ),
    CheckConstraint("starts_at < ends_at", name="window_values"),
    CheckConstraint(
        "limit_value >= 0 AND held_count >= 0 AND committed_count >= 0", name="count_nonnegative"
    ),
    CheckConstraint("held_count + committed_count <= limit_value", name="count_within_limit"),
    CheckConstraint("version > 0", name="version_positive"),
    UniqueConstraint(
        "account_id",
        "contact_id",
        "scope",
        "local_date",
        name="uq_proactive_budget_bucket_identity",
        postgresql_nulls_not_distinct=True,
    ),
    UniqueConstraint("id", "account_id", name="uq_proactive_budget_buckets_id_account"),
)

proactive_budget_reservations = Table(
    "proactive_budget_reservations",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("candidate_id", UUID_TYPE, nullable=False),
    Column("decision_id", UUID_TYPE, nullable=False),
    Column("policy_version_id", UUID_TYPE, nullable=False),
    Column("authorization_generation", Integer, nullable=False),
    Column("target", Text, nullable=False),
    Column("account_bucket_id", UUID_TYPE, nullable=False),
    Column("contact_bucket_id", UUID_TYPE, nullable=False),
    Column("bypass_bucket_id", UUID_TYPE),
    Column("reservation_key", LargeBinary, nullable=False),
    Column("account_local_date", Date, nullable=False),
    Column("contact_local_date", Date, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("bypass", Boolean, nullable=False, server_default=text("false")),
    Column("state", Text, nullable=False, server_default=text("'held'")),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("held_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("committed_at", UTC_TIMESTAMP),
    Column("terminal_at", UTC_TIMESTAMP),
    Column("outbound_group_id", UUID_TYPE),
    Column("copilot_draft_id", UUID_TYPE),
    Column("reason_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["account_id"], ["accounts.id"], name="fk_proactive_budget_reservations_account"
    ),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_budget_reservations_contact_scope",
    ),
    ForeignKeyConstraint(
        ["candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_budget_reservations_candidate_scope",
    ),
    ForeignKeyConstraint(
        ["decision_id", "account_id"],
        ["proactive_decisions.id", "proactive_decisions.account_id"],
        name="fk_proactive_budget_reservations_decision_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_proactive_budget_reservations_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["policy_version_id", "account_id"],
        ["proactive_policies.id", "proactive_policies.account_id"],
        name="fk_proactive_budget_reservations_policy_scope",
    ),
    ForeignKeyConstraint(
        ["account_bucket_id", "account_id"],
        ["proactive_budget_buckets.id", "proactive_budget_buckets.account_id"],
        name="fk_proactive_budget_reservations_account_bucket",
    ),
    ForeignKeyConstraint(
        ["contact_bucket_id", "account_id"],
        ["proactive_budget_buckets.id", "proactive_budget_buckets.account_id"],
        name="fk_proactive_budget_reservations_contact_bucket",
    ),
    ForeignKeyConstraint(
        ["bypass_bucket_id", "account_id"],
        ["proactive_budget_buckets.id", "proactive_budget_buckets.account_id"],
        name="fk_proactive_budget_reservations_bypass_bucket",
    ),
    ForeignKeyConstraint(
        ["outbound_group_id", "account_id", "conversation_id"],
        [
            "outbound_delivery_groups.id",
            "outbound_delivery_groups.account_id",
            "outbound_delivery_groups.conversation_id",
        ],
        name="fk_proactive_budget_reservations_outbound_group_scope",
    ),
    ForeignKeyConstraint(
        ["copilot_draft_id", "account_id", "conversation_id"],
        ["copilot_drafts.id", "copilot_drafts.account_id", "copilot_drafts.conversation_id"],
        name="fk_proactive_budget_reservations_copilot_draft_scope",
    ),
    CheckConstraint("octet_length(reservation_key) = 32", name="reservation_key_32_bytes"),
    CheckConstraint("authorization_generation > 0", name="authorization_generation_positive"),
    CheckConstraint("target IN ('auto_send','copilot_draft')", name="target_values"),
    CheckConstraint(
        "((target = 'auto_send' AND copilot_draft_id IS NULL) OR "
        "(target = 'copilot_draft' AND outbound_group_id IS NULL)) AND "
        "(state NOT IN ('committed','send_unknown') OR "
        "(target = 'auto_send' AND outbound_group_id IS NOT NULL) OR "
        "(target = 'copilot_draft' AND copilot_draft_id IS NOT NULL))",
        name="target_side_effect_match",
    ),
    CheckConstraint(
        "(bypass AND bypass_bucket_id IS NOT NULL) OR (NOT bypass AND bypass_bucket_id IS NULL)",
        name="bypass_bucket_match",
    ),
    CheckConstraint("local_date = contact_local_date", name="legacy_local_date_match"),
    CheckConstraint(
        "state IN ('held','committed','released','expired','send_unknown')", name="state_values"
    ),
    UniqueConstraint(
        "account_id", "reservation_key", name="uq_proactive_budget_reservations_account_key"
    ),
)
Index(
    "uq_proactive_budget_reservations_active_decision",
    proactive_budget_reservations.c.decision_id,
    unique=True,
    postgresql_where=proactive_budget_reservations.c.state.in_(
        ("held", "committed", "send_unknown")
    ),
)

proactive_decisions = Table(
    "proactive_decisions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("contact_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("candidate_id", UUID_TYPE, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("policy_version_id", UUID_TYPE, nullable=False),
    Column("timezone_name", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("decision_code", Text, nullable=False),
    Column("topic", Text),
    Column("priority", Numeric(5, 4), nullable=False),
    Column("defer_until", UTC_TIMESTAMP),
    Column("output_hash", LargeBinary, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'accepted'")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_decisions_account"),
    ForeignKeyConstraint(
        ["contact_id", "account_id"],
        ["contacts.id", "contacts.account_id"],
        name="fk_proactive_decisions_contact_scope",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_proactive_decisions_conversation_scope",
    ),
    ForeignKeyConstraint(
        ["candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_decisions_candidate_scope",
    ),
    ForeignKeyConstraint(
        ["policy_version_id", "account_id"],
        ["proactive_policies.id", "proactive_policies.account_id"],
        name="fk_proactive_decisions_policy_scope",
    ),
    CheckConstraint("generation > 0", name="generation_positive"),
    CheckConstraint("action IN ('send_now','defer_once','none')", name="action_values"),
    CheckConstraint(
        "decision_code IN ('timely_support','better_later_in_window',"
        "'not_natural_now','insufficient_context')",
        name="decision_code_values",
    ),
    CheckConstraint("priority >= 0 AND priority <= 1", name="priority_bounded"),
    CheckConstraint("octet_length(output_hash) = 32", name="output_hash_32_bytes"),
    CheckConstraint("state IN ('accepted','rejected','stale')", name="state_values"),
    UniqueConstraint("candidate_id", name="uq_proactive_decisions_candidate"),
    UniqueConstraint("id", "account_id", name="uq_proactive_decisions_id_account"),
    UniqueConstraint(
        "id", "account_id", "conversation_id", name="uq_proactive_decisions_full_scope"
    ),
    UniqueConstraint(
        "id", "account_id", "candidate_id", name="uq_proactive_decisions_candidate_scope"
    ),
)

proactive_decision_memberships = Table(
    "proactive_decision_memberships",
    metadata,
    Column("decision_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("occurrence_id", UUID_TYPE, nullable=False),
    ForeignKeyConstraint(
        ["decision_id", "account_id"],
        ["proactive_decisions.id", "proactive_decisions.account_id"],
        name="fk_proactive_decision_memberships_decision_scope",
    ),
    ForeignKeyConstraint(
        ["occurrence_id", "account_id"],
        ["proactive_occurrences.id", "proactive_occurrences.account_id"],
        name="fk_proactive_decision_memberships_occurrence_scope",
    ),
    CheckConstraint("ordinal > 0", name="ordinal_positive"),
    PrimaryKeyConstraint("decision_id", "ordinal", name="pk_proactive_decision_memberships"),
    UniqueConstraint(
        "decision_id", "occurrence_id", name="uq_proactive_decision_memberships_occurrence"
    ),
)

proactive_state_transitions = Table(
    "proactive_state_transitions",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("candidate_id", UUID_TYPE, nullable=False),
    Column("from_state", Text),
    Column("to_state", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("reason", Text),
    Column("actor", Text, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(
        ["account_id"], ["accounts.id"], name="fk_proactive_state_transitions_account"
    ),
    ForeignKeyConstraint(
        ["candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_state_transitions_candidate_scope",
    ),
    CheckConstraint("actor IN ('rule','agent','worker','app','control')", name="actor_values"),
    UniqueConstraint("id", "account_id", name="uq_proactive_state_transitions_id_account"),
)

proactive_scan_cursors = Table(
    "proactive_scan_cursors",
    metadata,
    Column("account_id", UUID_TYPE, nullable=False),
    Column("cursor_kind", Text, nullable=False),
    Column("last_scanned_at", UTC_TIMESTAMP),
    Column("last_candidate_id", UUID_TYPE),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_proactive_scan_cursors_account"),
    ForeignKeyConstraint(
        ["last_candidate_id", "account_id"],
        ["proactive_candidates.id", "proactive_candidates.account_id"],
        name="fk_proactive_scan_cursors_candidate_scope",
    ),
    CheckConstraint(
        "cursor_kind IN ('due','compensation','budget_reaper')", name="cursor_kind_values"
    ),
    CheckConstraint("version > 0", name="version_positive"),
    PrimaryKeyConstraint("account_id", "cursor_kind", name="pk_proactive_scan_cursors"),
)

M7_TABLES = (
    "proactive_policies",
    "proactive_contact_settings",
    "proactive_life_events",
    "proactive_intentions",
    "proactive_relationships",
    "proactive_occurrences",
    "proactive_occurrence_evidence",
    "proactive_candidates",
    "proactive_candidate_memberships",
    "proactive_jobs",
    "proactive_budget_buckets",
    "proactive_budget_reservations",
    "proactive_decisions",
    "proactive_decision_memberships",
    "proactive_state_transitions",
    "proactive_scan_cursors",
)
