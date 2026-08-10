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
