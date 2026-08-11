from datetime import UTC, datetime
from uuid import UUID

import pytest

from telegram_userbot.domain.messaging import (
    BodyKind,
    Direction,
    EventKind,
    MediaDescriptor,
    MediaKind,
    MessageBody,
    NormalizedTelegramEvent,
    PeerKind,
    album_sort_key,
    stable_telegram_random_id,
)


def _event(message_id: int, event_uuid: int, *, at_second: int = 0) -> NormalizedTelegramEvent:
    at = datetime(2030, 1, 1, 0, 0, at_second, tzinfo=UTC)
    return NormalizedTelegramEvent(
        UUID(int=event_uuid),
        UUID(int=1),
        UUID(int=2),
        EventKind.MESSAGE_CREATED,
        PeerKind.PRIVATE_USER,
        f"update:{event_uuid}",
        at,
        telegram_event_at=at,
        telegram_message_id=message_id,
        direction=Direction.INCOMING,
        body=MessageBody(BodyKind.TEXT, "hello"),
    )


def test_event_fingerprint_and_album_order_are_stable() -> None:
    first = _event(8, 10)
    replay = _event(8, 10)
    earlier_message = _event(7, 11)
    assert first.update_fingerprint == replay.update_fingerprint
    assert [
        event.telegram_message_id for event in sorted((first, earlier_message), key=album_sort_key)
    ] == [
        7,
        8,
    ]


def test_unsupported_peer_rejects_body_and_media() -> None:
    with pytest.raises(ValueError, match="unsupported peers"):
        NormalizedTelegramEvent(
            UUID(int=1),
            UUID(int=2),
            None,
            EventKind.MESSAGE_CREATED,
            PeerKind.GROUP,
            "group:1",
            datetime(2030, 1, 1, tzinfo=UTC),
            telegram_message_id=1,
            body=MessageBody(BodyKind.TEXT, "must-not-persist"),
        )


def test_media_and_random_id_contracts() -> None:
    assert MediaKind.PHOTO.image_download_eligible
    assert not MediaKind.VIDEO.image_download_eligible
    media = MediaDescriptor(MediaKind.PHOTO, 0, original_name_sanitized="safe.png")
    assert media.position == 0
    first = stable_telegram_random_id(UUID(int=9), bytes(range(16)))
    assert first == stable_telegram_random_id(UUID(int=9), bytes(range(16)))
    assert 0 < first < 2**63
    with pytest.raises(ValueError, match="128 bits"):
        stable_telegram_random_id(UUID(int=9), b"short")
