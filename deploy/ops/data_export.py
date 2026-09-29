"""Create one age-encrypted, account-scoped export without plaintext staging."""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_ROLE = "telegram_userbot_export_runtime"
_LOGIN_ROLE = "telegram_userbot_exporter_login"
_MAX_EXPORT_BYTES = 512 * 1024 * 1024
_MAX_CLEANUP_BATCH = 100
_STAGING_GID = 21015
_LEASE_DURATION = timedelta(minutes=30)
_LEASE_RENEW_INTERVAL_SECONDS = 300.0
_LEASE_RENEW_JOIN_SECONDS = 15.0
_LEASE_RENEW_STATEMENT_TIMEOUT_MS = 10_000
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# Each query has an explicit account predicate.  Contact-scoped requests omit
# account-global audit/proactive rows and only traverse the selected contact.
_EXPORT_QUERIES: tuple[tuple[str, str, bool], ...] = (
    (
        "account",
        "SELECT to_jsonb(q) FROM (SELECT id, telegram_user_id, display_label, status, "
        "default_timezone, created_at, updated_at, deleted_at FROM accounts WHERE id = %s) q",
        False,
    ),
    (
        "contacts",
        "SELECT to_jsonb(q) FROM (SELECT id, account_id, account_peer_id, automation_status, "
        "proactive_enabled, timezone, locale, created_at, updated_at, deleted_at "
        "FROM contacts WHERE account_id = %s "
        "AND (%s::uuid IS NULL OR id = %s::uuid) ORDER BY id) q",
        True,
    ),
    (
        "conversations",
        "SELECT to_jsonb(q) FROM (SELECT id, account_id, contact_id, account_peer_id, "
        "telegram_chat_id, base_mode_override, contact_paused, temporary_human_until, "
        "last_message_at, last_completed_turn_at, created_at, updated_at, deleted_at "
        "FROM conversations WHERE account_id = %s "
        "AND (%s::uuid IS NULL OR contact_id = %s::uuid) ORDER BY id) q",
        True,
    ),
    (
        "account_peers",
        "SELECT to_jsonb(q) FROM (SELECT ap.id, ap.account_id, ap.peer_id, ap.username, "
        "ap.display_name, ap.observed_is_contact, ap.last_observed_at "
        "FROM export_account_peers_v1 ap "
        "WHERE ap.account_id = %s AND (%s::uuid IS NULL OR EXISTS (SELECT 1 FROM contacts c "
        "WHERE c.account_id = ap.account_id AND c.account_peer_id = ap.id AND c.id = %s::uuid)) "
        "ORDER BY ap.id) q",
        True,
    ),
    (
        "telegram_peers",
        "SELECT to_jsonb(q) FROM (SELECT tp.id, tp.peer_type, tp.telegram_peer_id, tp.is_bot, "
        "tp.created_at FROM telegram_peers tp JOIN export_account_peers_v1 ap "
        "ON ap.peer_id = tp.id WHERE ap.account_id = %s AND (%s::uuid IS NULL OR EXISTS "
        "(SELECT 1 FROM contacts c WHERE c.account_id = ap.account_id "
        "AND c.account_peer_id = ap.id AND c.id = %s::uuid)) ORDER BY tp.id) q",
        True,
    ),
    (
        "messages",
        "SELECT to_jsonb(q) FROM (SELECT m.id, m.account_id, m.conversation_id, "
        "m.telegram_message_id, m.sender_account_peer_id, m.direction, m.role, m.source, "
        "m.source_status, m.current_revision_no, m.grouped_id, "
        "m.reply_to_telegram_message_id, m.telegram_created_at, m.edited_at, m.deleted_at, "
        "m.is_tombstone, m.first_observed_at, m.last_observed_at FROM messages m "
        "JOIN conversations c "
        "ON c.id = m.conversation_id AND c.account_id = m.account_id "
        "WHERE m.account_id = %s AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) "
        "ORDER BY m.telegram_created_at, m.id) q",
        True,
    ),
    (
        "message_revisions",
        "SELECT to_jsonb(q) FROM (SELECT mr.id, mr.account_id, mr.message_id, mr.revision_no, "
        "mr.body_kind, mr.text_content, mr.caption, mr.entities_schema_version, mr.entities, "
        "mr.source_event_id, mr.telegram_edited_at, mr.created_at "
        "FROM export_message_revisions_v1 mr "
        "JOIN messages m "
        "ON m.id = mr.message_id AND m.account_id = mr.account_id JOIN conversations c "
        "ON c.id = m.conversation_id AND c.account_id = m.account_id "
        "WHERE mr.account_id = %s AND mr.redacted_at IS NULL "
        "AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) "
        "ORDER BY mr.message_id, mr.revision_no) q",
        True,
    ),
    (
        "message_media",
        "SELECT to_jsonb(q) FROM (SELECT mm.id, mm.account_id, mm.message_revision_id, "
        "mm.media_object_id, mm.media_kind, mm.position, mm.declared_mime, mm.declared_size, "
        "mm.duration_ms, mm.original_name_sanitized, "
        "mm.created_at FROM export_message_media_v1 mm "
        "JOIN export_message_revisions_v1 mr "
        "ON mr.id = mm.message_revision_id AND mr.account_id = mm.account_id JOIN messages m "
        "ON m.id = mr.message_id AND m.account_id = mr.account_id JOIN conversations c "
        "ON c.id = m.conversation_id AND c.account_id = m.account_id "
        "WHERE mm.account_id = %s AND mr.redacted_at IS NULL "
        "AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) "
        "ORDER BY mm.message_revision_id, mm.position) q",
        True,
    ),
    (
        "media_metadata",
        "SELECT to_jsonb(q) FROM (SELECT DISTINCT mo.id, mo.account_id, mo.object_kind, mo.status, "
        "mo.validated_mime, mo.byte_size, mo.width, mo.height, mo.validation_error_code, "
        "mo.created_at, mo.ready_at, mo.deleted_at, mo.retention_class, mo.expires_at "
        "FROM media_objects mo JOIN export_message_media_v1 mm ON mm.media_object_id = mo.id "
        "AND mm.account_id = mo.account_id JOIN export_message_revisions_v1 mr "
        "ON mr.id = mm.message_revision_id AND mr.account_id = mm.account_id JOIN messages m "
        "ON m.id = mr.message_id AND m.account_id = mr.account_id JOIN conversations c "
        "ON c.id = m.conversation_id AND c.account_id = m.account_id "
        "WHERE mo.account_id = %s AND mr.redacted_at IS NULL "
        "AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) "
        "ORDER BY mo.id) q",
        True,
    ),
    (
        "memories",
        "SELECT to_jsonb(q) FROM (SELECT id, account_id, contact_id, conversation_id, "
        "memory_type, status, current_version_no, superseded_by_memory_id, created_at, "
        "updated_at FROM export_memories_v1 WHERE account_id = %s AND status <> 'forgotten' "
        "AND forgotten_at IS NULL "
        "AND (%s::uuid IS NULL OR contact_id = %s::uuid) ORDER BY id) q",
        True,
    ),
    (
        "memory_versions",
        "SELECT to_jsonb(q) FROM (SELECT mv.id, mv.account_id, mv.memory_id, mv.version_no, "
        "mv.operation, mv.payload_schema_version, mv.payload, mv.rendered_text, mv.importance, "
        "mv.confidence, mv.observed_at, mv.valid_from, mv.valid_to, mv.time_precision, "
        "mv.timezone, mv.model_role, mv.prompt_version, mv.validator_policy_version, "
        "mv.acceptance_kind, mv.created_at FROM export_memory_versions_v1 mv "
        "JOIN export_memories_v1 m "
        "ON m.id = mv.memory_id AND m.account_id = mv.account_id WHERE mv.account_id = %s "
        "AND mv.redacted_at IS NULL AND m.status <> 'forgotten' AND m.forgotten_at IS NULL "
        "AND (%s::uuid IS NULL OR m.contact_id = %s::uuid) "
        "ORDER BY mv.memory_id, mv.version_no) q",
        True,
    ),
    (
        "summaries",
        "SELECT to_jsonb(q) FROM (SELECT s.id, s.account_id, s.conversation_id, "
        "s.summary_kind, s.period_key, s.timezone_snapshot, s.period_start_at, s.period_end_at, "
        "s.status, s.current_version_no, s.created_at, s.updated_at FROM summaries s "
        "JOIN conversations c "
        "ON c.id = s.conversation_id AND c.account_id = s.account_id WHERE s.account_id = %s "
        "AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) ORDER BY s.id) q",
        True,
    ),
    (
        "summary_versions",
        "SELECT to_jsonb(q) FROM (SELECT sv.id, sv.account_id, sv.summary_id, sv.version_no, "
        "sv.range_start_event_id, sv.range_end_event_id, sv.period_start_at, sv.period_end_at, "
        "sv.timezone_snapshot, sv.content_text, sv.model_role, sv.prompt_version, "
        "sv.pipeline_version, sv.output_schema_version, sv.invalidation_state, sv.created_at "
        "FROM export_summary_versions_v1 sv JOIN summaries s "
        "ON s.id = sv.summary_id AND s.account_id = sv.account_id JOIN conversations c "
        "ON c.id = s.conversation_id AND c.account_id = s.account_id WHERE sv.account_id = %s "
        "AND sv.redacted_at IS NULL AND (%s::uuid IS NULL OR c.contact_id = %s::uuid) "
        "ORDER BY sv.summary_id, sv.version_no) q",
        True,
    ),
    (
        "audit",
        "SELECT to_jsonb(q) FROM (SELECT occurred_at, actor_type, action, target_type, result, "
        "reason_code, request_id FROM audit_log "
        "WHERE account_id = %s ORDER BY id) q",
        False,
    ),
    (
        "proactive_transitions",
        "SELECT to_jsonb(q) FROM (SELECT id, account_id, candidate_id, from_state, to_state, "
        "event, reason, actor, created_at FROM proactive_state_transitions "
        "WHERE account_id = %s ORDER BY created_at, id) q",
        False,
    ),
)


def _fail(code: str) -> int:
    print(f"DATA_EXPORT_FAILED:{code}", file=sys.stderr)
    return 2


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SECRET_INVALID")
    raw = path.read_bytes()
    if not 32 <= len(raw) <= 128 or any(byte in raw for byte in (0, 10, 13)):
        raise ValueError("SECRET_INVALID")
    return raw.decode("ascii")


def _validated_staging(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("STAGING_INVALID")
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if info.st_uid != 0 or info.st_gid != _STAGING_GID or info.st_mode & 0o007:
        raise ValueError("STAGING_PERMISSION_INVALID")
    if not info.st_mode & stat.S_IWGRP:
        raise ValueError("STAGING_PERMISSION_INVALID")
    return resolved


def _validated_recipient(path: Path) -> Path:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 4096:
        raise ValueError("AGE_RECIPIENT_INVALID")
    resolved = path.resolve(strict=True)
    try:
        lines = tuple(
            line.strip()
            for line in resolved.read_text(encoding="ascii").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except UnicodeError:
        raise ValueError("AGE_RECIPIENT_INVALID") from None
    if len(lines) != 1 or not lines[0].startswith(("age1", "ssh-")):
        raise ValueError("AGE_RECIPIENT_INVALID")
    if len(lines[0]) > 1024 or any(character.isspace() for character in lines[0]):
        raise ValueError("AGE_RECIPIENT_INVALID")
    return resolved


def _database_connection(
    *,
    application_name: str = "telegram_userbot_data_export",
    statement_timeout_ms: int = 300_000,
) -> psycopg.Connection[dict[str, Any]]:
    if (
        application_name
        not in {
            "telegram_userbot_data_export",
            "telegram_userbot_data_export_lease",
        }
        or not 1_000 <= statement_timeout_ms <= 300_000
    ):
        raise ValueError("DATABASE_CONFIG_INVALID")
    host = os.environ.get("DATABASE_HOST", "")
    database = os.environ.get("DATABASE_NAME", "")
    user = os.environ.get("DATABASE_USER", "")
    runtime_role = os.environ.get("DATABASE_RUNTIME_ROLE", "")
    if (
        host != "postgres"
        or database != "telegram_userbot"
        or user != _LOGIN_ROLE
        or runtime_role != _ROLE
    ):
        raise ValueError("DATABASE_CONFIG_INVALID")
    try:
        port = int(os.environ.get("DATABASE_PORT", ""))
    except ValueError:
        raise ValueError("DATABASE_CONFIG_INVALID") from None
    if port != 5432:
        raise ValueError("DATABASE_CONFIG_INVALID")
    password_file = Path(os.environ.get("DATABASE_PASSWORD_FILE", ""))
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=_read_secret(password_file),
        application_name=application_name,
        connect_timeout=10,
        options=(
            f"-c role={_ROLE} -c statement_timeout={statement_timeout_ms} -c lock_timeout=5000"
        ),
        row_factory=dict_row,
    )


def _lease_connection() -> psycopg.Connection[dict[str, Any]]:
    """Use a short-lived control connection outside the read-only export transaction."""

    return _database_connection(
        application_name="telegram_userbot_data_export_lease",
        statement_timeout_ms=_LEASE_RENEW_STATEMENT_TIMEOUT_MS,
    )


def _claim(
    connection: psycopg.Connection[dict[str, Any]], request_id: UUID, owner_id: UUID
) -> dict[str, Any]:
    now = datetime.now(UTC)
    lease = now + _LEASE_DURATION
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE data_export_requests
            SET state = 'claimed', owner_instance_id = %s, lease_expires_at = %s,
                attempt_count = attempt_count + 1, version = version + 1
            WHERE id = %s AND expires_at > %s AND (
              state = 'requested' OR (state = 'claimed' AND lease_expires_at <= %s)
            )
            RETURNING id, account_id, contact_id, attempt_count, version
            """,
            (owner_id, lease, request_id, now, now),
        ).fetchone()
    if row is None:
        raise ValueError("REQUEST_NOT_CLAIMABLE")
    return row


def _claim_next(
    connection: psycopg.Connection[dict[str, Any]], owner_id: UUID
) -> dict[str, Any] | None:
    """Claim one due request without a privileged host-side database query."""

    now = datetime.now(UTC)
    lease = now + _LEASE_DURATION
    with connection.transaction():
        return connection.execute(
            """
            WITH candidate AS (
              SELECT id FROM data_export_requests
              WHERE expires_at > %s AND (
                state = 'requested' OR (state = 'claimed' AND lease_expires_at <= %s)
              )
              ORDER BY created_at, id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE data_export_requests AS request
            SET state = 'claimed', owner_instance_id = %s, lease_expires_at = %s,
                attempt_count = request.attempt_count + 1, version = request.version + 1
            FROM candidate
            WHERE request.id = candidate.id
            RETURNING request.id, request.account_id, request.contact_id, request.attempt_count,
                      request.version
            """,
            (now, now, owner_id, lease),
        ).fetchone()


def _renew(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    request_id: UUID,
    owner_id: UUID,
    expected_version: int,
) -> int | None:
    """Advance a still-owned export lease and return its next fencing version."""

    now = datetime.now(UTC)
    lease = now + _LEASE_DURATION
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE data_export_requests
            SET lease_expires_at = %s, version = version + 1
            WHERE id = %s AND state = 'claimed' AND owner_instance_id = %s
              AND version = %s AND lease_expires_at > %s AND expires_at > %s
            RETURNING version
            """,
            (lease, request_id, owner_id, expected_version, now, now),
        ).fetchone()
    if row is None:
        return None
    version = row["version"]
    if type(version) is not int or version != expected_version + 1:
        raise RuntimeError("LEASE_RENEW_ROW_INVALID")
    return version


class ExportLeaseLostError(RuntimeError):
    """The claim was reclaimed, expired, or could no longer be verified."""


class _ArtifactLock:
    """One persistent inode per request, shared by writers and physical cleanup.

    Never unlink lock files: a second inode could allow two simultaneous owners.
    The private staging directory is validated before constructing this lock.
    """

    def __init__(self, staging: Path, request_id: UUID) -> None:
        self.path = staging / f".{request_id}.export.lock"
        self.fd: int | None = None

    def acquire(self) -> bool:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if self.path.is_symlink():
            raise ValueError("ARTIFACT_LOCK_INVALID")
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("ARTIFACT_LOCK_INVALID")  # noqa: TRY301
            if sys.platform == "win32":
                if info.st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        except BaseException:
            os.close(descriptor)
            raise
        self.fd = descriptor
        return True

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _ExportLeaseGuard:
    """Renew a claim from a separate connection and retain its current fence."""

    def __init__(
        self,
        *,
        request_id: UUID,
        owner_id: UUID,
        expected_version: int,
        connection_factory: Callable[[], psycopg.Connection[dict[str, Any]]] = _lease_connection,
        renew_interval_seconds: float = _LEASE_RENEW_INTERVAL_SECONDS,
    ) -> None:
        if expected_version < 1 or not 1 <= renew_interval_seconds <= 1_800:
            raise ValueError("LEASE_GUARD_INVALID")
        self._request_id = request_id
        self._owner_id = owner_id
        self._connection_factory = connection_factory
        self._renew_interval_seconds = renew_interval_seconds
        self._version = expected_version
        self._version_lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def owns_claim(self) -> bool:
        return not self._lost.is_set()

    @property
    def expected_version(self) -> int:
        with self._version_lock:
            return self._version

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("LEASE_GUARD_ALREADY_STARTED")
        self._thread = threading.Thread(
            target=self._run,
            name=f"data-export-lease-{self._request_id}",
            daemon=True,
        )
        self._thread.start()

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise ExportLeaseLostError("EXPORT_LEASE_LOST")

    def mark_lost(self) -> None:
        self._lost.set()

    def finalize_version(self) -> int:
        """Stop the renewer and obtain one final, current lease fence."""

        self.stop()
        self.assert_owned()
        if not self._renew_once():
            raise ExportLeaseLostError("EXPORT_LEASE_LOST")
        self.assert_owned()
        return self.expected_version

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_LEASE_RENEW_JOIN_SECONDS)
            if thread.is_alive():
                self._lost.set()

    def _run(self) -> None:
        while not self._stop.wait(self._renew_interval_seconds):
            if not self._renew_once():
                return

    def _renew_once(self) -> bool:
        if self._lost.is_set():
            return False
        expected_version = self.expected_version
        connection: psycopg.Connection[dict[str, Any]] | None = None
        try:
            connection = self._connection_factory()
            renewed_version = _renew(
                connection,
                request_id=self._request_id,
                owner_id=self._owner_id,
                expected_version=expected_version,
            )
        except Exception:
            self._lost.set()
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    self._lost.set()
        if self._lost.is_set():
            return False
        if renewed_version is None:
            self._lost.set()
            return False
        with self._version_lock:
            if self._version != expected_version or renewed_version != expected_version + 1:
                self._lost.set()
                return False
            self._version = renewed_version
        return True


def _iter_export_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    request_id: UUID,
    account_id: UUID,
    contact_id: UUID | None,
) -> Iterator[bytes]:
    header = {
        "format": "telegram-userbot-data-export-jsonl",
        "format_version": 1,
        "request_id": str(request_id),
        "account_id": str(account_id),
        "contact_id": None if contact_id is None else str(contact_id),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    yield json.dumps({"kind": "header", "value": header}, separators=(",", ":")).encode() + b"\n"
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for section, query, contact_scoped in _EXPORT_QUERIES:
            if contact_id is not None and not contact_scoped and section != "account":
                continue
            parameters: tuple[object, ...]
            if "%s::uuid" in query:
                parameters = (account_id, contact_id, contact_id)
            else:
                parameters = (account_id,)
            with connection.cursor(name=f"export_{section}") as cursor:
                cursor.itersize = 256
                cursor.execute(query, parameters)
                for row in cursor:
                    payload = row["to_jsonb"]
                    yield (
                        json.dumps(
                            {"kind": "record", "section": section, "value": payload},
                            default=str,
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )


def _artifact_paths(
    *,
    request_id: UUID,
    attempt_count: int,
    staging: Path,
) -> tuple[Path, Path]:
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("ARTIFACT_ATTEMPT_INVALID")
    suffix = f"{request_id}.{attempt_count}.jsonl.age"
    return staging / f".{suffix}.tmp", staging / suffix


def _encrypt_export(  # noqa: PLR0913, PLR0915 - stream, process, and lease boundaries stay explicit
    *,
    rows: Iterator[bytes],
    request_id: UUID,
    attempt_count: int,
    staging: Path,
    recipient: Path,
    lease_guard: _ExportLeaseGuard,
) -> tuple[Path, bytes]:
    temporary, final = _artifact_paths(
        request_id=request_id,
        attempt_count=attempt_count,
        staging=staging,
    )
    if final.exists() or final.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ValueError("ARTIFACT_ALREADY_EXISTS")
    lease_guard.assert_owned()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process: subprocess.Popen[bytes] | None = None
    plaintext_bytes = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            process = subprocess.Popen(  # noqa: S603 - fixed binary in sealed image
                ["age", "-R", str(recipient)],  # noqa: S607 - fixed binary in sealed image
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            if process.stdin is None:
                raise RuntimeError("AGE_PIPE_INVALID")  # noqa: TRY301
            try:
                for row in rows:
                    lease_guard.assert_owned()
                    plaintext_bytes += len(row)
                    if plaintext_bytes > _MAX_EXPORT_BYTES:
                        raise ValueError("EXPORT_SIZE_LIMIT_EXCEEDED")
                    process.stdin.write(row)
            finally:
                process.stdin.close()
            while True:
                try:
                    return_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    lease_guard.assert_owned()
                    continue
                break
            if return_code != 0:
                raise RuntimeError("AGE_ENCRYPTION_FAILED")  # noqa: TRY301
            lease_guard.assert_owned()
            output.flush()
            os.fsync(output.fileno())
        size = temporary.stat().st_size
        if not 1 <= size <= _MAX_EXPORT_BYTES:
            raise ValueError("ARTIFACT_SIZE_INVALID")  # noqa: TRY301
        digest_builder = hashlib.sha256()
        with temporary.open("rb") as encrypted:
            while chunk := encrypted.read(1024 * 1024):
                lease_guard.assert_owned()
                digest_builder.update(chunk)
        digest = digest_builder.digest()
        lease_guard.assert_owned()
        temporary.replace(final)
        return final, digest  # noqa: TRY300
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


def _artifact_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _artifact_digest_fd(file_descriptor: int) -> bytes:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(file_descriptor), "rb", closefd=True) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _same_artifact_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _delete_artifact_at(
    path: Path,
    *,
    expected_digest: bytes,
    directory_fd: int,
) -> None:
    open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path.name, open_flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("ARTIFACT_INVALID") from error
        raise
    try:
        initial = os.fstat(file_descriptor)
        if not stat.S_ISREG(initial.st_mode) or not 1 <= initial.st_size <= _MAX_EXPORT_BYTES:
            raise ValueError("ARTIFACT_INVALID")
        if not hmac.compare_digest(_artifact_digest_fd(file_descriptor), expected_digest):
            raise ValueError("ARTIFACT_DIGEST_MISMATCH")
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_artifact_file(initial, current):
            raise ValueError("ARTIFACT_CHANGED")
        os.unlink(path.name, dir_fd=directory_fd)
    finally:
        os.close(file_descriptor)


def _delete_artifact(path: Path, *, expected_digest: bytes) -> None:
    supports_directory_fd = (
        os.name == "posix"
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
    if supports_directory_fd:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory_fd = os.open(path.parent, directory_flags)
        except FileNotFoundError:
            return
        try:
            _delete_artifact_at(
                path,
                expected_digest=expected_digest,
                directory_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        return
    if path.is_symlink():
        raise ValueError("ARTIFACT_INVALID")
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if not path.is_file() or not 1 <= info.st_size <= _MAX_EXPORT_BYTES:
        raise ValueError("ARTIFACT_INVALID")
    if not hmac.compare_digest(_artifact_digest(path), expected_digest):
        raise ValueError("ARTIFACT_DIGEST_MISMATCH")
    path.unlink()


def _delete_stale_temporary_at(
    path: Path,
    *,
    cutoff: float,
    directory_fd: int,
) -> None:
    """Remove one stale temporary while keeping the directory entry stable.

    Cleanup runs against a directory that can be written by the export worker.
    Open the entry with ``O_NOFOLLOW``, inspect that descriptor, then compare a
    no-following directory stat immediately before unlinking.  This prevents a
    concurrently replaced temporary from turning the cleanup pass into an
    arbitrary-path delete.
    """

    open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path.name, open_flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("ARTIFACT_INVALID") from error
        raise
    try:
        initial = os.fstat(file_descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("ARTIFACT_INVALID")
        if initial.st_mtime > cutoff:
            return
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_artifact_file(initial, current):
            raise ValueError("ARTIFACT_CHANGED")
        os.unlink(path.name, dir_fd=directory_fd)
    finally:
        os.close(file_descriptor)


def _delete_stale_temporary(path: Path, *, cutoff: float) -> None:
    """Best-effort fallback for platforms without directory-fd operations."""

    if path.is_symlink():
        raise ValueError("ARTIFACT_INVALID")
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if not path.is_file():
        raise ValueError("ARTIFACT_INVALID")
    if info.st_mtime > cutoff:
        return
    path.unlink()


def _finalize(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    request_id: UUID,
    owner_id: UUID,
    expected_version: int,
    digest: bytes,
) -> bool:
    now = datetime.now(UTC)
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE data_export_requests
            SET state = 'completed', owner_instance_id = NULL, lease_expires_at = NULL,
                completed_at = %s, artifact_sha256 = %s, version = version + 1
            WHERE id = %s AND state = 'claimed' AND owner_instance_id = %s
              AND version = %s AND lease_expires_at > %s AND expires_at > %s
            RETURNING id
            """,
            (now, digest, request_id, owner_id, expected_version, now, now),
        ).fetchone()
    return row is not None


def _record_failure(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    request_id: UUID,
    owner_id: UUID,
    expected_version: int,
) -> bool:
    now = datetime.now(UTC)
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE data_export_requests
            SET state = 'failed', owner_instance_id = NULL, lease_expires_at = NULL,
                completed_at = %s, last_error_code = 'EXPORT_OPERATION_FAILED',
                version = version + 1
            WHERE id = %s AND state = 'claimed' AND owner_instance_id = %s
              AND version = %s AND lease_expires_at > %s AND expires_at > %s
            RETURNING id
            """,
            (now, request_id, owner_id, expected_version, now, now),
        ).fetchone()
    return row is not None


def run(request_id: UUID | None) -> bool:  # noqa: PLR0912 - claim, lease and file lock cleanup
    deployment_id = os.environ.get("DEPLOYMENT_ID", "")
    if _TOKEN.fullmatch(deployment_id) is None:
        raise ValueError("DEPLOYMENT_ID_INVALID")
    staging = _validated_staging(Path(os.environ.get("EXPORT_STAGING_DIR", "")))
    recipient = _validated_recipient(Path(os.environ.get("AGE_RECIPIENT_FILE", "")))
    owner_id = uuid4()
    connection = _database_connection()
    artifact: Path | None = None
    claim: dict[str, Any] | None = None
    lease_guard: _ExportLeaseGuard | None = None
    artifact_lock: _ArtifactLock | None = None
    try:
        claim = (
            _claim_next(connection, owner_id)
            if request_id is None
            else _claim(connection, request_id, owner_id)
        )
        if claim is None:
            return False
        claimed_request_id = claim["id"]
        attempt_count = claim["attempt_count"]
        claim_version = claim["version"]
        if (
            not isinstance(claimed_request_id, UUID)
            or type(attempt_count) is not int
            or attempt_count < 1
            or type(claim_version) is not int
            or claim_version < 1
        ):
            raise RuntimeError("CLAIM_ROW_INVALID")  # noqa: TRY301
        artifact_lock = _ArtifactLock(staging, claimed_request_id)
        if not artifact_lock.acquire():
            raise ExportLeaseLostError("EXPORT_ARTIFACT_BUSY")  # noqa: TRY301
        lease_guard = _ExportLeaseGuard(
            request_id=claimed_request_id,
            owner_id=owner_id,
            expected_version=claim_version,
            connection_factory=_lease_connection,
        )
        lease_guard.start()
        # Cleanup may have won between claiming and acquiring the filesystem lock.
        # Verify the durable fence under the lock before taking a snapshot or writing.
        lease_guard.finalize_version()
        artifact, digest = _encrypt_export(
            rows=_iter_export_rows(
                connection,
                request_id=claimed_request_id,
                account_id=claim["account_id"],
                contact_id=claim["contact_id"],
            ),
            request_id=claimed_request_id,
            attempt_count=attempt_count,
            staging=staging,
            recipient=recipient,
            lease_guard=lease_guard,
        )
        expected_version = lease_guard.finalize_version()
        if not _finalize(
            connection,
            request_id=claimed_request_id,
            owner_id=owner_id,
            expected_version=expected_version,
            digest=digest,
        ):
            lease_guard.mark_lost()
            lease_guard.assert_owned()
    except BaseException as error:
        if lease_guard is not None:
            lease_guard.stop()
        if artifact is not None:
            artifact.unlink(missing_ok=True)
        if (
            claim is not None
            and not isinstance(error, ExportLeaseLostError)
            and (lease_guard is None or lease_guard.owns_claim)
        ):
            _record_failure(
                connection,
                request_id=claim["id"],
                owner_id=owner_id,
                expected_version=(
                    claim["version"] if lease_guard is None else lease_guard.expected_version
                ),
            )
        raise
    finally:
        if lease_guard is not None:
            lease_guard.stop()
        try:
            connection.close()
        finally:
            if artifact_lock is not None:
                artifact_lock.close()
    return True


def _erase_request_files(staging: Path, row: dict[str, Any]) -> None:
    """Called under the request lock after a durable erasure revokes every writer."""
    request_id = row["id"]
    attempt = row["attempt_count"]
    if not isinstance(request_id, UUID) or type(attempt) is not int or attempt < 0:
        raise RuntimeError("CLEANUP_ROW_INVALID")
    digest = row["artifact_sha256"]
    if digest is not None:
        if not isinstance(digest, (bytes, bytearray, memoryview)) or len(digest) != 32:
            raise RuntimeError("CLEANUP_ROW_INVALID")
        _, artifact = _artifact_paths(request_id=request_id, attempt_count=attempt, staging=staging)
        _delete_artifact(artifact, expected_digest=bytes(digest))
    # A crash between rename and DB finalization leaves an unregistered final.
    # Reclaimed attempts and legacy request-only artifacts belong to the same scope.
    pattern = re.compile(rf"\.?{request_id}(?:\.[1-9][0-9]*)?\.jsonl\.age(?:\.tmp)?\Z")
    directory_fd = None
    if os.name == "posix":
        directory_fd = os.open(
            staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    try:
        for path in staging.iterdir():
            if pattern.fullmatch(path.name) is None:
                continue
            if directory_fd is None:
                _delete_stale_temporary(path, cutoff=float("inf"))
            else:
                _delete_stale_temporary_at(path, cutoff=float("inf"), directory_fd=directory_fd)
        if directory_fd is not None:
            os.fsync(directory_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _cleanup_erased(
    connection: psycopg.Connection[dict[str, Any]], staging: Path, now: datetime
) -> int:
    rows = connection.execute(
        """SELECT id, artifact_sha256, attempt_count, version FROM data_export_requests
           WHERE erasure_requested_at IS NOT NULL AND erasure_cleaned_at IS NULL
           ORDER BY erasure_requested_at, id LIMIT %s""",
        (_MAX_CLEANUP_BATCH,),
    ).fetchall()
    removed = 0
    for row in rows:
        if not isinstance(row["id"], UUID):
            raise TypeError("CLEANUP_ROW_INVALID")
        lock = _ArtifactLock(staging, row["id"])
        if not lock.acquire():
            continue
        try:
            _erase_request_files(staging, row)
            with connection.transaction():
                updated = connection.execute(
                    """UPDATE data_export_requests SET erasure_cleaned_at =
                           GREATEST(%s, clock_timestamp(), erasure_requested_at),
                         artifact_deleted_at = CASE WHEN state = 'completed'
                           THEN COALESCE(artifact_deleted_at, GREATEST(%s, clock_timestamp(),
                             completed_at)) ELSE artifact_deleted_at END,
                         version = version + 1
                       WHERE id = %s AND version = %s AND erasure_requested_at IS NOT NULL
                         AND erasure_cleaned_at IS NULL AND state NOT IN ('requested','claimed')
                       RETURNING id""",
                    (now, now, row["id"], row["version"]),
                ).fetchone()
            # A concurrent cleaner can have already acknowledged the same deletion.
            if updated is not None:
                removed += 1
        finally:
            lock.close()
    return removed


def _cleanup_stale_locked(path: Path, *, cutoff: float, directory_fd: int | None) -> None:
    match = re.fullmatch(r"\.([0-9a-f-]{36})\.[1-9][0-9]*\.jsonl\.age\.tmp", path.name)
    if match is None:
        return
    lock = _ArtifactLock(path.parent, UUID(match[1]))
    if not lock.acquire():
        return
    try:
        if directory_fd is None:
            _delete_stale_temporary(path, cutoff=cutoff)
        else:
            _delete_stale_temporary_at(path, cutoff=cutoff, directory_fd=directory_fd)
    finally:
        lock.close()


def cleanup_due() -> int:
    """Delete expired encrypted artifacts and mark their durable rows idempotently."""

    deployment_id = os.environ.get("DEPLOYMENT_ID", "")
    if _TOKEN.fullmatch(deployment_id) is None:
        raise ValueError("DEPLOYMENT_ID_INVALID")
    staging = _validated_staging(Path(os.environ.get("EXPORT_STAGING_DIR", "")))
    connection = _database_connection()
    removed = 0
    try:
        now = datetime.now(UTC)
        rows = connection.execute(
            """
            SELECT id, artifact_sha256, attempt_count, version
            FROM data_export_requests
            WHERE state = 'completed' AND artifact_deleted_at IS NULL
              AND expires_at <= %s AND erasure_requested_at IS NULL
            ORDER BY expires_at, id
            LIMIT %s
            """,
            (now, _MAX_CLEANUP_BATCH),
        ).fetchall()
        for row in rows:
            request_id = row["id"]
            raw_digest = row["artifact_sha256"]
            attempt_count = row["attempt_count"]
            if (
                not isinstance(request_id, UUID)
                or not isinstance(raw_digest, (bytes, bytearray, memoryview))
                or type(attempt_count) is not int
                or attempt_count < 1
            ):
                raise RuntimeError("CLEANUP_ROW_INVALID")
            digest = bytes(raw_digest)
            if len(digest) != 32:
                raise RuntimeError("CLEANUP_ROW_INVALID")
            _temporary, artifact = _artifact_paths(
                request_id=request_id,
                attempt_count=attempt_count,
                staging=staging,
            )
            _delete_artifact(artifact, expected_digest=digest)
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE data_export_requests
                    SET artifact_deleted_at = %s, version = version + 1
                    WHERE id = %s AND state = 'completed'
                      AND artifact_deleted_at IS NULL AND expires_at <= %s
                      AND version = %s AND artifact_sha256 = %s
                    RETURNING id
                    """,
                    (now, request_id, now, row["version"], digest),
                ).fetchone()
            if updated is None:
                raise RuntimeError("CLEANUP_CAS_FAILED")
            removed += 1

        removed += _cleanup_erased(connection, staging, now)
        # Only encrypted temporary output exists; plaintext is streamed directly to age.
        # The request lock also protects a healthy exporter renewing a long-lived lease.
        cutoff = now.timestamp() - 7200
        temporary_paths = staging.glob(".*.jsonl.age.tmp")
        supports_directory_fd = (
            os.name == "posix"
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd
            and hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
        )
        if supports_directory_fd:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                directory_fd = os.open(staging, directory_flags)
            except FileNotFoundError:
                return removed
            try:
                for temporary in temporary_paths:
                    _cleanup_stale_locked(
                        temporary,
                        cutoff=cutoff,
                        directory_fd=directory_fd,
                    )
            finally:
                os.close(directory_fd)
        else:
            for temporary in temporary_paths:
                _cleanup_stale_locked(temporary, cutoff=cutoff, directory_fd=None)
        return removed
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--request-id", type=UUID)
    operation.add_argument("--claim-next", action="store_true")
    operation.add_argument("--cleanup-due", action="store_true")
    operation.add_argument("--maintenance", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cleanup_due or args.maintenance:
            removed = cleanup_due()
            print(f"DATA_EXPORT_CLEANUP_COMPLETE:{removed}")
            if args.cleanup_due:
                return 0
        if args.request_id is None:
            if not args.claim_next and not args.maintenance:
                raise ValueError("REQUEST_ID_MISSING")  # noqa: TRY301
            if not run(None):
                print("DATA_EXPORT_NO_WORK")
                return 0
        else:
            run(args.request_id)
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        psycopg.Error,
        subprocess.SubprocessError,
    ):
        return _fail("OPERATION_FAILED")
    print("DATA_EXPORT_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
