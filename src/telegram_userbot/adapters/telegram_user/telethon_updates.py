"""Convert TL updates to metadata; later composition owns eligible image download."""

import json
import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from telethon import types, utils  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import RawMedia, RawTelegramUpdate
from telegram_userbot.domain.messaging import Direction, EventKind, MediaKind, PeerKind


@dataclass(frozen=True, slots=True)
class TelethonUpdateScope:
    """Primitive lookup key; no Telethon entity crosses the adapter boundary."""

    telegram_chat_id: int | None
    sender_telegram_peer_id: int | None
    telegram_message_id: int
    peer_kind_hint: PeerKind


@dataclass(frozen=True, slots=True)
class TelegramUpdateWatermark:
    """Observable Telegram ordering cursor saved only after durable ingest."""

    scope: str
    pts: int
    pts_count: int
    update_identity: str

    def __post_init__(self) -> None:
        valid_scope = self.scope == "account" or (
            self.scope.startswith("channel:")
            and self.scope.removeprefix("channel:").isdigit()
            and not self.scope.removeprefix("channel:").startswith("0")
            and 0 < int(self.scope.removeprefix("channel:")) <= (1 << 63) - 1
        )
        if not valid_scope:
            raise ValueError("Telegram update watermark scope is invalid")
        if (
            type(self.pts) is not int
            or self.pts < 0
            or type(self.pts_count) is not int
            or self.pts_count < 0
        ):
            raise ValueError("Telegram update watermark values are invalid")
        if (
            not isinstance(self.update_identity, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", self.update_identity) is None
        ):
            raise ValueError("Telegram update watermark identity is invalid")


@dataclass(frozen=True, slots=True)
class TelethonUpdateCandidate:
    scope: TelethonUpdateScope
    raw: RawTelegramUpdate
    watermark: TelegramUpdateWatermark | None


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    result = fallback if value is None else value
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _marked_peer_id(peer: object | None) -> int | None:
    if peer is None:
        return None
    if not isinstance(peer, (types.PeerUser, types.PeerChat, types.PeerChannel)):
        return None
    value = cast(int, utils.get_peer_id(peer))
    return value if value != 0 else None


def _peer_hint(peer: object | None, *, managed_user_id: int) -> PeerKind:
    if isinstance(peer, types.PeerUser):
        return PeerKind.SELF if peer.user_id == managed_user_id else PeerKind.PRIVATE_USER
    if isinstance(peer, types.PeerChat):
        return PeerKind.GROUP
    if isinstance(peer, types.PeerChannel):
        return PeerKind.CHANNEL
    return PeerKind.UNKNOWN


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _duration_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value * 1000)


def _snake_type(value: object, prefix: str) -> str:
    name = type(value).__name__
    name = name.removeprefix(prefix)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() or "unknown"


def _entities(values: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, list):
        return ()
    result: list[dict[str, Any]] = []
    for value in values:
        offset = _nonnegative_int(getattr(value, "offset", None))
        length = _nonnegative_int(getattr(value, "length", None))
        if offset is None or length is None:
            continue
        item: dict[str, Any] = {
            "kind": _snake_type(value, "MessageEntity"),
            "offset": offset,
            "length": length,
        }
        url = getattr(value, "url", None)
        if isinstance(url, str):
            item["url"] = url[:2048]
        language = getattr(value, "language", None)
        if isinstance(language, str):
            item["language"] = language[:64]
        user_id = _positive_int(getattr(value, "user_id", None))
        if user_id is not None:
            item["user_id"] = user_id
        document_id = _positive_int(getattr(value, "document_id", None))
        if document_id is not None:
            item["document_id"] = document_id
        result.append(item)
    return tuple(result)


_PHOTO_SIZE_TYPE = re.compile(r"[A-Za-z0-9]\Z")


def _opaque_file_reference(  # noqa: PLR0913 - exact Telegram binding fields
    *,
    kind: str,
    media_id: int,
    access_hash: int,
    dc_id: int,
    file_reference: bytes,
    photo_size_type: str | None = None,
) -> str:
    encoded = urlsafe_b64encode(file_reference).decode("ascii").rstrip("=")
    if kind == "photo":
        if (
            not isinstance(photo_size_type, str)
            or _PHOTO_SIZE_TYPE.fullmatch(photo_size_type) is None
        ):
            raise ValueError("photo size type is invalid")
        return f"v2:photo:{media_id}:{access_hash}:{dc_id}:{photo_size_type}:{encoded}"
    if kind != "document" or photo_size_type is not None:
        raise ValueError("opaque media kind is invalid")
    return f"v1:{kind}:{media_id}:{access_hash}:{dc_id}:{encoded}"


def _largest_dimensions(values: object) -> tuple[int | None, int | None]:
    if not isinstance(values, list):
        return None, None
    dimensions = [
        (width, height)
        for value in values
        if (width := _positive_int(getattr(value, "w", None))) is not None
        if (height := _positive_int(getattr(value, "h", None))) is not None
    ]
    if not dimensions:
        return None, None
    return max(dimensions, key=lambda item: item[0] * item[1])


def _photo_media(value: object) -> RawMedia | None:
    if not isinstance(value, types.Photo):
        return None
    media_id = _positive_int(value.id)
    dc_id = _positive_int(value.dc_id)
    access_hash = _integer(value.access_hash)
    if (
        media_id is None
        or dc_id is None
        or access_hash is None
        or not isinstance(value.file_reference, bytes)
        or not isinstance(value.sizes, list)
        or not value.sizes
    ):
        return None
    photo_size_type = getattr(value.sizes[-1], "type", None)
    if not isinstance(photo_size_type, str) or _PHOTO_SIZE_TYPE.fullmatch(photo_size_type) is None:
        return None
    width, height = _largest_dimensions(value.sizes)
    return RawMedia(
        MediaKind.PHOTO,
        file_reference=_opaque_file_reference(
            kind="photo",
            media_id=media_id,
            access_hash=access_hash,
            dc_id=dc_id,
            file_reference=value.file_reference,
            photo_size_type=photo_size_type,
        ),
        mime_type="image/jpeg",
        width=width,
        height=height,
        telegram_media_id=media_id,
    )


def _document_media(  # noqa: PLR0912 - TL document attributes are mutually explicit
    value: object,
) -> RawMedia | None:
    if not isinstance(value, types.Document):
        return None
    media_id = _positive_int(value.id)
    dc_id = _positive_int(value.dc_id)
    size = _nonnegative_int(value.size)
    access_hash = _integer(value.access_hash)
    if (
        media_id is None
        or dc_id is None
        or size is None
        or access_hash is None
        or not isinstance(value.mime_type, str)
        or not isinstance(value.file_reference, bytes)
    ):
        return None

    kind = MediaKind.DOCUMENT
    duration_ms: int | None = None
    file_name: str | None = None
    width: int | None = None
    height: int | None = None
    is_sticker = False
    is_voice = False
    is_audio = False
    is_video = False
    is_video_note = False
    for attribute in value.attributes:
        if isinstance(attribute, types.DocumentAttributeFilename):
            file_name = attribute.file_name
        elif isinstance(attribute, types.DocumentAttributeImageSize):
            width, height = _positive_int(attribute.w), _positive_int(attribute.h)
        elif isinstance(attribute, types.DocumentAttributeAudio):
            duration_ms = _duration_ms(attribute.duration)
            is_voice = bool(attribute.voice)
            is_audio = True
        elif isinstance(attribute, types.DocumentAttributeVideo):
            duration_ms = _duration_ms(attribute.duration)
            width, height = _positive_int(attribute.w), _positive_int(attribute.h)
            is_video_note = bool(attribute.round_message)
            is_video = True
        elif isinstance(attribute, types.DocumentAttributeSticker):
            is_sticker = True

    if is_sticker:
        kind = MediaKind.STICKER
    elif is_voice:
        kind = MediaKind.VOICE
    elif is_video_note:
        kind = MediaKind.VIDEO_NOTE
    elif is_video:
        kind = MediaKind.VIDEO
    elif is_audio:
        kind = MediaKind.AUDIO
    elif value.mime_type.lower().startswith("image/"):
        kind = MediaKind.IMAGE_DOCUMENT

    return RawMedia(
        kind,
        file_reference=_opaque_file_reference(
            kind="document",
            media_id=media_id,
            access_hash=access_hash,
            dc_id=dc_id,
            file_reference=value.file_reference,
        ),
        mime_type=value.mime_type.lower(),
        size=size,
        duration_ms=duration_ms,
        file_name=file_name,
        width=width,
        height=height,
        telegram_document_id=media_id,
        telegram_media_id=media_id,
    )


def _message_media(value: object | None) -> tuple[RawMedia, ...]:
    media: RawMedia | None = None
    if isinstance(value, types.MessageMediaPhoto):
        media = _photo_media(value.photo)
    elif isinstance(value, types.MessageMediaDocument):
        media = _document_media(value.document)
    return () if media is None else (media,)


def _reply_to_message_id(value: object | None) -> int | None:
    if not isinstance(value, types.MessageReplyHeader):
        return None
    return _positive_int(value.reply_to_msg_id)


def _message_metadata(message: object) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    forward = getattr(message, "fwd_from", None)
    if forward is not None:
        metadata["forwarded"] = True
        peer_id = _marked_peer_id(getattr(forward, "from_id", None))
        if peer_id is not None:
            metadata["forward_from_peer_id"] = peer_id
        forward_date = getattr(forward, "date", None)
        if isinstance(forward_date, datetime):
            metadata["forward_date"] = _aware(forward_date, forward_date).isoformat()
    via_bot_id = _positive_int(getattr(message, "via_bot_id", None))
    if via_bot_id is not None:
        metadata["via_bot_id"] = via_bot_id
    edit_date = getattr(message, "edit_date", None)
    if isinstance(edit_date, datetime):
        metadata["edit_date"] = _aware(edit_date, edit_date).isoformat()
    return metadata


_SERVICE_INTEGER_FIELDS = (
    "user_id",
    "inviter_id",
    "channel_id",
    "chat_id",
    "call_id",
    "duration",
    "period",
    "distance",
    "boost_count",
)


def _service_metadata(action: object) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in _SERVICE_INTEGER_FIELDS:
        value = _nonnegative_int(getattr(action, field, None))
        if value is not None:
            metadata[field] = value
    users = getattr(action, "users", None)
    if isinstance(users, list):
        metadata["user_ids"] = [
            item for value in users if (item := _positive_int(value)) is not None
        ][:100]
    return metadata


def _identity(update_name: str, chat_id: int | None, message_id: int, pts: int | None) -> str:
    return f"{update_name}:{chat_id if chat_id is not None else 'unknown'}:{message_id}:{pts or 0}"


def _watermark(
    update: object, *, chat_id: int | None, identity: str
) -> TelegramUpdateWatermark | None:
    pts = _nonnegative_int(getattr(update, "pts", None))
    pts_count = _nonnegative_int(getattr(update, "pts_count", None))
    if pts is None or pts_count is None:
        return None
    # Telethon's marked channel IDs are ``-(1_000_000_000_000 + channel_id)``.
    # The durable cursor contract deliberately stores the positive raw channel ID,
    # never an adapter-specific marked peer representation.
    scope = (
        "account"
        if chat_id is None or chat_id > -1_000_000_000_000
        else f"channel:{-chat_id - 1_000_000_000_000}"
    )
    return TelegramUpdateWatermark(scope, pts, pts_count, identity)


def _message_candidate(
    *,
    update: object,
    message: object,
    kind: EventKind,
    managed_user_id: int,
    observed_at: datetime,
) -> TelethonUpdateCandidate | None:
    if not isinstance(message, (types.Message, types.MessageService)):
        return None
    message_id = _positive_int(message.id)
    if message_id is None:
        return None
    chat_id = _marked_peer_id(message.peer_id)
    hint = _peer_hint(message.peer_id, managed_user_id=managed_user_id)
    sender_id = _marked_peer_id(message.from_id)
    if sender_id is None:
        sender_id = managed_user_id if bool(message.out) else chat_id
    pts = _nonnegative_int(getattr(update, "pts", None))
    identity = _identity(type(update).__name__, chat_id, message_id, pts)
    event_kind = EventKind.SERVICE if isinstance(message, types.MessageService) else kind
    media = () if event_kind is EventKind.SERVICE else _message_media(message.media)
    text = None if event_kind is EventKind.SERVICE else message.message
    metadata = (
        _service_metadata(message.action)
        if event_kind is EventKind.SERVICE
        else _message_metadata(message)
    )
    raw = RawTelegramUpdate(
        update_identity=identity,
        kind=event_kind,
        observed_at=observed_at,
        telegram_event_at=_aware(message.date, observed_at),
        telegram_message_id=message_id,
        grouped_id=_positive_int(getattr(message, "grouped_id", None)),
        reply_to_telegram_message_id=_reply_to_message_id(message.reply_to),
        direction=Direction.OUTGOING if bool(message.out) else Direction.INCOMING,
        sender_telegram_peer_id=sender_id,
        text=text,
        is_caption=bool(media and text),
        entities=_entities(message.entities),
        media=media,
        service_kind=(
            _snake_type(message.action, "MessageAction")
            if event_kind is EventKind.SERVICE
            else None
        ),
        metadata=metadata,
    )
    return TelethonUpdateCandidate(
        TelethonUpdateScope(chat_id, sender_id, message_id, hint),
        raw,
        _watermark(update, chat_id=chat_id, identity=identity),
    )


def _short_message(
    update: object, *, managed_user_id: int, observed_at: datetime
) -> TelethonUpdateCandidate | None:
    if isinstance(update, types.UpdateShortMessage):
        peer = types.PeerUser(update.user_id)
        sender = types.PeerUser(managed_user_id if update.out else update.user_id)
    elif isinstance(update, types.UpdateShortChatMessage):
        peer = types.PeerChat(update.chat_id)
        sender = types.PeerUser(managed_user_id if update.out else update.from_id)
    else:
        return None
    message = types.Message(
        id=update.id,
        peer_id=peer,
        date=update.date,
        message=update.message,
        out=update.out,
        from_id=sender,
        fwd_from=update.fwd_from,
        via_bot_id=update.via_bot_id,
        reply_to=update.reply_to,
        entities=update.entities,
    )
    return _message_candidate(
        update=update,
        message=message,
        kind=EventKind.MESSAGE_CREATED,
        managed_user_id=managed_user_id,
        observed_at=observed_at,
    )


def _delete_candidates(
    update: object, *, managed_user_id: int, observed_at: datetime
) -> tuple[TelethonUpdateCandidate, ...]:
    if isinstance(update, types.UpdateDeleteMessages):
        chat_id = None
        hint = PeerKind.UNKNOWN
        message_ids = update.messages
    elif isinstance(update, types.UpdateDeleteChannelMessages):
        peer = types.PeerChannel(update.channel_id)
        chat_id = _marked_peer_id(peer)
        hint = _peer_hint(peer, managed_user_id=managed_user_id)
        message_ids = update.messages
    else:
        return ()
    pts = _nonnegative_int(update.pts)
    result: list[TelethonUpdateCandidate] = []
    for value in message_ids:
        message_id = _positive_int(value)
        if message_id is None:
            continue
        identity = _identity(type(update).__name__, chat_id, message_id, pts)
        result.append(
            TelethonUpdateCandidate(
                TelethonUpdateScope(chat_id, None, message_id, hint),
                RawTelegramUpdate(
                    update_identity=identity,
                    kind=EventKind.MESSAGE_DELETED,
                    observed_at=observed_at,
                    telegram_message_id=message_id,
                ),
                _watermark(update, chat_id=chat_id, identity=identity),
            )
        )
    return tuple(result)


def _reaction_key(value: object) -> str | None:
    if isinstance(value, types.ReactionEmoji) and value.emoticon:
        return f"emoji:{value.emoticon}"
    if isinstance(value, types.ReactionCustomEmoji):
        document_id = _positive_int(value.document_id)
        return None if document_id is None else f"custom:{document_id}"
    if isinstance(value, types.ReactionPaid):
        return "paid"
    return None


def _reaction_candidates(
    update: object, *, managed_user_id: int, observed_at: datetime
) -> tuple[TelethonUpdateCandidate, ...]:
    if not isinstance(update, types.UpdateMessageReactions):
        return ()
    chat_id = _marked_peer_id(update.peer)
    hint = _peer_hint(update.peer, managed_user_id=managed_user_id)
    message_id = _positive_int(update.msg_id)
    if message_id is None:
        return ()
    facts = sorted(
        (
            key,
            count,
            item.chosen_order is not None,
        )
        for item in update.reactions.results
        if (key := _reaction_key(item.reaction)) is not None
        if (count := _nonnegative_int(item.count)) is not None
    )
    digest = sha256(
        json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    result: list[TelethonUpdateCandidate] = []
    for key, count, chosen in facts:
        identity = f"UpdateMessageReactions:{chat_id}:{message_id}:{digest}:{key}"
        result.append(
            TelethonUpdateCandidate(
                TelethonUpdateScope(chat_id, None, message_id, hint),
                RawTelegramUpdate(
                    update_identity=identity,
                    kind=EventKind.REACTION_CHANGED,
                    observed_at=observed_at,
                    telegram_message_id=message_id,
                    reaction_key=key,
                    reaction_active=count > 0,
                    metadata={"aggregate": True, "count": count, "chosen": chosen},
                ),
                None,
            )
        )
    if not facts:
        identity = f"UpdateMessageReactions:{chat_id}:{message_id}:{digest}:empty"
        result.append(
            TelethonUpdateCandidate(
                TelethonUpdateScope(chat_id, None, message_id, hint),
                RawTelegramUpdate(
                    update_identity=identity,
                    kind=EventKind.SERVICE,
                    observed_at=observed_at,
                    telegram_message_id=message_id,
                    service_kind="reaction_snapshot_empty",
                    metadata={"aggregate": True, "reaction_keys": []},
                ),
                None,
            )
        )
    return tuple(result)


def convert_telethon_update(
    update: object, *, managed_user_id: int, observed_at: datetime
) -> tuple[TelethonUpdateCandidate, ...]:
    """Convert supported raw TL updates; unknown update types are ignored."""

    if managed_user_id <= 0:
        raise ValueError("managed Telegram user ID must be positive")
    observed_at = _aware(observed_at, observed_at)
    if isinstance(update, types.UpdateShort):
        update = update.update
    if isinstance(update, (types.UpdateShortMessage, types.UpdateShortChatMessage)):
        candidate = _short_message(update, managed_user_id=managed_user_id, observed_at=observed_at)
        return () if candidate is None else (candidate,)
    if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
        candidate = _message_candidate(
            update=update,
            message=update.message,
            kind=EventKind.MESSAGE_CREATED,
            managed_user_id=managed_user_id,
            observed_at=observed_at,
        )
        return () if candidate is None else (candidate,)
    if isinstance(update, (types.UpdateEditMessage, types.UpdateEditChannelMessage)):
        candidate = _message_candidate(
            update=update,
            message=update.message,
            kind=EventKind.MESSAGE_EDITED,
            managed_user_id=managed_user_id,
            observed_at=observed_at,
        )
        return () if candidate is None else (candidate,)
    deleted = _delete_candidates(update, managed_user_id=managed_user_id, observed_at=observed_at)
    if deleted:
        return deleted
    return _reaction_candidates(update, managed_user_id=managed_user_id, observed_at=observed_at)


__all__ = [
    "TelegramUpdateWatermark",
    "TelethonUpdateCandidate",
    "TelethonUpdateScope",
    "convert_telethon_update",
]
