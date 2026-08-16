import asyncio
import io
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid7

import pytest
from PIL import Image

from telegram_userbot.adapters.media import (
    CleanupCandidate,
    DurableMediaCleanup,
    ImageIngestionError,
    ImageIngestor,
    ImageLimits,
    PrivateMediaStore,
    StoredMedia,
    ValidatedImage,
)
from telegram_userbot.adapters.media import storage as media_storage
from telegram_userbot.adapters.persistence.media_repository import (
    MediaDeletionLease,
    MediaDeletionOutcome,
    MediaRepository,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


def image_bytes(
    image_format: str = "PNG", *, size: tuple[int, int] = (8, 6), exif: Image.Exif | None = None
) -> bytes:
    image = Image.new("RGB", size, (10, 20, 30))
    output = io.BytesIO()
    options = {"exif": exif} if exif is not None else {}
    image.save(output, format=image_format, **options)
    return output.getvalue()


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.unit
@pytest.mark.asyncio
async def test_image_ingestion_validates_magic_decode_limits_and_hash() -> None:
    payload = image_bytes("PNG")
    ingested = await ImageIngestor().ingest(
        chunks(payload[:7], payload[7:]), declared_mime="image/png", declared_size=len(payload)
    )
    assert ingested.mime_type == "image/png"
    assert (ingested.width, ingested.height) == (8, 6)
    assert ingested.byte_size == len(payload)
    assert len(ingested.sha256) == 32

    cases = (
        (payload, "image/jpeg", "image_mime_mismatch"),
        (payload[:20], "image/png", "image_decode_failed"),
        (b"not an image", "image/png", "image_decode_failed"),
    )
    for invalid, mime, code in cases:
        with pytest.raises(ImageIngestionError, match=code):
            ImageIngestor().validate_bytes(invalid, declared_mime=mime)

    animation = io.BytesIO()
    frames = [Image.new("RGB", (2, 2), color) for color in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(animation, format="PNG", save_all=True, append_images=frames[1:], duration=10)
    with pytest.raises(ImageIngestionError, match="animation_unsupported"):
        ImageIngestor().validate_bytes(animation.getvalue(), declared_mime="image/png")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_image_ingestion_fails_closed_for_size_dimension_pixel_and_timeout() -> None:
    payload = image_bytes(size=(8, 6))
    with pytest.raises(ImageIngestionError, match="image_byte_limit"):
        await ImageIngestor(ImageLimits(max_bytes=10)).ingest(
            chunks(payload), declared_mime="image/png"
        )
    with pytest.raises(ImageIngestionError, match="image_dimension_limit"):
        ImageIngestor(ImageLimits(max_side=7)).validate_bytes(payload, declared_mime="image/png")
    with pytest.raises(ImageIngestionError, match="image_pixel_limit"):
        ImageIngestor(ImageLimits(max_pixels=47)).validate_bytes(payload, declared_mime="image/png")

    async def delayed() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.05)
        yield payload

    with pytest.raises(ImageIngestionError, match="image_download_timeout"):
        await ImageIngestor(ImageLimits(timeout_seconds=0.001)).ingest(
            delayed(), declared_mime="image/png"
        )

    with pytest.raises(ValueError, match="limits must be positive"):
        ImageLimits(max_concurrency=0)
    with pytest.raises(ImageIngestionError, match="image_mime_unsupported"):
        await ImageIngestor().ingest(chunks(payload), declared_mime="image/gif")
    with pytest.raises(ImageIngestionError, match="image_byte_limit"):
        await ImageIngestor().ingest(chunks(payload), declared_mime="image/png", declared_size=-1)
    with pytest.raises(ImageIngestionError, match="image_download_chunk_invalid"):
        await ImageIngestor().ingest(chunks(b""), declared_mime="image/png")
    with pytest.raises(ImageIngestionError, match="image_empty"):
        await ImageIngestor().ingest(chunks(), declared_mime="image/png")
    with pytest.raises(ImageIngestionError, match="image_empty"):
        ImageIngestor().validate_bytes(b"", declared_mime="image/png")
    with pytest.raises(ImageIngestionError, match="image_byte_limit"):
        ImageIngestor(ImageLimits(max_bytes=1)).validate_bytes(payload, declared_mime="image/png")
    with pytest.raises(ValueError, match="invalid validated image metadata"):
        ValidatedImage(SensitiveValue(payload), b"short", "image/png", 8, 6, len(payload))
    with pytest.raises(ValueError, match="invalid validated image metadata"):
        ValidatedImage(SensitiveValue(payload), b"x" * 32, "image/gif", 8, 6, len(payload))

    gif = image_bytes("GIF")
    with pytest.raises(ImageIngestionError, match="image_format_unsupported"):
        ImageIngestor().validate_bytes(gif, declared_mime="image/png")


@pytest.mark.unit
def test_media_directory_fsync_skips_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    media_storage._fsync_directory(tmp_path)


@pytest.mark.unit
def test_private_store_is_atomic_hash_verified_and_provider_copy_clears_exif(
    tmp_path: Path,
) -> None:
    exif = Image.Exif()
    exif[0x010E] = "SYNTHETIC_PRIVATE_DESCRIPTION"
    payload = image_bytes("JPEG", exif=exif)
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/jpeg")
    store = PrivateMediaStore(tmp_path / "media-data", quota_bytes=10_000)
    original = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    provider = store.store_provider_copy(account_id=uuid7(), object_id=uuid7(), image=image)

    assert (
        store.read_verified(
            storage_key=original.storage_key,
            expected_sha256=original.sha256,
            max_bytes=10_000,
        )
        == payload
    )
    provider_payload = store.read_verified(
        storage_key=provider.storage_key,
        expected_sha256=provider.sha256,
        max_bytes=10_000,
    )
    assert b"SYNTHETIC_PRIVATE_DESCRIPTION" not in provider_payload
    assert provider.metadata_cleared is True
    assert not list((tmp_path / "media-data").rglob(".ingest-*"))

    target = store.resolve_key(original.storage_key)
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash_mismatch"):
        store.read_verified(
            storage_key=original.storage_key,
            expected_sha256=original.sha256,
            max_bytes=10_000,
        )
    for key in ("../escape.png", "/absolute.png", "folder\\escape.png"):
        with pytest.raises(ValueError, match="storage_key"):
            store.resolve_key(key, must_exist=False)

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    escape = tmp_path / "media-data" / "escape.png"
    try:
        escape.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="storage_key_symlink"):
            store.resolve_key("escape.png")

    inside_target = tmp_path / "media-data" / "inside-target.png"
    inside_target.write_bytes(b"inside")
    inside_alias = tmp_path / "media-data" / "inside-alias.png"
    try:
        inside_alias.symlink_to(inside_target)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="storage_key_symlink"):
            store.resolve_key("inside-alias.png")

    directory = tmp_path / "media-data" / "not-a-file"
    directory.mkdir()
    with pytest.raises(ValueError, match="object_invalid"):
        store.resolve_key("not-a-file")


@pytest.mark.unit
def test_private_store_delete_is_hash_verified_and_missing_is_idempotent(tmp_path: Path) -> None:
    payload = image_bytes()
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/png")
    store = PrivateMediaStore(tmp_path / "verified-delete")
    stored = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)

    with pytest.raises(ValueError, match="hash_mismatch"):
        store.delete_verified(storage_key=stored.storage_key, expected_sha256=b"x" * 32)
    assert store.resolve_key(stored.storage_key).exists()
    assert store.delete_verified(
        storage_key=stored.storage_key,
        expected_sha256=stored.sha256,
    )
    assert not store.delete_verified(
        storage_key=stored.storage_key,
        expected_sha256=stored.sha256,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_media_cleanup_records_success_and_hash_failure(tmp_path: Path) -> None:
    payload = image_bytes()
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/png")
    store = PrivateMediaStore(tmp_path / "durable-cleanup")
    stored = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    account_id, object_id = uuid7(), uuid7()
    repository = AsyncMock(spec=MediaRepository)
    repository.claim_expired.return_value = (
        MediaDeletionLease(
            object_id,
            account_id,
            stored.storage_key,
            stored.sha256,
            1,
            1,
        ),
    )
    repository.finish_deletion.return_value = MediaDeletionOutcome(True)

    report = await DurableMediaCleanup(
        repository=cast(MediaRepository, repository),
        store=store,
    ).run_once(now=datetime(2030, 1, 1, tzinfo=UTC))
    assert (report.deleted, report.already_missing, report.failed) == (1, 0, 0)
    repository.finish_deletion.assert_awaited_once()
    assert repository.finish_deletion.await_args.kwargs["deleted"] is True

    damaged = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    repository.reset_mock()
    repository.claim_expired.return_value = (
        MediaDeletionLease(uuid7(), uuid7(), damaged.storage_key, b"x" * 32, 2, 2),
    )
    repository.finish_deletion.return_value = MediaDeletionOutcome(True, True)
    alerts: list[str] = []
    report = await DurableMediaCleanup(
        repository=cast(MediaRepository, repository),
        store=store,
        on_failure=alerts.append,
    ).run_once(now=datetime(2030, 1, 1, tzinfo=UTC))
    assert (report.deleted, report.already_missing, report.failed) == (0, 0, 1)
    assert store.resolve_key(damaged.storage_key).exists()
    assert cast(bool, repository.finish_deletion.await_args.kwargs["deleted"]) is False
    assert alerts == ["media_delete_failed_critical"]

    repository.reset_mock()
    repository.claim_expired.return_value = (
        MediaDeletionLease(uuid7(), uuid7(), "already-missing.png", b"m" * 32, 3, 1),
    )
    repository.finish_deletion.return_value = MediaDeletionOutcome(True)
    report = await DurableMediaCleanup(
        repository=cast(MediaRepository, repository),
        store=store,
    ).run_once(now=datetime(2030, 1, 1, tzinfo=UTC))
    assert (report.deleted, report.already_missing, report.failed) == (0, 1, 0)

    repository.reset_mock()
    repository.claim_expired.return_value = (
        MediaDeletionLease(uuid7(), uuid7(), "still-missing.png", b"m" * 32, 4, 1),
    )
    repository.finish_deletion.return_value = MediaDeletionOutcome(False)
    report = await DurableMediaCleanup(
        repository=cast(MediaRepository, repository),
        store=store,
    ).run_once(now=datetime(2030, 1, 1, tzinfo=UTC))
    assert report == type(report)(0, 0, 0)


@pytest.mark.unit
def test_media_cleanup_respects_expiry_references_and_missing_files(tmp_path: Path) -> None:
    payload = image_bytes()
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/png")
    store = PrivateMediaStore(tmp_path / "media-data")
    expired = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    protected = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    future = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    report = store.cleanup(
        (
            CleanupCandidate(expired.storage_key, now - timedelta(days=1), 0),
            CleanupCandidate(protected.storage_key, now - timedelta(days=1), 1),
            CleanupCandidate(future.storage_key, now + timedelta(days=1), 0),
            CleanupCandidate("missing/object.png", now - timedelta(days=1), 0),
        ),
        now=now,
    )
    assert report.deleted_keys == (expired.storage_key,)
    assert report.protected_keys == (protected.storage_key,)
    assert report.missing_keys == ("missing/object.png",)
    assert store.resolve_key(protected.storage_key).exists()
    assert store.resolve_key(future.storage_key).exists()


@pytest.mark.unit
def test_media_store_quota_read_limit_and_copy_formats_fail_closed(tmp_path: Path) -> None:
    payload = image_bytes()
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/png")
    with pytest.raises(ValueError, match="quota must be positive"):
        PrivateMediaStore(tmp_path / "invalid", quota_bytes=0)
    tiny = PrivateMediaStore(tmp_path / "tiny", quota_bytes=1)
    with pytest.raises(RuntimeError, match="media_quota_exceeded"):
        tiny.store_original(account_id=uuid7(), object_id=uuid7(), image=image)

    store = PrivateMediaStore(tmp_path / "copies")
    original = store.store_original(account_id=uuid7(), object_id=uuid7(), image=image)
    with pytest.raises(ValueError, match="media_byte_limit"):
        store.read_verified(
            storage_key=original.storage_key,
            expected_sha256=original.sha256,
            max_bytes=1,
        )
    assert (
        store.store_provider_copy(account_id=uuid7(), object_id=uuid7(), image=image).mime_type
        == "image/png"
    )
    webp_payload = image_bytes("WEBP")
    webp = ImageIngestor().validate_bytes(webp_payload, declared_mime="image/webp")
    assert (
        store.store_provider_copy(account_id=uuid7(), object_id=uuid7(), image=webp).mime_type
        == "image/webp"
    )


@pytest.mark.unit
def test_media_quota_check_and_rename_are_one_critical_section(tmp_path: Path) -> None:
    payload = image_bytes()
    image = ImageIngestor().validate_bytes(payload, declared_mime="image/png")
    root = tmp_path / "concurrent"
    stores = [PrivateMediaStore(root, quota_bytes=len(payload)) for _ in range(2)]

    def write(index: int) -> StoredMedia:
        return stores[index].store_original(account_id=uuid7(), object_id=uuid7(), image=image)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(write, index) for index in range(2))
        results = []
        errors = []
        for future in futures:
            try:
                results.append(future.result())
            except RuntimeError as error:
                errors.append(str(error))
    assert len(results) == 1
    assert errors == ["media_quota_exceeded"]
    used = (stores[0].quota().used_bytes, stores[1].quota().used_bytes)
    assert len(payload) in used
