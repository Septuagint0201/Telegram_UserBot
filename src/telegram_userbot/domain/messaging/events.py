"""Framework-neutral Telegram event facts used by the ingest boundary."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

FINGERPRINT_VERSION = 1


class EventKind(StrEnum):
    MESSAGE_CREATED = "message.created"
    MESSAGE_EDITED = "message.edited"
    MESSAGE_DELETED = "message.deleted"
    REACTION_CHANGED = "reaction.changed"
    SERVICE = "service"


class Direction(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class PeerKind(StrEnum):
    PRIVATE_USER = "private_user"
    BOT = "bot"
    SELF = "self"
    GROUP = "group"
    CHANNEL = "channel"
    SECRET_CHAT = "secret"  # noqa: S105 - Telegram peer classification, not a credential
    UNKNOWN = "unknown"

    @property
    def supported(self) -> bool:
        return self is PeerKind.PRIVATE_USER


class BodyKind(StrEnum):
    TEXT = "text"
    CAPTION = "caption"
    NONE = "none"


class MediaKind(StrEnum):
    PHOTO = "photo"
    IMAGE_DOCUMENT = "image_document"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    DOCUMENT = "document"
    STICKER = "sticker"

    @property
    def image_download_eligible(self) -> bool:
        return self in {MediaKind.PHOTO, MediaKind.IMAGE_DOCUMENT}


@dataclass(frozen=True, slots=True)
class MessageBody:
    kind: BodyKind
    text: str | None = None
    entities: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.kind is BodyKind.NONE and self.text is not None:
            raise ValueError("body kind none cannot contain text")
        if self.kind is not BodyKind.NONE and self.text is None:
            raise ValueError("text and caption bodies require text")

    @property
    def content_sha256(self) -> bytes | None:
        if self.text is None:
            return None
        envelope = {
            "kind": self.kind,
            "text": self.text,
            "entities": self.entities,
        }
        return sha256(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).digest()


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    kind: MediaKind
    position: int
    telegram_file_ref: str | None = None
    declared_mime: str | None = None
    declared_size: int | None = None
    duration_ms: int | None = None
    original_name_sanitized: str | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("media position must be nonnegative")
        if self.original_name_sanitized and any(
            separator in self.original_name_sanitized for separator in ("/", "\\")
        ):
            raise ValueError("media file name must not contain a path")


@dataclass(frozen=True, slots=True)
class ReactionDescriptor:
    key: str
    active: bool
    actor_telegram_peer_id: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedTelegramEvent:
    event_uuid: UUID
    account_id: UUID
    conversation_id: UUID | None
    event_kind: EventKind
    peer_kind: PeerKind
    update_identity: str
    observed_at: datetime
    telegram_event_at: datetime | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    grouped_id: int | None = None
    reply_to_telegram_message_id: int | None = None
    direction: Direction | None = None
    sender_telegram_peer_id: int | None = None
    outbound_random_id: int | None = None
    body: MessageBody | None = None
    media: tuple[MediaDescriptor, ...] = ()
    reaction: ReactionDescriptor | None = None
    service_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.update_identity:
            raise ValueError("update identity must be non-empty")
        if not self.peer_kind.supported and (self.body is not None or self.media):
            raise ValueError("unsupported peers must be normalized without body or media")
        if self.event_kind is EventKind.REACTION_CHANGED and self.reaction is None:
            raise ValueError("reaction event requires reaction metadata")
        if (
            self.event_kind
            in {
                EventKind.MESSAGE_CREATED,
                EventKind.MESSAGE_EDITED,
                EventKind.MESSAGE_DELETED,
                EventKind.REACTION_CHANGED,
            }
            and self.telegram_message_id is None
        ):
            raise ValueError("message event requires telegram message id")

    @property
    def update_fingerprint(self) -> bytes:
        payload = {
            "version": FINGERPRINT_VERSION,
            "account_id": str(self.account_id),
            "update_identity": self.update_identity,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()

    @property
    def ordering_key(self) -> str:
        timestamp = self.telegram_event_at or self.observed_at
        message_id = self.telegram_message_id if self.telegram_message_id is not None else -1
        return f"{timestamp.isoformat()}:{message_id:020d}:{self.update_identity}"

    @property
    def model_eligible(self) -> bool:
        return (
            self.peer_kind.supported
            and self.direction is Direction.INCOMING
            and self.event_kind in {EventKind.MESSAGE_CREATED, EventKind.MESSAGE_EDITED}
        )


def album_sort_key(event: NormalizedTelegramEvent) -> tuple[datetime, int, datetime, str]:
    """Stable album order, including deterministic tie breakers for replay."""

    return (
        event.telegram_event_at or event.observed_at,
        event.telegram_message_id or -1,
        event.observed_at,
        str(event.event_uuid),
    )
