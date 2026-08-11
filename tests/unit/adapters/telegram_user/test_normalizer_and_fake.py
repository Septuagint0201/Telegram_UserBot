from collections import deque
from datetime import UTC, datetime
from uuid import UUID

import pytest

from telegram_userbot.adapters.telegram_user import (
    FakeSendOutcome,
    PeerAdmission,
    RawMedia,
    RawTelegramUpdate,
    ReplayTelegramGateway,
    normalize_update,
)
from telegram_userbot.application.ports.telegram import (
    TelegramSendUnknownError,
    TelegramTextRequest,
)
from telegram_userbot.domain.messaging import Direction, EventKind, MediaKind, PeerKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def test_normalizer_strips_unsupported_peer_content() -> None:
    event = normalize_update(
        event_uuid=UUID(int=3),
        admission=PeerAdmission(UUID(int=1), None, PeerKind.GROUP, -1002),
        raw=RawTelegramUpdate(
            "update:3",
            EventKind.MESSAGE_CREATED,
            NOW,
            telegram_message_id=5,
            direction=Direction.INCOMING,
            text="private body",
            media=(RawMedia(MediaKind.VIDEO, file_reference="secret-ref"),),
        ),
    )
    assert event.body is None
    assert event.media == ()
    assert not event.model_eligible
    assert event.metadata == {
        "peer_kind": PeerKind.GROUP,
        "supported_scope": False,
        "unsupported_reason": PeerKind.GROUP,
    }


def test_normalizer_keeps_image_metadata_but_no_binary() -> None:
    event = normalize_update(
        event_uuid=UUID(int=4),
        admission=PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42),
        raw=RawTelegramUpdate(
            "update:4",
            EventKind.MESSAGE_CREATED,
            NOW,
            telegram_message_id=6,
            direction=Direction.INCOMING,
            text="caption",
            is_caption=True,
            media=(
                RawMedia(
                    MediaKind.IMAGE_DOCUMENT,
                    file_reference="opaque-ref",
                    mime_type="image/png",
                    size=12,
                    file_name="C:\\unsafe\\photo.png",
                    width=640,
                    height=480,
                    telegram_document_id=700,
                ),
                RawMedia(MediaKind.VOICE, file_reference="voice-ref", duration_ms=1000),
            ),
        ),
    )
    assert event.media[0].original_name_sanitized == "photo.png"
    assert event.media[0].metadata["download_eligible"] is True
    assert event.media[0].metadata["binary_persisted"] is False
    assert event.media[0].metadata["telegram_document_id"] == 700
    assert event.media[0].metadata["width"] == 640
    assert event.media[0].metadata["height"] == 480
    assert event.media[1].metadata["download_eligible"] is False


def test_normalizer_represents_missing_message_text_and_rejects_keyless_reaction() -> None:
    admission = PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42)
    message = normalize_update(
        event_uuid=UUID(int=40),
        admission=admission,
        raw=RawTelegramUpdate(
            "update:no-text",
            EventKind.MESSAGE_CREATED,
            NOW,
            telegram_message_id=40,
            direction=Direction.INCOMING,
        ),
    )
    assert message.body is not None
    assert message.body.kind.value == "none"
    with pytest.raises(ValueError, match="requires a key"):
        normalize_update(
            event_uuid=UUID(int=41),
            admission=admission,
            raw=RawTelegramUpdate(
                "reaction:no-key",
                EventKind.REACTION_CHANGED,
                NOW,
                telegram_message_id=40,
            ),
        )


def test_reaction_and_service_updates_remain_non_triggering_metadata() -> None:
    admission = PeerAdmission(UUID(int=1), UUID(int=2), PeerKind.PRIVATE_USER, 42)
    reaction = normalize_update(
        event_uuid=UUID(int=5),
        admission=admission,
        raw=RawTelegramUpdate(
            "reaction:5",
            EventKind.REACTION_CHANGED,
            NOW,
            telegram_message_id=6,
            reaction_key="emoji:thumbs_up",
            reaction_active=True,
        ),
    )
    service = normalize_update(
        event_uuid=UUID(int=6),
        admission=admission,
        raw=RawTelegramUpdate(
            "service:6",
            EventKind.SERVICE,
            NOW,
            service_kind="history_cleared",
        ),
    )
    assert reaction.reaction is not None
    assert reaction.reaction.key == "emoji:thumbs_up"
    assert not reaction.model_eligible
    assert service.body is None
    assert service.service_kind == "history_cleared"
    assert not service.model_eligible


@pytest.mark.asyncio
async def test_fake_unknown_after_accept_deduplicates_same_random_id() -> None:
    gateway = ReplayTelegramGateway(outcomes=deque([FakeSendOutcome.UNKNOWN_AFTER_ACCEPT]))
    request = TelegramTextRequest(
        AccountId(UUID(int=1)),
        ConversationId(UUID(int=2)),
        RunId(UUID(int=3)),
        99,
        SensitiveValue("synthetic output"),
    )
    with pytest.raises(TelegramSendUnknownError):
        await gateway.send_text(request)
    receipt = await gateway.send_text(request)
    assert receipt.telegram_message_id == 1000
    assert len(gateway.accepted_by_random_id) == 1
