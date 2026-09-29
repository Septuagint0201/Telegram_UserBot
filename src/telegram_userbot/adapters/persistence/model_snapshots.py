"""Deterministic, content-free identities for immutable model snapshots."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from telegram_userbot.domain.shared.hashing import JsonValue, stable_json_bytes

CAPABILITY_DIGEST_FIELDS = (
    "endpoint_id",
    "protocol",
    "model_name",
    "supports_text",
    "supports_temperature",
    "supports_reasoning_effort",
    "supports_image",
    "supports_stream",
    "supports_structured_output",
    "chat_token_limit_field",
    "max_context_tokens",
    "max_output_tokens_limit",
    "max_images_per_request",
    "max_image_bytes_per_request",
    "auto_image_tokens",
    "messages_auto_detail_equivalent",
    "supported_input_roles",
    "embedding_dimensions",
    "metadata_schema_version",
    "metadata",
    "observed_at",
    "expires_at",
)


def _canonical_json(value: object) -> JsonValue:  # noqa: PLR0911 - closed JSON type switch
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("model snapshot contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("model snapshot contains a non-finite number")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model snapshot timestamp must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("model snapshot object keys must be strings")
        return {str(key): _canonical_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | memoryview):
        return [_canonical_json(item) for item in value]
    raise ValueError("model snapshot contains an unsupported value")


def capability_snapshot_digest(
    row: Mapping[str, object],
    *,
    aliases: Mapping[str, str] | None = None,
) -> bytes:
    """Hash the exact admission fields with stable UUID and UTC encodings."""

    resolved_aliases = aliases or {}
    payload: dict[str, JsonValue] = {}
    for field in CAPABILITY_DIGEST_FIELDS:
        source = resolved_aliases.get(field, field)
        if source not in row:
            raise ValueError("model capability snapshot is incomplete")
        payload[field] = _canonical_json(row[source])
    return hashlib.sha256(stable_json_bytes(payload)).digest()


__all__ = ["CAPABILITY_DIGEST_FIELDS", "capability_snapshot_digest"]
