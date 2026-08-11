"""Detached records for the M4 conversation orchestrator boundary."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from telegram_userbot.domain.conversation import ModeResolution, WorkSnapshot


@dataclass(frozen=True, slots=True)
class TurnRecord:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    state: str
    trigger_kind: str
    generation_no: int
    snapshot: WorkSnapshot
    quiet_deadline_at: datetime | None
    hard_deadline_at: datetime | None
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    fencing_token: int


@dataclass(frozen=True, slots=True)
class ModelRunRecord:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    turn_id: UUID
    state: str
    generation_no: int
    profile_id: UUID
    config_version_id: UUID
    credential_version_id: UUID
    input_fingerprint: bytes
    started_at: datetime
    snapshot: WorkSnapshot
    trigger_kind: str


@dataclass(frozen=True, slots=True)
class GenerationClaim:
    run: ModelRunRecord
    resolution: ModeResolution
    max_telegram_message_id: int | None
    typing_lease_token: UUID | None


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: UUID
    state: str
    reason: str
    delivery_group_id: UUID | None = None
    draft_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DraftRecord:
    id: UUID
    account_id: UUID
    conversation_id: UUID
    turn_id: UUID
    model_run_id: UUID | None
    state: str
    current_revision_no: int | None
    expires_at: datetime | None
    snapshot: WorkSnapshot


@dataclass(frozen=True, slots=True)
class ControlResult:
    changed: bool
    result_code: str
    resolution: ModeResolution
    cancelled_work: bool
