from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.model_snapshots import (
    CAPABILITY_DIGEST_FIELDS,
    capability_snapshot_digest,
)


def _row() -> dict[str, object]:
    return {
        "endpoint_id": UUID("01900000-0000-7000-8000-000000000001"),
        "protocol": "openai_responses",
        "model_name": "model-a",
        "supports_text": True,
        "supports_temperature": False,
        "supports_reasoning_effort": True,
        "supports_image": True,
        "supports_stream": False,
        "supports_structured_output": True,
        "chat_token_limit_field": None,
        "max_context_tokens": 8192,
        "max_output_tokens_limit": 2048,
        "max_images_per_request": 4,
        "max_image_bytes_per_request": 4_000_000,
        "auto_image_tokens": 256,
        "messages_auto_detail_equivalent": True,
        "supported_input_roles": ("user", "assistant"),
        "embedding_dimensions": (1536,),
        "metadata_schema_version": 1,
        "metadata": {"region": "test", "revision": 1},
        "observed_at": datetime(2030, 1, 2, tzinfo=UTC),
        "expires_at": None,
    }


@pytest.mark.unit
def test_digest_is_stable_for_nested_supported_values_and_aliases() -> None:
    row = _row()
    digest = capability_snapshot_digest(row)
    assert len(digest) == 32
    assert digest == capability_snapshot_digest(dict(row))
    aliases = {name: f"cap_{name}" for name in CAPABILITY_DIGEST_FIELDS}
    aliased = {aliases[name]: value for name, value in row.items()}
    assert digest == capability_snapshot_digest(aliased, aliases=aliases)

    changed = dict(row)
    changed["metadata"] = {"region": "other", "revision": 1}
    assert capability_snapshot_digest(changed) != digest

    finite = dict(row)
    finite["metadata"] = {"ratio": 1.25, "decimal": Decimal("1.25")}
    assert len(capability_snapshot_digest(finite)) == 32


@pytest.mark.unit
@pytest.mark.parametrize(
    "replacement",
    [
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        b"binary",
        object(),
    ],
)
def test_digest_rejects_non_finite_or_unsupported_values(replacement: object) -> None:
    row = _row()
    row["metadata"] = {"value": replacement}
    with pytest.raises(ValueError, match="model snapshot"):
        capability_snapshot_digest(row)


@pytest.mark.unit
def test_digest_rejects_missing_fields_non_string_keys_and_naive_timestamp() -> None:
    row = _row()
    row.pop("protocol")
    with pytest.raises(ValueError, match="incomplete"):
        capability_snapshot_digest(row)

    row = _row()
    row["metadata"] = cast(object, {1: "not allowed"})
    with pytest.raises(ValueError, match="keys"):
        capability_snapshot_digest(row)

    row = _row()
    row["observed_at"] = datetime(2030, 1, 2, tzinfo=None)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timestamp"):
        capability_snapshot_digest(row)
