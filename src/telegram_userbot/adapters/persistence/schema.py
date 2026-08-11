"""SQLAlchemy Core schema for the M1 durable-state boundary.

PostgreSQL is the canonical source of durable facts.  This module is imported by
Alembic and repositories, but application startup never calls ``create_all``.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
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
    Column(
        "source_event_id",
        BigInteger,
        ForeignKey("message_events.id", name="fk_message_revisions_source_event"),
        nullable=False,
    ),
    Column("telegram_edited_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("redacted_at", UTC_TIMESTAMP),
    Column("redaction_reason", Text),
    ForeignKeyConstraint(
        ["message_id", "account_id"],
        ["messages.id", "messages.account_id"],
        name="fk_message_revisions_message_scope",
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
    Column("parent_object_id", UUID_TYPE, ForeignKey("media_objects.id")),
    Column("validation_error_code", Text),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("ready_at", UTC_TIMESTAMP),
    Column("delete_requested_at", UTC_TIMESTAMP),
    Column("deleted_at", UTC_TIMESTAMP),
    Column("retention_class", Text, nullable=False),
    Column("expires_at", UTC_TIMESTAMP),
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
    Column("media_object_id", UUID_TYPE, ForeignKey("media_objects.id")),
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
    Column("supersedes_turn_id", UUID_TYPE, ForeignKey("conversation_turns.id")),
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
    Column("quiet_deadline_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=NOW),
    Column("completed_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["conversation_id", "account_id"],
        ["conversations.id", "conversations.account_id"],
        name="fk_conversation_turns_scope",
    ),
    CheckConstraint(
        "state IN ('collecting','sealed','generating','superseded','completed',"
        "'cancelled','failed')",
        name="state_values",
    ),
    CheckConstraint(
        "trigger_kind IN ('incoming','replacement','copilot','proactive')",
        name="trigger_kind_values",
    ),
    CheckConstraint("collection_sequence >= 1", name="collection_sequence_positive"),
    CheckConstraint("active_generation_no >= 0", name="generation_nonnegative"),
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
    Column("model_run_id", UUID_TYPE),
    Column("source", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'planned'")),
    Column("intent_count", Integer, nullable=False),
    Column("sent_count", Integer, nullable=False, server_default=text("0")),
    Column("idempotency_key", LargeBinary, nullable=False),
    Column("mode_version", BigInteger),
    Column("content_revision", BigInteger),
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
        "state IN ('planned','sending','partial','sent','failed','unknown','cancelled')",
        name="state_values",
    ),
    CheckConstraint("intent_count > 0", name="intent_count_positive"),
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

outbound_intents = Table(
    "outbound_intents",
    metadata,
    Column("id", UUID_TYPE, primary_key=True),
    Column("delivery_group_id", UUID_TYPE, nullable=False),
    Column("account_id", UUID_TYPE, nullable=False),
    Column("conversation_id", UUID_TYPE, nullable=False),
    Column("model_run_id", UUID_TYPE),
    Column("sequence_no", Integer, nullable=False),
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
