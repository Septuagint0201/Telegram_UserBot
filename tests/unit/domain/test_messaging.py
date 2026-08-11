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
    ReactionDescriptor,
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


@pytest.mark.parametrize(
    ("kind", "text", "message"),
    [
        (BodyKind.NONE, "unexpected", "none cannot contain text"),
        (BodyKind.TEXT, None, "require text"),
    ],
)
def test_message_body_rejects_inconsistent_kind_and_text(
    kind: BodyKind, text: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MessageBody(kind, text)
    assert MessageBody(BodyKind.NONE).content_sha256 is None


@pytest.mark.parametrize(
    ("position", "name", "message"),
    [
        (-1, None, "nonnegative"),
        (0, "unsafe/path.png", "must not contain a path"),
    ],
)
def test_media_descriptor_rejects_invalid_persistence_metadata(
    position: int, name: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MediaDescriptor(MediaKind.PHOTO, position, original_name_sanitized=name)


def _validation_event(
    *,
    kind: EventKind,
    identity: str,
    telegram_message_id: int | None = None,
    reaction: ReactionDescriptor | None = None,
) -> NormalizedTelegramEvent:
    return NormalizedTelegramEvent(
        UUID(int=20),
        UUID(int=1),
        UUID(int=2),
        kind,
        PeerKind.PRIVATE_USER,
        identity,
        datetime(2030, 1, 1, tzinfo=UTC),
        telegram_message_id=telegram_message_id,
        reaction=reaction,
    )


def test_normalized_event_requires_identity_reaction_and_message_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        _validation_event(kind=EventKind.SERVICE, identity="")
    with pytest.raises(ValueError, match="reaction metadata"):
        _validation_event(
            kind=EventKind.REACTION_CHANGED,
            identity="reaction:missing",
            telegram_message_id=1,
        )
    with pytest.raises(ValueError, match="telegram message id"):
        _validation_event(kind=EventKind.MESSAGE_CREATED, identity="message:missing")
    reaction = _validation_event(
        kind=EventKind.REACTION_CHANGED,
        identity="reaction:valid",
        telegram_message_id=1,
        reaction=ReactionDescriptor("emoji:wave", True),
    )
    assert reaction.reaction is not None
