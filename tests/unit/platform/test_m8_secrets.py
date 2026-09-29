import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from telegram_userbot.platform.config.secrets import (
    SecretFileError,
    SecretFilePolicy,
    read_secret_file,
)


def metadata(  # noqa: PLR0913 - compact stat fixture builder
    *,
    mode: int = stat.S_IFREG | 0o440,
    inode: int = 7,
    uid: int = 10,
    gid: int = 20,
    size: int = 15,
    mtime: int = 100,
) -> os.stat_result:
    return os.stat_result((mode, inode, 3, 1, uid, gid, size, 0, mtime, mtime))


@dataclass
class FakeOperations:
    payload: bytes
    before: os.stat_result
    opened: os.stat_result | None = None
    after_descriptor: os.stat_result | None = None
    after_path: os.stat_result | None = None
    reads: int = 0
    fstats: int = 0
    lstats: int = 0
    closed: bool = False

    def lstat(self, _path: str | os.PathLike[str]) -> os.stat_result:
        self.lstats += 1
        return self.before if self.lstats == 1 else self.after_path or self.before

    def open(self, _path: str | os.PathLike[str], _flags: int) -> int:
        return 9

    def fstat(self, _descriptor: int) -> os.stat_result:
        self.fstats += 1
        if self.fstats == 1:
            return self.opened or self.before
        return self.after_descriptor or self.opened or self.before

    def read(self, _descriptor: int, _size: int) -> bytes:
        self.reads += 1
        return self.payload if self.reads == 1 else b""

    def close(self, _descriptor: int) -> None:
        self.closed = True


@pytest.mark.unit
def test_secret_file_load_is_redacted_and_portable(tmp_path: Path) -> None:
    path = tmp_path / "provider-key"
    path.write_bytes(b"synthetic-value")

    loaded = read_secret_file(path, SecretFilePolicy(enforce_posix=False))

    assert loaded.reveal_for_use() == b"synthetic-value"
    assert "synthetic-value" not in repr(loaded)
    assert "synthetic-value" not in str(loaded)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "max_bytes", "code"),
    [
        (b"", 64, "SECRET_FILE_EMPTY"),
        (b"changeme", 64, "SECRET_FILE_PLACEHOLDER"),
        (b"example-provider", 64, "SECRET_FILE_PLACEHOLDER"),
        (b"private\x00value", 64, "SECRET_FILE_CONTAINS_NUL"),
        (b"private\nvalue", 64, "SECRET_FILE_CONTAINS_NEWLINE"),
        (b"private\rvalue", 64, "SECRET_FILE_CONTAINS_NEWLINE"),
        (b"too-large", 4, "SECRET_FILE_TOO_LARGE"),
    ],
)
def test_secret_content_failures_are_stable_and_content_free(
    payload: bytes, max_bytes: int, code: str
) -> None:
    stats = metadata(size=min(len(payload), max_bytes))
    operations = FakeOperations(payload, stats)
    private_path = "SYNTHETIC_PRIVATE_SECRET_PATH"

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(
            private_path,
            SecretFilePolicy(max_bytes=max_bytes, enforce_posix=False),
            operations=operations,
        )

    assert captured.value.code == code
    assert private_path not in str(captured.value)
    decoded = payload.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in str(captured.value)
    assert operations.closed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stats", "policy", "code"),
    [
        (
            metadata(mode=stat.S_IFLNK | 0o440),
            SecretFilePolicy(enforce_posix=True),
            "SECRET_FILE_SYMLINK",
        ),
        (
            metadata(mode=stat.S_IFIFO | 0o440),
            SecretFilePolicy(enforce_posix=True),
            "SECRET_FILE_NOT_REGULAR",
        ),
        (
            metadata(mode=stat.S_IFREG | 0o444),
            SecretFilePolicy(enforce_posix=True),
            "SECRET_FILE_MODE_MISMATCH",
        ),
        (
            metadata(uid=99),
            SecretFilePolicy(expected_uid=10, enforce_posix=True),
            "SECRET_FILE_OWNER_MISMATCH",
        ),
        (
            metadata(gid=99),
            SecretFilePolicy(expected_gid=20, enforce_posix=True),
            "SECRET_FILE_GROUP_MISMATCH",
        ),
    ],
)
def test_secret_metadata_policy_rejects_unsafe_files(
    stats: os.stat_result, policy: SecretFilePolicy, code: str
) -> None:
    operations = FakeOperations(b"synthetic-value", stats)

    with pytest.raises(SecretFileError, match=code):
        read_secret_file("private-path", policy, operations=operations)


@pytest.mark.unit
def test_secret_reader_rejects_replacement_and_closes_descriptor() -> None:
    before = metadata()
    operations = FakeOperations(
        b"synthetic-value",
        before,
        after_path=metadata(inode=8),
    )

    with pytest.raises(SecretFileError, match="SECRET_FILE_CHANGED"):
        read_secret_file(
            "private-path",
            SecretFilePolicy(expected_uid=10, expected_gid=20, enforce_posix=True),
            operations=operations,
        )

    assert operations.closed


@pytest.mark.unit
def test_secret_os_errors_are_sanitized() -> None:
    class UnreadableOperations(FakeOperations):
        def open(self, _path: str | os.PathLike[str], _flags: int) -> int:
            raise PermissionError("private-path")

    operations = UnreadableOperations(b"synthetic-value", metadata())

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(
            "private-path",
            SecretFilePolicy(enforce_posix=False),
            operations=operations,
        )

    assert captured.value.code == "SECRET_FILE_UNREADABLE"
    assert "private-path" not in str(captured.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "policy",
    [
        SecretFilePolicy(expected_mode=0o440),
        SecretFilePolicy(expected_mode=0o400),
        SecretFilePolicy(expected_mode=0o600),
        SecretFilePolicy(expected_mode=0o640),
    ],
)
def test_secret_policy_accepts_only_safe_expected_modes(policy: SecretFilePolicy) -> None:
    assert policy.expected_mode in {0o400, 0o440, 0o600, 0o640}


@pytest.mark.unit
@pytest.mark.parametrize("mode", [0o777, 0o444, 0o470, 0o540])
def test_secret_policy_rejects_wide_or_executable_expected_modes(mode: int) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        SecretFilePolicy(expected_mode=mode)
