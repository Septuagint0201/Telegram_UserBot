"""Typed, content-free durable runtime cursor contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from telegram_userbot.domain.shared.time import require_aware

_DEPLOYMENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_TELEGRAM_SCOPE = re.compile(r"(?:account|channel:[1-9][0-9]{0,18})\Z")
_UPDATE_IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_TELEGRAM_ID = 2**63 - 1


class ControlUpdateState(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"


class ControlUpdateDisposition(StrEnum):
    HANDLED = "handled"
    IGNORED = "ignored"
    REJECTED = "rejected"


class ControlUpdateSendState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    SENT = "sent"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


class ControlUpdateClaimOutcome(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    COMPLETED = "completed"
    BELOW_OFFSET = "below_offset"


def _deployment_id(value: str) -> None:
    if not isinstance(value, str) or _DEPLOYMENT_ID.fullmatch(value) is None:
        raise ValueError("runtime cursor deployment id is invalid")


def _bot_user_id(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_TELEGRAM_ID:
        raise ValueError("runtime cursor bot user id is invalid")


@dataclass(frozen=True, slots=True)
class ControlBotCursor:
    deployment_id: str
    bot_user_id: int
    next_offset: int
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _deployment_id(self.deployment_id)
        _bot_user_id(self.bot_user_id)
        if type(self.next_offset) is not int or self.next_offset < 0 or self.version <= 0:
            raise ValueError("control bot cursor values are invalid")
        object.__setattr__(self, "updated_at", require_aware(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class ControlUpdateReceipt:
    deployment_id: str
    bot_user_id: int
    update_id: int
    state: ControlUpdateState
    disposition: ControlUpdateDisposition | None
    send_state: ControlUpdateSendState
    owner_instance_id: UUID
    claimed_at: datetime
    lease_expires_at: datetime
    completed_at: datetime | None
    attempt_count: int
    version: int

    def __post_init__(self) -> None:
        _deployment_id(self.deployment_id)
        _bot_user_id(self.bot_user_id)
        if type(self.update_id) is not int or self.update_id < 0:
            raise ValueError("control update id is invalid")
        if not isinstance(self.state, ControlUpdateState):
            raise TypeError("control update state must use the stable vocabulary")
        if self.disposition is not None and not isinstance(
            self.disposition, ControlUpdateDisposition
        ):
            raise TypeError("control update disposition must use the stable vocabulary")
        if not isinstance(self.send_state, ControlUpdateSendState):
            raise TypeError("control update send state must use the stable vocabulary")
        if not isinstance(self.owner_instance_id, UUID) or self.owner_instance_id.int == 0:
            raise ValueError("control update owner instance id is invalid")
        claimed_at = require_aware(self.claimed_at, "claimed_at")
        lease_expires_at = require_aware(self.lease_expires_at, "lease_expires_at")
        completed_at = (
            None if self.completed_at is None else require_aware(self.completed_at, "completed_at")
        )
        if claimed_at > lease_expires_at:
            raise ValueError("control update lease timestamps are out of order")
        if self.state is ControlUpdateState.CLAIMED:
            if (
                self.disposition is not None
                or completed_at is not None
                or self.send_state is not ControlUpdateSendState.NOT_REQUIRED
            ):
                raise ValueError("claimed control update has terminal fields")
        elif self.disposition is None or completed_at is None or completed_at < claimed_at:
            raise ValueError("completed control update is missing terminal fields")
        if self.attempt_count <= 0 or self.version <= 0:
            raise ValueError("control update versions must be positive")
        object.__setattr__(self, "claimed_at", claimed_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        object.__setattr__(self, "completed_at", completed_at)


@dataclass(frozen=True, slots=True)
class ControlUpdateClaim:
    outcome: ControlUpdateClaimOutcome
    receipt: ControlUpdateReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ControlUpdateClaimOutcome):
            raise TypeError("control update claim outcome must use the stable vocabulary")
        if self.outcome is ControlUpdateClaimOutcome.BELOW_OFFSET:
            if self.receipt is not None:
                raise ValueError("below-offset claim cannot expose a receipt")
        elif self.receipt is None:
            raise ValueError("control update claim requires a receipt")

    @property
    def acquired(self) -> bool:
        return self.outcome is ControlUpdateClaimOutcome.ACQUIRED


@dataclass(frozen=True, slots=True)
class TelegramIngestWatermark:
    account_id: UUID
    scope: str
    pts: int
    pts_count: int
    update_identity: str
    durable_ingested_at: datetime
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, UUID) or self.account_id.int == 0:
            raise ValueError("Telegram ingest account id is invalid")
        if not isinstance(self.scope, str) or _TELEGRAM_SCOPE.fullmatch(self.scope) is None:
            raise ValueError("Telegram ingest scope is invalid")
        if type(self.pts) is not int or self.pts < 0:
            raise ValueError("Telegram ingest pts is invalid")
        if type(self.pts_count) is not int or self.pts_count < 0:
            raise ValueError("Telegram ingest pts count is invalid")
        if (
            not isinstance(self.update_identity, str)
            or _UPDATE_IDENTITY.fullmatch(self.update_identity) is None
        ):
            raise ValueError("Telegram ingest update identity is invalid")
        if self.version <= 0:
            raise ValueError("Telegram ingest watermark version must be positive")
        object.__setattr__(
            self,
            "durable_ingested_at",
            require_aware(self.durable_ingested_at, "durable_ingested_at"),
        )
        object.__setattr__(self, "updated_at", require_aware(self.updated_at, "updated_at"))


__all__ = [
    "ControlBotCursor",
    "ControlUpdateClaim",
    "ControlUpdateClaimOutcome",
    "ControlUpdateDisposition",
    "ControlUpdateReceipt",
    "ControlUpdateSendState",
    "ControlUpdateState",
    "TelegramIngestWatermark",
]
