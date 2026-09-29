"""Durable, content-free state for administrator-requested data exports."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from telegram_userbot.domain.shared.time import require_aware

EXPORT_FORMAT_VERSION = 1
_REQUESTED_BY = re.compile(r"^actor:hmac-sha256:[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class DataExportState(StrEnum):
    REQUESTED = "requested"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


def pseudonymous_export_actor(*, actor_identity: bytes, hmac_key: bytes) -> str:
    """Bind an operator identity without storing its Telegram ID or username."""

    if not actor_identity or len(actor_identity) > 256:
        raise ValueError("data export actor identity is invalid")
    if len(hmac_key) != 32:
        raise ValueError("data export actor HMAC key must be 32 bytes")
    digest = hmac.new(hmac_key, actor_identity, hashlib.sha256).hexdigest()
    return f"actor:hmac-sha256:{digest}"


@dataclass(frozen=True, slots=True)
class DataExportRequest:
    """One export request without artifact paths, content, or recipient material."""

    id: UUID
    account_id: UUID
    contact_id: UUID | None
    state: DataExportState
    requested_by: str
    format_version: int
    created_at: datetime
    expires_at: datetime
    owner_instance_id: UUID | None
    lease_expires_at: datetime | None
    completed_at: datetime | None
    artifact_sha256: bytes | None
    artifact_deleted_at: datetime | None
    last_error_code: str | None
    attempt_count: int
    version: int

    def __post_init__(self) -> None:  # noqa: PLR0912 - validates state shape invariants
        if not isinstance(self.id, UUID) or self.id.int == 0 or self.id.version != 7:
            raise ValueError("data export request id is invalid")
        if not isinstance(self.account_id, UUID) or self.account_id.int == 0:
            raise ValueError("data export account id is invalid")
        if self.contact_id is not None and (
            not isinstance(self.contact_id, UUID) or self.contact_id.int == 0
        ):
            raise ValueError("data export contact id is invalid")
        if not isinstance(self.state, DataExportState):
            raise TypeError("data export state must use the stable vocabulary")
        if _REQUESTED_BY.fullmatch(self.requested_by) is None:
            raise ValueError("data export requester reference is invalid")
        if self.format_version != EXPORT_FORMAT_VERSION:
            raise ValueError("data export format version is unsupported")
        created_at = require_aware(self.created_at, "created_at")
        expires_at = require_aware(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise ValueError("data export expiry must follow creation")
        lease_expires_at = (
            None
            if self.lease_expires_at is None
            else require_aware(self.lease_expires_at, "lease_expires_at")
        )
        completed_at = (
            None if self.completed_at is None else require_aware(self.completed_at, "completed_at")
        )
        artifact_deleted_at = (
            None
            if self.artifact_deleted_at is None
            else require_aware(self.artifact_deleted_at, "artifact_deleted_at")
        )
        if self.owner_instance_id is not None and (
            not isinstance(self.owner_instance_id, UUID) or self.owner_instance_id.int == 0
        ):
            raise ValueError("data export owner id is invalid")
        if self.artifact_sha256 is not None and len(self.artifact_sha256) != 32:
            raise ValueError("data export artifact digest must be SHA-256")
        if self.last_error_code is not None and _ERROR_CODE.fullmatch(self.last_error_code) is None:
            raise ValueError("data export error code is invalid")
        if self.attempt_count < 0 or self.version <= 0:
            raise ValueError("data export counters are invalid")

        if self.state is DataExportState.REQUESTED:
            valid_shape = (
                all(
                    value is None
                    for value in (
                        self.owner_instance_id,
                        lease_expires_at,
                        completed_at,
                        self.artifact_sha256,
                        artifact_deleted_at,
                        self.last_error_code,
                    )
                )
                and self.attempt_count == 0
            )
        elif self.state is DataExportState.CLAIMED:
            valid_shape = (
                self.owner_instance_id is not None
                and lease_expires_at is not None
                and lease_expires_at > created_at
                and completed_at is None
                and self.artifact_sha256 is None
                and artifact_deleted_at is None
                and self.last_error_code is None
                and self.attempt_count > 0
            )
        elif self.state is DataExportState.COMPLETED:
            valid_shape = (
                self.owner_instance_id is None
                and lease_expires_at is None
                and completed_at is not None
                and self.artifact_sha256 is not None
                and self.last_error_code is None
                and self.attempt_count > 0
                and (artifact_deleted_at is None or artifact_deleted_at >= completed_at)
            )
        else:
            valid_shape = (
                self.owner_instance_id is None
                and lease_expires_at is None
                and completed_at is not None
                and self.artifact_sha256 is None
                and artifact_deleted_at is None
                and self.last_error_code is not None
            )
        if not valid_shape:
            raise ValueError("data export state fields are inconsistent")

        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "artifact_deleted_at", artifact_deleted_at)


__all__ = [
    "EXPORT_FORMAT_VERSION",
    "DataExportRequest",
    "DataExportState",
    "pseudonymous_export_actor",
]
