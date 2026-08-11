"""COPILOT draft state, token, and revision contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest


class DraftState(StrEnum):
    REQUESTED = "requested"
    COLLECTING = "collecting"
    GENERATING = "generating"
    READY = "ready"
    EDITING = "editing"
    APPROVED = "approved"
    SEND_QUEUED = "send_queued"
    SEND_UNKNOWN = "send_unknown"
    SENT = "sent"
    IGNORED = "ignored"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    FAILED = "failed"


ACTIVE_DRAFT_STATES = frozenset(
    {
        DraftState.REQUESTED,
        DraftState.COLLECTING,
        DraftState.GENERATING,
        DraftState.READY,
        DraftState.EDITING,
        DraftState.APPROVED,
        DraftState.SEND_QUEUED,
        DraftState.SEND_UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class DraftActionToken:
    token_sha256: bytes
    admin_telegram_user_id: int
    bot_chat_id: int
    purpose: str
    revision_no: int
    expires_at: datetime
    used_at: datetime | None = None

    def accepts(  # noqa: PLR0913 - every callback binding is security-relevant
        self,
        *,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        purpose: str,
        revision_no: int,
        now: datetime,
    ) -> bool:
        return (
            self.used_at is None
            and now < self.expires_at
            and self.admin_telegram_user_id == admin_telegram_user_id
            and self.bot_chat_id == bot_chat_id
            and self.purpose == purpose
            and self.revision_no == revision_no
            and compare_digest(self.token_sha256, sha256(raw_token.encode()).digest())
        )


def validate_draft_transition(current: DraftState, target: DraftState) -> None:
    allowed = {
        DraftState.REQUESTED: {DraftState.COLLECTING},
        DraftState.COLLECTING: {DraftState.GENERATING},
        DraftState.GENERATING: {DraftState.READY},
        DraftState.READY: {DraftState.EDITING, DraftState.APPROVED},
        DraftState.EDITING: {DraftState.READY},
        DraftState.APPROVED: {DraftState.SEND_QUEUED},
        DraftState.SEND_QUEUED: {DraftState.SEND_UNKNOWN, DraftState.SENT},
        DraftState.SEND_UNKNOWN: {DraftState.SENT},
    }
    terminal = {
        DraftState.IGNORED,
        DraftState.EXPIRED,
        DraftState.INVALIDATED,
        DraftState.FAILED,
    }
    if target in terminal and current in ACTIVE_DRAFT_STATES:
        return
    if target not in allowed.get(current, set()):
        raise ValueError(f"invalid draft transition: {current}->{target}")
