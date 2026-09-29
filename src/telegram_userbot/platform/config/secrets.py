"""Fail-closed loading for Compose-mounted secret files."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from telegram_userbot.domain.shared.redaction import SensitiveValue

type SecretPath = str | os.PathLike[str]

_PLACEHOLDERS = frozenset({"changeme", "change-me", "default", "example", "placeholder"})


class SecretFileError(ValueError):
    """A stable, content-free secret loading failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SecretFilePolicy:
    """Expected POSIX identity and bounded content policy for one secret."""

    expected_uid: int | None = None
    expected_gid: int | None = None
    expected_mode: int = 0o440
    max_bytes: int = 64 * 1024
    enforce_posix: bool = os.name == "posix"

    def __post_init__(self) -> None:
        if self.expected_uid is not None and self.expected_uid < 0:
            raise ValueError("expected secret uid must be non-negative")
        if self.expected_gid is not None and self.expected_gid < 0:
            raise ValueError("expected secret gid must be non-negative")
        if not 0 < self.max_bytes <= 1024 * 1024:
            raise ValueError("secret size limit is invalid")
        if (
            self.expected_mode < 0
            or self.expected_mode > 0o777
            or not self.expected_mode & stat.S_IRUSR
            or self.expected_mode & (stat.S_IXUSR | stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO)
        ):
            raise ValueError("expected secret mode is unsafe")


class SecretFileOperations(Protocol):
    """Injectable filesystem boundary used by portable unit tests."""

    def lstat(self, path: SecretPath) -> os.stat_result: ...

    def open(self, path: SecretPath, flags: int) -> int: ...

    def fstat(self, descriptor: int) -> os.stat_result: ...

    def read(self, descriptor: int, size: int) -> bytes: ...

    def close(self, descriptor: int) -> None: ...


class _SystemSecretFileOperations:
    @staticmethod
    def lstat(path: SecretPath) -> os.stat_result:
        return os.lstat(path)

    @staticmethod
    def open(path: SecretPath, flags: int) -> int:
        return os.open(path, flags)

    @staticmethod
    def fstat(descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    @staticmethod
    def read(descriptor: int, size: int) -> bytes:
        return os.read(descriptor, size)

    @staticmethod
    def close(descriptor: int) -> None:
        os.close(descriptor)


_SYSTEM_OPERATIONS = _SystemSecretFileOperations()


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_object(left, right)
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _reject_symlink(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise SecretFileError("SECRET_FILE_SYMLINK")


def _require_same_snapshot(left: os.stat_result, right: os.stat_result) -> None:
    if not _same_snapshot(left, right):
        raise SecretFileError("SECRET_FILE_CHANGED")


def _validate_metadata(metadata: os.stat_result, policy: SecretFilePolicy) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretFileError("SECRET_FILE_NOT_REGULAR")
    if metadata.st_size < 0 or metadata.st_size > policy.max_bytes:
        raise SecretFileError("SECRET_FILE_TOO_LARGE")
    if not policy.enforce_posix:
        return
    if stat.S_IMODE(metadata.st_mode) != policy.expected_mode:
        raise SecretFileError("SECRET_FILE_MODE_MISMATCH")
    if policy.expected_uid is not None and metadata.st_uid != policy.expected_uid:
        raise SecretFileError("SECRET_FILE_OWNER_MISMATCH")
    if policy.expected_gid is not None and metadata.st_gid != policy.expected_gid:
        raise SecretFileError("SECRET_FILE_GROUP_MISMATCH")


def _read_bounded(
    descriptor: int, *, policy: SecretFilePolicy, operations: SecretFileOperations
) -> bytes:
    content = bytearray()
    while len(content) <= policy.max_bytes:
        chunk = operations.read(descriptor, min(8192, policy.max_bytes + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > policy.max_bytes:
        raise SecretFileError("SECRET_FILE_TOO_LARGE")
    return bytes(content)


def _is_placeholder(content: bytes) -> bool:
    try:
        normalized = content.decode("utf-8").strip().casefold().replace("_", "-")
    except UnicodeDecodeError:
        return False
    return normalized in _PLACEHOLDERS or any(
        normalized.startswith(marker + "-") for marker in _PLACEHOLDERS
    )


def _validate_content(content: bytes) -> None:
    if not content:
        raise SecretFileError("SECRET_FILE_EMPTY")
    if b"\x00" in content:
        raise SecretFileError("SECRET_FILE_CONTAINS_NUL")
    if b"\r" in content or b"\n" in content:
        raise SecretFileError("SECRET_FILE_CONTAINS_NEWLINE")
    if _is_placeholder(content):
        raise SecretFileError("SECRET_FILE_PLACEHOLDER")


def read_secret_file(
    path: SecretPath,
    policy: SecretFilePolicy,
    *,
    operations: SecretFileOperations = _SYSTEM_OPERATIONS,
) -> SensitiveValue[bytes]:
    """Read one validated secret without exposing its path or contents on failure."""

    descriptor: int | None = None
    try:
        before = operations.lstat(path)
        _reject_symlink(before)
        _validate_metadata(before, policy)

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = operations.open(path, flags)
        opened = operations.fstat(descriptor)
        _require_same_snapshot(before, opened)
        _validate_metadata(opened, policy)

        content = _read_bounded(descriptor, policy=policy, operations=operations)
        after_descriptor = operations.fstat(descriptor)
        after_path = operations.lstat(path)
        _require_same_snapshot(opened, after_descriptor)
        _require_same_snapshot(opened, after_path)
        _validate_content(content)
        return SensitiveValue(content)
    except SecretFileError:
        raise
    except OSError:
        raise SecretFileError("SECRET_FILE_UNREADABLE") from None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                operations.close(descriptor)


__all__ = [
    "SecretFileError",
    "SecretFileOperations",
    "SecretFilePolicy",
    "read_secret_file",
]
