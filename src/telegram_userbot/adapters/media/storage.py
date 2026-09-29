"""App-owned media namespace with atomic writes and reference-aware cleanup."""

import hashlib
import io
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from uuid import UUID

from PIL import Image, ImageOps

from telegram_userbot.adapters.media.validation import ValidatedImage

MIME_EXTENSION = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

_ROOT_LOCK_GUARD = RLock()
_ROOT_LOCKS: dict[Path, RLock] = {}


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
    def __init__(
        self, root: Path, *, quota_bytes: int = 10 * 1024 * 1024 * 1024, initialize: bool = True
    ) -> None:
        if quota_bytes <= 0:
            raise ValueError("media quota must be positive")
        if root.is_symlink():
            raise ValueError("media_root_symlink")
        if initialize:
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o700)
        elif not root.is_dir():
            raise ValueError("media_root_unavailable")
        self._root = root.resolve(strict=True)
        self._quota_bytes = quota_bytes
        with _ROOT_LOCK_GUARD:
            self._quota_thread_lock = _ROOT_LOCKS.setdefault(self._root, RLock())

    def quota(self) -> MediaQuota:
        with self._quota_guard():
            return MediaQuota(self._used_bytes(), self._quota_bytes)

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
        digest = hashlib.sha256(payload).digest()
        key = PurePosixPath(
            str(account_id), digest.hex()[:2], f"{object_id}{MIME_EXTENSION[mime_type]}"
        )
        with self._quota_guard():
            if (
                self._erasure_marker(account_id, object_id).exists()
                or self._erasure_marker(account_id, None).exists()
            ):
                raise RuntimeError("media_object_erased")
            target = self.resolve_key(key.as_posix(), must_exist=False)
            existing_size = (
                target.stat().st_size if target.is_file() and not target.is_symlink() else 0
            )
            required_delta = len(payload) - existing_size
            if required_delta > self._quota_bytes - self._used_bytes():
                raise RuntimeError("media_quota_exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.parent.chmod(0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".ingest-{object_id}-", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                temporary.chmod(0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(target)
                _verify_persisted_file(target, payload, digest)
                _fsync_directory(target.parent)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        return StoredMedia(
            key.as_posix(), digest, len(payload), mime_type, width, height, metadata_cleared
        )

    def _used_bytes(self) -> int:
        lock_path = self._root / ".quota.lock"
        return sum(
            path.stat().st_size
            for path in self._root.rglob("*")
            if path != lock_path and path.is_file() and not path.is_symlink()
        )

    @contextmanager
    def _quota_guard(self) -> Iterator[None]:
        """Serialize quota check and rename within this process and host."""

        with self._quota_thread_lock:
            lock_path = self._root / ".quota.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.name == "nt":
                    msvcrt = __import__("msvcrt")
                    if lock_path.stat().st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    fcntl = __import__("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if os.name == "nt":
                    msvcrt = __import__("msvcrt")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = __import__("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def resolve_key(self, storage_key: str, *, must_exist: bool = True) -> Path:
        key = PurePosixPath(*self._storage_key_parts(storage_key))
        candidate = self._root / Path(*key.parts)
        for part in (candidate, *candidate.parents):
            if part == self._root:
                break
            if part.is_symlink():
                raise ValueError("media_storage_key_symlink")
        target = candidate.resolve(strict=must_exist)
        if self._root not in target.parents:
            raise ValueError("media_storage_key_outside_root")
        if must_exist and (not target.is_file() or target.is_symlink()):
            raise ValueError("media_storage_object_invalid")
        return target

    def read_verified(self, *, storage_key: str, expected_sha256: bytes, max_bytes: int) -> bytes:
        if len(expected_sha256) != 32:
            raise ValueError("media_hash_invalid")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("media_byte_limit")
        descriptor = self._open_read_descriptor(storage_key)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ValueError("media_byte_limit")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(max_bytes + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(payload) > max_bytes
            or len(payload) != after.st_size
            or _stable_file_identity(before) != _stable_file_identity(after)
        ):
            raise ValueError("media_object_changed")
        if hashlib.sha256(payload).digest() != expected_sha256:
            raise ValueError("media_hash_mismatch")
        return payload

    @staticmethod
    def _storage_key_parts(storage_key: str) -> tuple[str, ...]:
        if not isinstance(storage_key, str) or not storage_key or len(storage_key) > 1024:
            raise ValueError("media_storage_key_invalid")
        key = PurePosixPath(storage_key)
        parts = key.parts
        if (
            key.is_absolute()
            or not parts
            or any(
                part in {"", ".", ".."}
                or len(part.encode("utf-8")) > 255
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
                for part in parts
            )
            or "\\" in storage_key
        ):
            raise ValueError("media_storage_key_invalid")
        return parts

    def _open_read_descriptor(self, storage_key: str) -> int:
        """Open one file beneath the media root without following any path symlink.

        Linux production uses a descriptor-relative walk so replacing a parent
        directory between validation and ``open`` cannot redirect the provider
        read outside the private root. Windows lacks ``dir_fd`` support and keeps
        the resolved-path fallback behind the existing private-root ACL boundary.
        """

        parts = self._storage_key_parts(storage_key)
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if os.name == "nt":
            try:
                return os.open(self.resolve_key(storage_key), file_flags)
            except OSError:
                raise ValueError("media_storage_object_invalid") from None

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory_flag:
            raise ValueError("media_storage_boundary_unsupported")
        directory_flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
        descriptors: list[int] = []
        try:
            parent = os.open(self._root, directory_flags)
            descriptors.append(parent)
            for part in parts[:-1]:
                parent = os.open(part, directory_flags, dir_fd=parent)
                descriptors.append(parent)
            return os.open(parts[-1], file_flags, dir_fd=parent)
        except OSError:
            raise ValueError("media_storage_object_invalid") from None
        finally:
            for directory_descriptor in reversed(descriptors):
                os.close(directory_descriptor)

    def delete_verified(self, *, storage_key: str, expected_sha256: bytes) -> bool:
        if len(expected_sha256) != 32:
            raise ValueError("media_hash_invalid")
        with self._quota_guard():
            try:
                target = self.resolve_key(storage_key)
            except FileNotFoundError:
                return False
            with target.open("rb") as handle:
                actual_sha256 = hashlib.file_digest(handle, "sha256").digest()
            if actual_sha256 != expected_sha256:
                raise ValueError("media_hash_mismatch")
            target.unlink()
            _fsync_directory(target.parent)
            return True

    def _erasure_marker(self, account_id: UUID, object_id: UUID | None) -> Path:
        return self.resolve_key(
            f".erased/{account_id}/{object_id if object_id is not None else 'account'}",
            must_exist=False,
        )

    def _persist_erasure_marker(self, account_id: UUID, object_id: UUID | None) -> None:
        marker = self._erasure_marker(account_id, object_id)
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if marker.exists() and not marker.is_file():
            raise ValueError("media_erasure_marker_invalid")
        with marker.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(marker.parent)
        _fsync_directory(marker.parent.parent)
        _fsync_directory(self._root)

    def _account_files(self, account_id: UUID) -> tuple[Path, ...]:
        directory = self.resolve_key(str(account_id), must_exist=False)
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise ValueError("media_account_directory_invalid")
        files: list[Path] = []
        for parent, directories, names in os.walk(directory, followlinks=False):
            for name in directories + names:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError("media_storage_key_symlink")
            files.extend(Path(parent) / name for name in directories + names)
        return tuple(files)

    @staticmethod
    def _legacy_temporary(path: Path) -> bool:
        if not path.name.startswith(".ingest-"):
            return False
        try:
            UUID(path.name[8:44])
        except ValueError:
            return True
        return False

    def erase_legacy_account_uploads(self, account_id: UUID) -> None:
        """Only for a durably deleting account; stop all its writers before unlink."""
        with self._quota_guard():
            self._persist_erasure_marker(account_id, None)
            for path in self._account_files(account_id):
                if self._legacy_temporary(path):
                    self._unlink_regular(path)

    def verify_erasure_inventory(
        self, *, account_id: UUID, objects: Mapping[UUID, bool | None], account_wipe: bool
    ) -> None:
        """Require every remaining file to belong to a surviving database object.

        The caller holds the account's database admission lock. The shared file
        lock closes the write/rename window, including writers admitted earlier.
        Unknown files are evidence requiring repair, never an absence proof.
        """
        with self._quota_guard():
            if account_wipe:
                self._persist_erasure_marker(account_id, None)
            for object_id, deleted in objects.items():
                if deleted:
                    self._persist_erasure_marker(account_id, object_id)
            for path in self._account_files(account_id):
                if path.is_dir():
                    continue
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("media_inventory_invalid")
                try:
                    object_id = UUID(
                        path.name[8:44] if path.name.startswith(".ingest-") else path.stem
                    )
                except ValueError:
                    raise ValueError("media_inventory_unattributed") from None
                if (
                    account_wipe
                    or object_id not in objects
                    or objects[object_id] is not False
                    or (
                        not path.name.startswith(f".ingest-{object_id}-")
                        and path.suffix not in MIME_EXTENSION.values()
                    )
                ):
                    raise ValueError("media_inventory_unattributed")

    @staticmethod
    def _unlink_regular(path: Path) -> None:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("media_storage_object_invalid")
        path.unlink()
        _fsync_directory(path.parent)

    def erase_object(
        self,
        *,
        account_id: UUID,
        object_id: UUID,
        storage_key: str | None,
        expected_sha256: bytes | None,
    ) -> bool:
        """Fence writers and remove this UUID's finals and crash remnants atomically.

        A pending row has no committed hash. Its exact UUID namespace is authoritative;
        anonymous legacy temporary files require an account-wide wipe instead.
        """
        if (storage_key is None) != (expected_sha256 is None):
            raise ValueError("media_registered_identity_incomplete")
        if expected_sha256 is not None and len(expected_sha256) != 32:
            raise ValueError("media_hash_invalid")
        if storage_key is not None:
            parts = self._storage_key_parts(storage_key)
            if parts[0] != str(account_id) or Path(parts[-1]).stem != str(object_id):
                raise ValueError("media_object_namespace_mismatch")
        with self._quota_guard():
            self._persist_erasure_marker(account_id, object_id)
            files = self._account_files(account_id)
            if any(self._legacy_temporary(path) for path in files):
                raise ValueError("media_legacy_temporary_unattributed")
            if any(
                path.stem == str(object_id) and path.suffix not in MIME_EXTENSION.values()
                for path in files
            ):
                raise ValueError("media_object_namespace_unrecognized")
            targets = [
                path
                for path in files
                if path.name.startswith(f".ingest-{object_id}-")
                or (path.stem == str(object_id) and path.suffix in MIME_EXTENSION.values())
            ]
            # Validate all files first; a registered final always needs its committed hash.
            registered = self.resolve_key(storage_key, must_exist=False) if storage_key else None
            for path in targets:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("media_storage_object_invalid")
                if path == registered:
                    with path.open("rb") as handle:
                        if hashlib.file_digest(handle, "sha256").digest() != expected_sha256:
                            raise ValueError("media_hash_mismatch")
            if registered is not None and registered.exists() and registered not in targets:
                raise ValueError("media_object_namespace_mismatch")
            for path in targets:
                self._unlink_regular(path)
            return bool(targets)

    def cleanup(self, candidates: Iterable[CleanupCandidate], *, now: datetime) -> CleanupReport:
        deleted: list[str] = []
        protected: list[str] = []
        missing: list[str] = []
        with self._quota_guard():
            for candidate in sorted(
                candidates, key=lambda item: (item.expires_at, item.storage_key)
            ):
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


def _fsync_directory(directory: Path) -> None:
    """Persist directory entry changes on the production POSIX filesystem."""

    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stable_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Fields that must remain stable while one already-open file is consumed."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
