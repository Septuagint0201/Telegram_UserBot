"""Detached records returned across repository boundaries."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class JobState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: UUID
    telegram_user_id: int
    display_label: str
    status: str
    default_timezone: str


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    id: UUID
    account_id: UUID
    mode_version: int
    content_revision: int
    base_mode_override: str | None
    contact_paused: bool


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: UUID
    account_id: UUID | None
    queue_name: str
    job_type: str
    state: JobState
    priority: int
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    version: int
    fencing_token: int
    dispatch_generation: int


@dataclass(frozen=True, slots=True)
class NewJobRecord:
    id: UUID
    account_id: UUID | None
    queue_name: str
    job_type: str
    idempotency_key: bytes
    payload: dict[str, Any]
    available_at: datetime
    priority: int = 0
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: int
    topic: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelProfileRecord:
    id: UUID
    logical_role: str
    profile_kind: str
    state: str
    active_config_version_no: int | None
    version: int
    credential_id: UUID
    credential_status: str
    credential_active_version_no: int | None
    credential_version: int


@dataclass(frozen=True, slots=True)
class ModelControlProfileRecord:
    id: UUID
    logical_role: str
    profile_kind: str
    state: str
    active_config_version_no: int | None
    version: int
    credential_id: UUID
    credential_status: str
    credential_version: int
    protocol: str | None
    model_name: str | None
    endpoint_label: str | None


@dataclass(frozen=True, slots=True)
class ModelControlDraftRecord:
    session_id: UUID | None
    draft_id: UUID
    profile_id: UUID
    logical_role: str
    profile_kind: str
    credential_id: UUID
    expected_profile_version: int
    draft_version: int
    state: str
    pending_field: str | None
    endpoint_id: UUID | None
    protocol: str | None
    model_name: str | None
    temperature: float | None
    max_output_tokens: int | None
    timeout_seconds: int | None
    enabled: bool | None
    protocol_options: dict[str, object]
    capability_snapshot_id: UUID | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ModelConfigSnapshotRecord:
    id: UUID
    profile_id: UUID
    version_no: int
    endpoint_id: UUID
    credential_id: UUID
    credential_version_no: int
    capability_snapshot_id: UUID | None
    canonical_payload: dict[str, Any]
    config_sha256: bytes


@dataclass(frozen=True, slots=True)
class ClaimedKeyLaunchRecord:
    profile_id: UUID
    logical_role: str
    credential_id: UUID
    credential_status: str
    credential_version: int


@dataclass(frozen=True, slots=True)
class TelegramIngestResult:
    event_id: int
    duplicate: bool
    projected: bool
    message_id: UUID | None = None
    revision_no: int | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundIntentRecord:
    id: UUID
    delivery_group_id: UUID
    account_id: UUID
    conversation_id: UUID
    model_run_id: UUID | None
    sequence_no: int
    telegram_random_id: int
    text_content: str
    payload_sha256: bytes
    state: str
    telegram_message_id: int | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class NewDeliveryGroupRecord:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    model_run_id: UUID | None
    source: str
    idempotency_key: bytes
    created_at: datetime
    mode_version: int = 1
    content_revision: int = 0
    turn_id: UUID | None = None
    model_role: str | None = "main_ai"
    generation_no: int = 1
    account_control_version: int = 1
    copilot_draft_id: UUID | None = None
    approved_draft_revision_id: UUID | None = None
    logical_content_sha256: bytes | None = None
    normalizer_version: str = "normalized-text-v1"
    splitter_version: str = "telegram-text-v1"
    max_delivery_chunks: int = 16
    send_authorized_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AttemptCompletionRecord:
    outcome: str
    finished_at: datetime
    telegram_message_id: int | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None
    next_attempt_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadHighWatermarkRecord:
    operation_id: UUID
    account_id: UUID
    conversation_id: UUID
    max_telegram_message_id: int
    idempotency_key: bytes
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class TypingLeaseRecord:
    account_id: UUID
    conversation_id: UUID
    lease_token: UUID | None
    lease_expires_at: datetime | None
    updated_at: datetime
