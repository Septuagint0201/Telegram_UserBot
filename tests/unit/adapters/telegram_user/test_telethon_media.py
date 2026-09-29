from __future__ import annotations

from uuid import UUID

import pytest
from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.media import (
    TelethonMediaReferenceError,
    TelethonOpaqueMediaResolver,
)
from telegram_userbot.application.ports.media import TelegramImageDownloadRequest
from telegram_userbot.application.ports.telegram_peer import TelegramMediaBinding
from telegram_userbot.domain.messaging import MediaKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, MessageId

ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000001")
CONVERSATION_ID = UUID("018f0000-0000-7000-8000-000000000002")
MESSAGE_ID = UUID("018f0000-0000-7000-8000-000000000003")


def _request() -> TelegramImageDownloadRequest:
    return TelegramImageDownloadRequest(
        AccountId(ACCOUNT_ID),
        ConversationId(CONVERSATION_ID),
        MessageId(MESSAGE_ID),
        1,
        0,
    )


def _binding(kind: MediaKind, reference: str) -> TelegramMediaBinding:
    return TelegramMediaBinding(
        ACCOUNT_ID,
        CONVERSATION_ID,
        MESSAGE_ID,
        1,
        0,
        kind,
        reference,
        "image/jpeg",
        1,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_photo_reference_restores_exact_bound_size_type() -> None:
    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding:
        return _binding(MediaKind.PHOTO, "v2:photo:700:701:2:x:cGhvdG8tcmVm")

    resolved = await TelethonOpaqueMediaResolver(load)(_request())

    assert isinstance(resolved, types.InputPhotoFileLocation)
    assert resolved.id == 700
    assert resolved.file_reference == b"photo-ref"
    assert resolved.thumb_size == "x"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_image_document_reference_keeps_original_empty_thumb() -> None:
    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding:
        return _binding(MediaKind.IMAGE_DOCUMENT, "v1:document:800:-9:2:ZG9jLXJlZg")

    resolved = await TelethonOpaqueMediaResolver(load)(_request())

    assert isinstance(resolved, types.InputDocumentFileLocation)
    assert resolved.file_reference == b"doc-ref"
    assert resolved.thumb_size == ""


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "v1:photo:700:701:2:cGhvdG8tcmVm",
        "v2:photo:700:701:2::cGhvdG8tcmVm",
        "v2:photo:700:0701:2:x:cGhvdG8tcmVm",
        "v2:photo:700:701:2:xx:cGhvdG8tcmVm",
        "v2:photo:700:701:2:x:_",
    ],
)
async def test_photo_reference_rejects_missing_or_noncanonical_size_and_payload(
    reference: str,
) -> None:
    async def load(_request: TelegramImageDownloadRequest) -> TelegramMediaBinding:
        return _binding(MediaKind.PHOTO, reference)

    with pytest.raises(TelethonMediaReferenceError) as captured:
        await TelethonOpaqueMediaResolver(load)(_request())
    assert captured.value.code == "TELEGRAM_MEDIA_REFERENCE_INVALID"
