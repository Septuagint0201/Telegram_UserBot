"""Bounded, revision-owned image membership for immutable memory inputs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError
from telegram_userbot.adapters.persistence.model_runtime import (
    ModelRuntimeSnapshotError,
    RuntimeImageSnapshot,
)
from telegram_userbot.domain.memory.models import InputSource, TrustClass
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.model_config import ModelCapabilities


async def memory_images(  # noqa: PLR0913 - scope, bounds and capability are independent guards
    session: AsyncSession,
    *,
    account_id: UUID,
    sources: tuple[InputSource, ...],
    capabilities: ModelCapabilities,
    now: datetime,
    event_range: EventRange,
) -> tuple[tuple[InputSource, ...], tuple[RuntimeImageSnapshot, ...]]:
    # Revision rows are already locked by input selection. Attachment creation
    # requires their FK key-share lock; media state is locked separately below.
    descriptors = (
        (
            await session.execute(
                select(s.message_media)
                .join(
                    s.message_revisions,
                    s.message_revisions.c.id == s.message_media.c.message_revision_id,
                )
                .where(
                    s.message_media.c.account_id == account_id,
                    s.message_revisions.c.source_event_id.between(
                        event_range.start, event_range.end
                    ),
                    s.message_media.c.message_revision_id.in_(
                        [
                            item.source_id
                            for item in sources
                            if item.source_type == "message_revision"
                        ]
                    ),
                    s.message_media.c.media_kind.in_(("photo", "image_document")),
                )
                .order_by(s.message_media.c.message_revision_id, s.message_media.c.position)
                .limit(capabilities.max_images_per_request + 1)
            )
        )
        .mappings()
        .all()
    )
    if descriptors and not capabilities.supports_images:
        raise MemoryPipelineError("MEMORY_VISION_UNAVAILABLE")
    if len(descriptors) > capabilities.max_images_per_request:
        raise MemoryPipelineError("MEMORY_IMAGE_LIMIT_EXCEEDED")
    items: list[InputSource] = []
    images: list[RuntimeImageSnapshot] = []
    total_bytes = 0
    for descriptor in descriptors:
        if descriptor["media_object_id"] is None:
            raise MemoryPipelineError("MEMORY_IMAGE_PENDING", retryable=True)
        row = (
            (
                await session.execute(
                    select(s.media_objects)
                    .where(
                        s.media_objects.c.id == descriptor["media_object_id"],
                        s.media_objects.c.account_id == account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        image, source = image_source(row, descriptor, now=now)
        total_bytes += image.byte_size
        if total_bytes > capabilities.max_image_bytes_per_request:
            raise MemoryPipelineError("MEMORY_IMAGE_LIMIT_EXCEEDED")
        images.append(image)
        items.append(source)
    if len({image.object_id for image in images}) != len(images):
        raise MemoryPipelineError("MEMORY_IMAGE_SOURCE_INVALID")
    return tuple(items), tuple(images)


def image_source(
    row: Any, descriptor: Any, *, now: datetime
) -> tuple[RuntimeImageSnapshot, InputSource]:
    if row is not None and row["status"] == "pending":
        raise MemoryPipelineError("MEMORY_IMAGE_PENDING", retryable=True)
    if (
        row is None
        or row["object_kind"] != "provider_copy"
        or row["status"] != "ready"
        or row["source_revision_id"] != descriptor["message_revision_id"]
        or row["delete_requested_at"] is not None
        or row["deleted_at"] is not None
        or (row["expires_at"] is not None and row["expires_at"] <= now)
    ):
        raise MemoryPipelineError("MEMORY_IMAGE_UNAVAILABLE")
    try:
        image = RuntimeImageSnapshot(
            row["id"], row["storage_key"], row["sha256"], row["validated_mime"], row["byte_size"]
        )
    except ValueError, TypeError, ModelRuntimeSnapshotError:
        raise MemoryPipelineError("MEMORY_IMAGE_SOURCE_INVALID") from None
    # The marker hash binds binary digest, canonical root, attachment position,
    # and byte interpretation. Storage paths never enter the provider document.
    body = json.dumps(
        {
            "media_object_id": str(image.object_id),
            "message_revision_id": str(descriptor["message_revision_id"]),
            "attachment_id": str(descriptor["id"]),
            "position": descriptor["position"],
            "sha256": image.sha256.hex(),
            "mime": image.mime_type,
            "byte_size": image.byte_size,
            "detail": "auto",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return image, InputSource(
        image.object_id,
        f"sha256-{image.sha256.hex()}",
        body,
        hashlib.sha256(body.encode()).digest(),
        "media_object",
        TrustClass.MODEL_INFERENCE,
        visual_only=True,
    )
