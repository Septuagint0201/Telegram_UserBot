"""Verified private bytes on the actual provider wire, with no filesystem writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.adapters.persistence.test_memory_images import PNG, image_rows
from tests.unit.processes.test_memory_pipeline import NOW, SECRET, prepared

from telegram_userbot.adapters.media import PrivateMediaStore
from telegram_userbot.adapters.persistence.memory_images import image_source
from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError
from telegram_userbot.adapters.persistence.memory_pipeline import input_document
from telegram_userbot.application.ports.model import ModelGatewayError
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.processes.memory_pipeline import MemoryPipelineExecutor, generation_request
from telegram_userbot.processes.model_gateway import PrivateMediaRuntimeImageLoader

pytestmark = pytest.mark.unit


def image_prepared() -> tuple[Any, Any]:
    value, keyring = prepared()
    image, source = image_source(*image_rows(), now=NOW)
    manifest = replace(value.manifest, sources=(*value.manifest.sources, source), image_count=1)
    return replace(
        value,
        manifest=manifest,
        images=(image,),
        user_input=SensitiveValue(input_document(manifest, [])),
        model=replace(
            value.model, capabilities=replace(value.model.capabilities, supports_images=True)
        ),
    ), keyring


async def test_image_request_preserves_order_digest_and_private_repr(
    tmp_path: Path, monkeypatch: Any
) -> None:
    value, keyring = image_prepared()
    target = tmp_path / value.images[0].storage_key
    target.parent.mkdir()
    target.write_bytes(PNG)
    # A read-only worker must neither initialize the root nor take a quota lock.
    monkeypatch.setattr(Path, "chmod", MagicMock(side_effect=AssertionError("chmod")))
    monkeypatch.setattr(Path, "mkdir", MagicMock(side_effect=AssertionError("mkdir")))
    loader = PrivateMediaRuntimeImageLoader(tmp_path)
    runtime = MemoryPipelineExecutor(
        keyring=keyring, fingerprint_secret=SensitiveValue(SECRET), image_loader=loader
    )
    images = await runtime._image_content(value)
    request = generation_request(value, images)
    assert [part.kind.value for part in request.messages[1].content] == ["text", "image"]
    assert images[0].image_bytes is not None
    assert images[0].image_bytes.reveal_for_use() == PNG
    assert json.loads(images[0].value.reveal_for_use())["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert "synthetic-image" not in repr(request)
    assert not (tmp_path / ".quota.lock").exists()


@pytest.mark.parametrize(
    "mode", ["changed", "mime", "size", "loader", "membership", "missing_file"]
)
async def test_image_read_failure_is_closed(tmp_path: Path, mode: str) -> None:
    value, keyring = image_prepared()
    payload = PNG if mode != "changed" else b"different"
    loader: Any = AsyncMock(load=AsyncMock(return_value=SensitiveValue(payload)))
    if mode == "mime":
        value = replace(value, images=(replace(value.images[0], mime_type="image/jpeg"),))
    elif mode == "size":
        value = replace(value, images=(replace(value.images[0], byte_size=1),))
    elif mode == "loader":
        loader = None
    elif mode == "membership":
        value = replace(value, manifest=replace(value.manifest, sources=()))
    elif mode == "missing_file":
        loader = PrivateMediaRuntimeImageLoader(tmp_path / "absent")
    runtime = MemoryPipelineExecutor(
        keyring=keyring, fingerprint_secret=SensitiveValue(SECRET), image_loader=loader
    )
    with pytest.raises((ModelGatewayError, MemoryPipelineError)) as error:
        await runtime._image_content(value)
    assert isinstance(error.value, (ModelGatewayError, MemoryPipelineError))
    assert error.value.code in {
        "MODEL_IMAGE_SNAPSHOT_INVALID",
        "MODEL_IMAGE_SNAPSHOT_UNAVAILABLE",
        "MEMORY_IMAGE_LOADER_UNAVAILABLE",
        "MEMORY_IMAGE_SOURCE_INVALID",
    }


def test_existing_store_requires_a_real_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="media_root_unavailable"):
        PrivateMediaStore(tmp_path / "absent", initialize=False)
