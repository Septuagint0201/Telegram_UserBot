"""Outbound delivery identities and state vocabulary."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from uuid import UUID


class DeliveryGroupState(StrEnum):
    PLANNED = "planned"
    SENDING = "sending"
    PARTIAL = "partial"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class OutboundIntentState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FLOOD_WAIT = "flood_wait"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OutboundChunk:
    intent_id: UUID
    sequence_no: int
    telegram_random_id: int
    text: str
    payload_sha256: bytes


def payload_sha256(text: str) -> bytes:
    return sha256(text.encode("utf-8")).digest()


def stable_telegram_random_id(intent_id: UUID, entropy: bytes) -> int:
    """Return a stable, positive, non-zero signed 64-bit Telegram random ID."""

    if len(entropy) < 16:
        raise ValueError("at least 128 bits of entropy are required")
    raw = sha256(b"telegram-random-id-v1\0" + intent_id.bytes + entropy).digest()
    value = int.from_bytes(raw[:8], "big") & ((1 << 63) - 1)
    return value or 1
