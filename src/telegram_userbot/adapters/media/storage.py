"""App-owned media namespace with atomic writes and reference-aware cleanup."""

import hashlib
import io
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from PIL import Image, ImageOps

from telegram_userbot.adapters.media.validation import ValidatedImage

MIME_EXTENSION = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@dataclass(frozen=True, slots=True)
class MediaQuota:
    used_bytes: int
    limit_bytes: int = 10 * 1024 * 1024 * 1024

    @property
    def available_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)


@dataclass(frozen=True, slots=True)
class StoredMedia:
    storage_key: str
    sha256: bytes
    byte_size: int
    mime_type: str
    width: int
    height: int
    metadata_cleared: bool


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    storage_key: str
    expires_at: datetime
    reference_count: int


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted_keys: tuple[str, ...]
    protected_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]


class PrivateMediaStore:
    def __init__(self, root: Path, *, quota_bytes: int = 10 * 1024 * 1024 * 1024) -> None:
        if quota_bytes <= 0:
            raise ValueError("media quota must be positive")
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        self._root = root.resolve(strict=True)
        self._quota_bytes = quota_bytes

    def quota(self) -> MediaQuota:
        used = sum(
            path.stat().st_size
            for path in self._root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        return MediaQuota(used, self._quota_bytes)

    def store_original(
        self, *, account_id: UUID, object_id: UUID, image: ValidatedImage
    ) -> StoredMedia:
        return self._store(
            account_id=account_id,
            object_id=object_id,
            payload=image.content.reveal_for_use(),
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            metadata_cleared=False,
        )

    def store_provider_copy(
        self, *, account_id: UUID, object_id: UUID, image: ValidatedImage
    ) -> StoredMedia:
        payload, width, height = _metadata_free_copy(image)
        return self._store(
            account_id=account_id,
            object_id=object_id,
            payload=payload,
            mime_type=image.mime_type,
            width=width,
            height=height,
            metadata_cleared=True,
        )

    def _store(  # noqa: PLR0913 - durable metadata is explicit
        self,
        *,
        account_id: UUID,
        object_id: UUID,
        payload: bytes,
        mime_type: str,
        width: int,
        height: int,
        metadata_cleared: bool,
    ) -> StoredMedia:
        if len(payload) > self.quota().available_bytes:
            raise RuntimeError("media_quota_exceeded")
        digest = hashlib.sha256(payload).digest()
        key = PurePosixPath(
            str(account_id), digest.hex()[:2], f"{object_id}{MIME_EXTENSION[mime_type]}"
        )
        target = self.resolve_key(key.as_posix(), must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ingest-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            temporary.chmod(0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            _verify_persisted_file(target, payload, digest)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return StoredMedia(
            key.as_posix(), digest, len(payload), mime_type, width, height, metadata_cleared
        )

    def resolve_key(self, storage_key: str, *, must_exist: bool = True) -> Path:
        key = PurePosixPath(storage_key)
        if key.is_absolute() or ".." in key.parts or "\\" in storage_key:
            raise ValueError("media_storage_key_invalid")
        target = (self._root / Path(*key.parts)).resolve(strict=must_exist)
        if self._root not in target.parents:
            raise ValueError("media_storage_key_outside_root")
        if must_exist and (not target.is_file() or target.is_symlink()):
            raise ValueError("media_storage_object_invalid")
        return target

    def read_verified(self, *, storage_key: str, expected_sha256: bytes, max_bytes: int) -> bytes:
        target = self.resolve_key(storage_key)
        if target.stat().st_size > max_bytes:
            raise ValueError("media_byte_limit")
        payload = target.read_bytes()
        if hashlib.sha256(payload).digest() != expected_sha256:
            raise ValueError("media_hash_mismatch")
        return payload

    def cleanup(self, candidates: Iterable[CleanupCandidate], *, now: datetime) -> CleanupReport:
        deleted: list[str] = []
        protected: list[str] = []
        missing: list[str] = []
        for candidate in sorted(candidates, key=lambda item: (item.expires_at, item.storage_key)):
            if candidate.expires_at > now:
                continue
            if candidate.reference_count > 0:
                protected.append(candidate.storage_key)
                continue
            try:
                target = self.resolve_key(candidate.storage_key)
            except FileNotFoundError:
                missing.append(candidate.storage_key)
                continue
            target.unlink()
            deleted.append(candidate.storage_key)
        return CleanupReport(tuple(deleted), tuple(protected), tuple(missing))


def _metadata_free_copy(image: ValidatedImage) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(image.content.reveal_for_use())) as opened:
        normalized = ImageOps.exif_transpose(opened)
        if image.mime_type == "image/jpeg" and normalized.mode not in {"RGB", "L"}:
            normalized = normalized.convert("RGB")
        output = io.BytesIO()
        if image.mime_type == "image/jpeg":
            normalized.save(output, format="JPEG", quality=90, optimize=True)
        elif image.mime_type == "image/png":
            normalized.save(output, format="PNG", optimize=True)
        else:
            normalized.save(output, format="WEBP", quality=90, method=6)
        return output.getvalue(), normalized.width, normalized.height


def _verify_persisted_file(target: Path, payload: bytes, digest: bytes) -> None:
    persisted = target.read_bytes()
    if len(persisted) != len(payload) or hashlib.sha256(persisted).digest() != digest:
        target.unlink(missing_ok=True)
        raise RuntimeError("media_write_verification_failed")
