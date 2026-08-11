"""Translate adapter-owned Telegram data into canonical, replayable events."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from telegram_userbot.domain.messaging import (
    BodyKind,
    Direction,
    EventKind,
    MediaDescriptor,
    MediaKind,
    MessageBody,
    NormalizedTelegramEvent,
    PeerKind,
    ReactionDescriptor,
)


@dataclass(frozen=True, slots=True)
class PeerAdmission:
    account_id: UUID
    conversation_id: UUID | None
    peer_kind: PeerKind
    telegram_chat_id: int | None


@dataclass(frozen=True, slots=True)
class RawMedia:
    kind: MediaKind
    file_reference: str | None = None
    mime_type: str | None = None
    size: int | None = None
    duration_ms: int | None = None
    file_name: str | None = None
    width: int | None = None
    height: int | None = None
    telegram_document_id: int | None = None


@dataclass(frozen=True, slots=True)
class RawTelegramUpdate:
    update_identity: str
    kind: EventKind
    observed_at: datetime
    telegram_event_at: datetime | None = None
    telegram_message_id: int | None = None
    grouped_id: int | None = None
    reply_to_telegram_message_id: int | None = None
    direction: Direction | None = None
    sender_telegram_peer_id: int | None = None
    outbound_random_id: int | None = None
    text: str | None = None
    is_caption: bool = False
    entities: tuple[dict[str, Any], ...] = ()
    media: tuple[RawMedia, ...] = ()
    reaction_key: str | None = None
    reaction_active: bool = True
    reaction_actor_telegram_peer_id: int | None = None
    service_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _safe_file_name(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.replace("\\", "/").rsplit("/", maxsplit=1)[-1][:255] or None


def _media(raw: RawMedia, position: int) -> MediaDescriptor:
    metadata: dict[str, Any] = {
        "download_eligible": raw.kind.image_download_eligible,
        "binary_persisted": False,
    }
    if raw.telegram_document_id is not None:
        metadata["telegram_document_id"] = raw.telegram_document_id
    if raw.width is not None:
        metadata["width"] = raw.width
    if raw.height is not None:
        metadata["height"] = raw.height
    return MediaDescriptor(
        kind=raw.kind,
        position=position,
        telegram_file_ref=raw.file_reference,
        declared_mime=raw.mime_type,
        declared_size=raw.size,
        duration_ms=raw.duration_ms,
        original_name_sanitized=_safe_file_name(raw.file_name),
        width=raw.width,
        height=raw.height,
        metadata=metadata,
    )


def normalize_update(
    *, event_uuid: UUID, admission: PeerAdmission, raw: RawTelegramUpdate
) -> NormalizedTelegramEvent:
    """Normalize one update; unsupported peers are deliberately content-free."""

    supported = admission.peer_kind.supported and admission.conversation_id is not None
    body: MessageBody | None = None
    normalized_media: tuple[MediaDescriptor, ...] = ()
    if supported and raw.kind in {EventKind.MESSAGE_CREATED, EventKind.MESSAGE_EDITED}:
        if raw.text is None:
            body = MessageBody(BodyKind.NONE)
        else:
            body = MessageBody(
                BodyKind.CAPTION if raw.is_caption else BodyKind.TEXT,
                raw.text,
                raw.entities,
            )
        normalized_media = tuple(_media(item, index) for index, item in enumerate(raw.media))

    reaction = None
    if supported and raw.kind is EventKind.REACTION_CHANGED:
        if not raw.reaction_key:
            raise ValueError("reaction update requires a key")
        reaction = ReactionDescriptor(
            key=raw.reaction_key,
            active=raw.reaction_active,
            actor_telegram_peer_id=raw.reaction_actor_telegram_peer_id,
        )

    metadata: dict[str, Any] = {
        "peer_kind": admission.peer_kind,
        "supported_scope": supported,
    }
    if supported:
        metadata.update(raw.metadata)
    else:
        metadata["unsupported_reason"] = admission.peer_kind

    return NormalizedTelegramEvent(
        event_uuid=event_uuid,
        account_id=admission.account_id,
        conversation_id=admission.conversation_id if supported else None,
        event_kind=raw.kind,
        peer_kind=admission.peer_kind,
        update_identity=raw.update_identity,
        observed_at=raw.observed_at,
        telegram_event_at=raw.telegram_event_at,
        telegram_chat_id=admission.telegram_chat_id,
        telegram_message_id=raw.telegram_message_id,
        grouped_id=raw.grouped_id,
        reply_to_telegram_message_id=raw.reply_to_telegram_message_id,
        direction=raw.direction,
        sender_telegram_peer_id=raw.sender_telegram_peer_id,
        outbound_random_id=raw.outbound_random_id,
        body=body,
        media=normalized_media,
        reaction=reaction,
        service_kind=raw.service_kind,
        metadata=metadata,
    )
