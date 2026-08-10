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
