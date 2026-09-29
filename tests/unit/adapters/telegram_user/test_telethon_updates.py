from datetime import UTC, datetime
from uuid import UUID

import pytest
from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import PeerAdmission, normalize_update
from telegram_userbot.adapters.telegram_user.telethon_updates import (
    TelegramUpdateWatermark,
    convert_telethon_update,
)
from telegram_userbot.domain.messaging import Direction, EventKind, MediaKind, PeerKind

NOW = datetime(2026, 8, 24, 3, 4, 5, tzinfo=UTC)


def _document(*, mime: str, attributes: list[object], document_id: int = 800) -> types.Document:
    return types.Document(
        id=document_id,
        access_hash=900,
        file_reference=b"opaque-ref",
        date=NOW,
        mime_type=mime,
        size=4096,
        dc_id=2,
        attributes=attributes,
    )


@pytest.mark.unit
def test_convert_private_album_photo_and_image_document_without_leaking_tl_types() -> None:
    photo = types.Photo(
        id=700,
        access_hash=701,
        file_reference=b"photo-ref",
        date=NOW,
        sizes=[types.PhotoSize("x", 640, 480, 2048)],
        dc_id=2,
    )
    message = types.Message(
        id=10,
        peer_id=types.PeerUser(42),
        date=NOW,
        message="caption",
        out=False,
        from_id=types.PeerUser(42),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=9),
        media=types.MessageMediaPhoto(photo=photo),
        entities=[types.MessageEntityBold(offset=0, length=7)],
        grouped_id=555,
    )

    (candidate,) = convert_telethon_update(
        types.UpdateNewMessage(message, pts=100, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )

    assert candidate.scope.telegram_chat_id == 42
    assert candidate.scope.peer_kind_hint is PeerKind.PRIVATE_USER
    assert candidate.raw.kind is EventKind.MESSAGE_CREATED
    assert candidate.raw.direction is Direction.INCOMING
    assert candidate.raw.grouped_id == 555
    assert candidate.raw.reply_to_telegram_message_id == 9
    assert candidate.raw.is_caption
    assert candidate.raw.entities == ({"kind": "bold", "offset": 0, "length": 7},)
    assert candidate.raw.media[0].kind is MediaKind.PHOTO
    assert candidate.raw.media[0].telegram_media_id == 700
    assert candidate.raw.media[0].width == 640
    assert candidate.raw.media[0].file_reference is not None
    assert candidate.raw.media[0].file_reference.startswith("v2:photo:700:701:2:x:")
    assert candidate.watermark is not None
    assert candidate.watermark.pts == 100

    normalized = normalize_update(
        event_uuid=UUID(int=3),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=candidate.raw,
    )
    assert normalized.media[0].metadata["download_eligible"] is True
    assert normalized.media[0].metadata["telegram_media_id"] == 700
    assert all(not type(value).__module__.startswith("telethon") for value in candidate.raw.media)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attributes", "mime", "expected_kind", "download_eligible"),
    [
        (
            [types.DocumentAttributeImageSize(w=800, h=600)],
            "image/png",
            MediaKind.IMAGE_DOCUMENT,
            True,
        ),
        (
            [types.DocumentAttributeAudio(duration=5, voice=True)],
            "audio/ogg",
            MediaKind.VOICE,
            False,
        ),
        (
            [types.DocumentAttributeVideo(duration=4.5, w=640, h=480)],
            "video/mp4",
            MediaKind.VIDEO,
            False,
        ),
        (
            [types.DocumentAttributeVideo(duration=3, w=240, h=240, round_message=True)],
            "video/mp4",
            MediaKind.VIDEO_NOTE,
            False,
        ),
    ],
)
def test_convert_document_media_download_policy(
    attributes: list[object],
    mime: str,
    expected_kind: MediaKind,
    download_eligible: bool,
) -> None:
    document = _document(mime=mime, attributes=attributes)
    message = types.Message(
        id=11,
        peer_id=types.PeerUser(42),
        date=NOW,
        message=None,
        out=False,
        from_id=types.PeerUser(42),
        media=types.MessageMediaDocument(document=document),
    )

    (candidate,) = convert_telethon_update(
        types.UpdateNewMessage(message, pts=101, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )
    normalized = normalize_update(
        event_uuid=UUID(int=4),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=candidate.raw,
    )

    assert candidate.raw.media[0].kind is expected_kind
    assert candidate.raw.media[0].telegram_document_id == 800
    assert normalized.media[0].metadata["download_eligible"] is download_eligible
    assert normalized.media[0].metadata["binary_persisted"] is False


@pytest.mark.unit
@pytest.mark.parametrize("size_type", ["", "xx", "/", None, 1])
def test_convert_photo_rejects_noncanonical_download_size_type(size_type: object) -> None:
    size = types.PhotoSize("x", 640, 480, 2048)
    size.type = size_type
    photo = types.Photo(
        id=700,
        access_hash=701,
        file_reference=b"photo-ref",
        date=NOW,
        sizes=[size],
        dc_id=2,
    )
    message = types.Message(
        id=10,
        peer_id=types.PeerUser(42),
        date=NOW,
        message="caption",
        out=False,
        from_id=types.PeerUser(42),
        media=types.MessageMediaPhoto(photo=photo),
    )

    (candidate,) = convert_telethon_update(
        types.UpdateNewMessage(message, pts=100, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )

    assert candidate.raw.media == ()


@pytest.mark.unit
def test_convert_edit_delete_reaction_and_service_metadata() -> None:
    edited = types.Message(
        id=20,
        peer_id=types.PeerUser(42),
        date=NOW,
        message="edited",
        out=True,
        from_id=types.PeerUser(1000),
        edit_date=NOW,
    )
    (edit_candidate,) = convert_telethon_update(
        types.UpdateEditMessage(edited, pts=200, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )
    assert edit_candidate.raw.kind is EventKind.MESSAGE_EDITED
    assert edit_candidate.raw.direction is Direction.OUTGOING
    assert edit_candidate.raw.metadata["edit_date"] == NOW.isoformat()

    deleted = convert_telethon_update(
        types.UpdateDeleteMessages([20, 21], pts=201, pts_count=2),
        managed_user_id=1000,
        observed_at=NOW,
    )
    assert [item.scope.telegram_chat_id for item in deleted] == [None, None]
    assert [item.raw.telegram_message_id for item in deleted] == [20, 21]
    assert all(item.raw.kind is EventKind.MESSAGE_DELETED for item in deleted)

    reactions = types.MessageReactions(
        results=[
            types.ReactionCount(types.ReactionEmoji("👍"), count=3, chosen_order=0),
            types.ReactionCount(types.ReactionCustomEmoji(900), count=1),
        ]
    )
    reaction_candidates = convert_telethon_update(
        types.UpdateMessageReactions(types.PeerUser(42), 20, reactions),
        managed_user_id=1000,
        observed_at=NOW,
    )
    assert {item.raw.reaction_key for item in reaction_candidates} == {
        "emoji:👍",
        "custom:900",
    }
    assert {item.raw.metadata["count"] for item in reaction_candidates} == {1, 3}

    service_message = types.MessageService(
        id=22,
        peer_id=types.PeerUser(42),
        date=NOW,
        out=False,
        from_id=types.PeerUser(42),
        action=types.MessageActionPhoneCall(call_id=1234, duration=7),
        message="must not cross the service boundary",
    )
    (service_candidate,) = convert_telethon_update(
        types.UpdateNewMessage(service_message, pts=202, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )
    assert service_candidate.raw.kind is EventKind.SERVICE
    assert service_candidate.raw.text is None
    assert service_candidate.raw.service_kind == "phone_call"
    assert service_candidate.raw.metadata == {"call_id": 1234, "duration": 7}


@pytest.mark.unit
def test_short_and_unsupported_updates_are_safe_and_deterministic() -> None:
    update = types.UpdateShortMessage(
        id=30,
        user_id=42,
        message="hello",
        pts=300,
        pts_count=1,
        date=NOW,
        out=False,
    )
    first = convert_telethon_update(update, managed_user_id=1000, observed_at=NOW)
    second = convert_telethon_update(
        types.UpdateShort(update=update, date=NOW),
        managed_user_id=1000,
        observed_at=NOW,
    )
    assert first == second
    assert first[0].raw.update_identity == "UpdateShortMessage:42:30:300"
    assert (
        convert_telethon_update(
            types.UpdateUserStatus(user_id=42, status=types.UserStatusOnline(expires=NOW)),
            managed_user_id=1000,
            observed_at=NOW,
        )
        == ()
    )


@pytest.mark.unit
def test_group_short_message_is_marked_unsupported_before_normalization() -> None:
    (candidate,) = convert_telethon_update(
        types.UpdateShortChatMessage(
            id=40,
            from_id=42,
            chat_id=99,
            message="must be stripped",
            pts=400,
            pts_count=1,
            date=NOW,
        ),
        managed_user_id=1000,
        observed_at=NOW,
    )
    normalized = normalize_update(
        event_uuid=UUID(int=5),
        admission=PeerAdmission(
            UUID(int=1), None, PeerKind.GROUP, candidate.scope.telegram_chat_id
        ),
        raw=candidate.raw,
    )

    assert candidate.scope.peer_kind_hint is PeerKind.GROUP
    assert normalized.body is None
    assert normalized.media == ()
    assert normalized.metadata["supported_scope"] is False


@pytest.mark.unit
def test_channel_watermark_uses_positive_raw_channel_scope() -> None:
    channel_message = types.Message(
        id=41,
        peer_id=types.PeerChannel(77),
        date=NOW,
        message="must be stripped",
        out=False,
        from_id=types.PeerUser(42),
    )

    (candidate,) = convert_telethon_update(
        types.UpdateNewChannelMessage(channel_message, pts=401, pts_count=1),
        managed_user_id=1000,
        observed_at=NOW,
    )

    assert candidate.watermark is not None
    assert candidate.watermark.scope == "channel:77"


@pytest.mark.unit
@pytest.mark.parametrize("scope", ["channel:-77", "channel:0", "channel:077", "private:42"])
def test_watermark_rejects_noncanonical_scope(scope: str) -> None:
    with pytest.raises(ValueError, match="scope"):
        TelegramUpdateWatermark(scope, 1, 1, "UpdateNewMessage:42:1:1")
