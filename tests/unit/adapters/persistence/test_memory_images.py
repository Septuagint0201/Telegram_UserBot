"""Image admission binds roots and bytes and never silently truncates a range."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.memory_images import image_source, memory_images
from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError, message_source
from telegram_userbot.adapters.persistence.memory_source_hash import EMPTY_MESSAGE_SHA256
from telegram_userbot.domain.memory.trigger import EventRange
from tests.unit.adapters.persistence.test_memory_pipeline import result
from tests.unit.processes.test_memory_pipeline import NOW, prepared, source_row

pytestmark = pytest.mark.unit
PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"


def image_rows() -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = {
        "id": UUID(int=101),
        "message_revision_id": UUID(int=30),
        "media_object_id": UUID(int=102),
        "position": 0,
    }
    row = {
        "id": UUID(int=102),
        "source_revision_id": UUID(int=30),
        "object_kind": "provider_copy",
        "status": "ready",
        "storage_key": "private/image.png",
        "sha256": hashlib.sha256(PNG).digest(),
        "validated_mime": "image/png",
        "byte_size": len(PNG),
        "expires_at": None,
        "delete_requested_at": None,
        "deleted_at": None,
    }
    return row, descriptor


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("status", "pending", "MEMORY_IMAGE_PENDING"),
        ("status", "rejected", "MEMORY_IMAGE_UNAVAILABLE"),
        ("status", "deleted", "MEMORY_IMAGE_UNAVAILABLE"),
        ("object_kind", "original", "MEMORY_IMAGE_UNAVAILABLE"),
        ("source_revision_id", UUID(int=99), "MEMORY_IMAGE_UNAVAILABLE"),
        ("delete_requested_at", NOW, "MEMORY_IMAGE_UNAVAILABLE"),
        ("deleted_at", NOW, "MEMORY_IMAGE_UNAVAILABLE"),
        ("expires_at", NOW, "MEMORY_IMAGE_UNAVAILABLE"),
        ("storage_key", None, "MEMORY_IMAGE_SOURCE_INVALID"),
        ("sha256", None, "MEMORY_IMAGE_SOURCE_INVALID"),
        ("validated_mime", "text/html", "MEMORY_IMAGE_SOURCE_INVALID"),
        ("byte_size", 0, "MEMORY_IMAGE_SOURCE_INVALID"),
    ],
)
def test_unavailable_images_are_explicit_failures(field: str, value: Any, code: str) -> None:
    row, descriptor = image_rows()
    with pytest.raises(MemoryPipelineError, match=code) as failure:
        image_source({**row, field: value}, descriptor, now=NOW)
    assert failure.value.retryable == (value == "pending")
    assert "private/image" not in str(failure.value)


def test_marker_hash_pins_root_position_and_bytes_without_storage_path() -> None:
    row, descriptor = image_rows()
    image, source = image_source(row, descriptor, now=NOW)
    assert image.sha256 == hashlib.sha256(PNG).digest()
    assert source.visual_only
    assert source.trust.value == "model_inference"
    assert source.content_sha256 == hashlib.sha256(source.content.encode()).digest()
    assert json.loads(source.content)["message_revision_id"] == str(
        descriptor["message_revision_id"]
    )
    assert "private/image" not in source.content
    assert image_source(row, {**descriptor, "position": 1}, now=NOW)[1] != source
    with pytest.raises(MemoryPipelineError, match="MEMORY_IMAGE_UNAVAILABLE"):
        image_source(None, descriptor, now=NOW)


@pytest.mark.parametrize(
    "mode", ["ready", "empty", "missing", "limit", "bytes", "unsupported", "duplicate"]
)
async def test_bounded_image_selection(mode: str) -> None:
    value, _ = prepared()
    row, descriptor = image_rows()
    count = 0 if mode == "empty" else 2 if mode in {"limit", "duplicate"} else 1
    if mode == "missing":
        descriptor["media_object_id"] = None
    session = AsyncMock(
        execute=AsyncMock(side_effect=[result(rows=[descriptor] * count), result(row), result(row)])
    )
    cap = replace(
        value.model.capabilities,
        supports_images=mode != "unsupported",
        max_images_per_request=1 if mode == "limit" else 10,
        max_image_bytes_per_request=1 if mode == "bytes" else 1000,
    )
    kwargs: dict[str, Any] = {
        "account_id": value.manifest.account_id,
        "sources": value.manifest.sources,
        "capabilities": cap,
        "now": NOW,
        "event_range": EventRange(1, 10),
    }
    if mode in {"ready", "empty"}:
        sources, images = await memory_images(session, **kwargs)
        assert len(sources) == len(images) == count
    else:
        code = {
            "missing": "MEMORY_IMAGE_PENDING",
            "limit": "MEMORY_IMAGE_LIMIT_EXCEEDED",
            "bytes": "MEMORY_IMAGE_LIMIT_EXCEEDED",
            "unsupported": "MEMORY_VISION_UNAVAILABLE",
            "duplicate": "MEMORY_IMAGE_SOURCE_INVALID",
        }[mode]
        with pytest.raises(MemoryPipelineError, match=code):
            await memory_images(session, **kwargs)


def test_empty_message_hash_does_not_reinterpret_redacted_text() -> None:
    row = {
        **source_row(),
        "body_kind": "none",
        "text_content": None,
        "entities": [],
        "content_sha256": None,
    }
    source = message_source(row)
    assert source.content_sha256 == EMPTY_MESSAGE_SHA256
    assert source.visual_only
    assert json.loads(source.content) == {"kind": "none", "text": None, "entities": []}
    with pytest.raises(MemoryPipelineError, match="MEMORY_SOURCE_INVALID"):
        message_source({**row, "body_kind": "text"})
