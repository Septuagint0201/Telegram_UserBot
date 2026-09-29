"""Fail-closed restore gate, independent erasure replay, and local integrity checks."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto.credentials import (
    CredentialBinding,
    CredentialEnvelope,
    parse_credential_keyring,
)

_DEPLOYMENT = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_HEX_32 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LEDGER_BYTES = 16 * 1024 * 1024
_MAX_LEDGER_ENTRIES = 100_000
_EXPECTED_SESSION_TABLES = frozenset(
    {"entities", "sent_files", "sessions", "update_state", "version"}
)
_SESSION_FILENAME = "account.session"
_OPS_STATE_GID = 21016

type LedgerEntry = dict[str, object]
type LedgerScopeReplayer = Callable[
    [psycopg.Connection[dict[str, Any]], UUID, bytes, tuple[LedgerEntry, ...]], None
]


@dataclass(frozen=True, slots=True)
class LedgerContract:
    """A parser contract; scope semantics are supplied by explicit replay handlers."""

    schema_version: int
    header_fields: frozenset[str]
    entry_fields: frozenset[str]


_LEDGER_CONTRACTS: Mapping[int, LedgerContract] = {
    1: LedgerContract(
        schema_version=1,
        header_fields=frozenset(
            {
                "kind",
                "schema_version",
                "deployment_id",
                "account_scope_hmac",
                "snapshot_id",
                "exported_at",
            }
        ),
        entry_fields=frozenset(
            {
                "kind",
                "request_id",
                "scope_type",
                "target_scope_hmac",
                "request_idempotency_key",
                "policy_version",
                "completed_at",
            }
        ),
    )
}


def _fail(code: str) -> int:
    print(f"RESTORE_GATE_FAILED:{code}", file=sys.stderr)
    return 2


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _read_file(path: Path, *, minimum: int, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("INPUT_FILE_INVALID")
    raw = path.read_bytes()
    if not minimum <= len(raw) <= maximum or b"\x00" in raw:
        raise ValueError("INPUT_FILE_INVALID")
    return raw


def _load_identity(path: Path) -> tuple[str, UUID, str]:
    try:
        document = json.loads(
            _read_file(path, minimum=1, maximum=1_048_576).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
        deployment_id = document["deployment_id"]
        account_id = UUID(document["runtime_identity"]["account_id"])
        schema_revision = document["database_compatibility"]["schema_revision"]
    except KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError:
        raise ValueError("DEPLOYMENT_CONFIG_INVALID") from None
    if (
        not isinstance(deployment_id, str)
        or _DEPLOYMENT.fullmatch(deployment_id) is None
        or not isinstance(schema_revision, str)
        or not 1 <= len(schema_revision) <= 64
    ):
        raise ValueError("DEPLOYMENT_CONFIG_INVALID")
    return deployment_id, account_id, schema_revision


def _read_secret(path: Path, *, minimum: int = 32, maximum: int = 65_536) -> bytes:
    raw = _read_file(path, minimum=minimum, maximum=maximum)
    if b"\n" in raw or b"\r" in raw:
        raise ValueError("SECRET_INVALID")
    return raw


def _connection() -> psycopg.Connection[dict[str, Any]]:
    if (
        os.environ.get("DATABASE_HOST") != "postgres"
        or os.environ.get("DATABASE_NAME") != "telegram_userbot"
        or os.environ.get("DATABASE_USER") != "telegram_userbot_migrator_login"
        or os.environ.get("DATABASE_RUNTIME_ROLE") != "telegram_userbot_migrator"
        or os.environ.get("DATABASE_PORT") != "5432"
    ):
        raise ValueError("DATABASE_CONFIG_INVALID")
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname="telegram_userbot",
        user="telegram_userbot_migrator_login",
        password=_read_secret(Path(os.environ.get("DATABASE_PASSWORD_FILE", ""))).decode("ascii"),
        options=(
            "-c role=telegram_userbot_migrator -c statement_timeout=300000 -c lock_timeout=5000"
        ),
        connect_timeout=10,
        application_name="telegram_userbot_restore_gate",
        row_factory=dict_row,
    )


def _reset_gate(
    connection: psycopg.Connection[dict[str, Any]], deployment_id: str, account_id: UUID
) -> None:
    current = connection.execute(
        "SELECT account_id FROM deployment_restore_state WHERE deployment_id = %s FOR UPDATE",
        (deployment_id,),
    ).fetchone()
    if current is not None and current["account_id"] != account_id:
        raise ValueError("RESTORE_GATE_IDENTITY_CONFLICT")
    connection.execute(
        """
        INSERT INTO deployment_restore_state (
          deployment_id, account_id, gate_state, restore_generation,
          erasure_replay_verified, unknown_send_reconciled, credentials_verified,
          session_verified, verified_at, version, updated_at
        ) VALUES (%s, %s, 'validating', 1, false, false, false, false, NULL, 1, now())
        ON CONFLICT (deployment_id) DO UPDATE SET
          gate_state = 'validating',
          restore_generation = deployment_restore_state.restore_generation + 1,
          erasure_replay_verified = false,
          unknown_send_reconciled = false,
          credentials_verified = false,
          session_verified = false,
          verified_at = NULL,
          version = deployment_restore_state.version + 1,
          updated_at = now()
        """,
        (deployment_id, account_id),
    )


def close_gate(*, deployment_id: str, account_id: UUID) -> None:
    with _connection() as connection, connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"telegram-userbot:restore:{deployment_id}",),
        )
        _reset_gate(connection, deployment_id, account_id)


def _load_ledger(  # noqa: PLR0912, PLR0913 - validates durable ledger contract
    path: Path,
    *,
    expected_digest: str,
    deployment_id: str,
    account_id: UUID,
    scope_secret: bytes,
    supported_scopes: frozenset[str] = frozenset({"memory"}),
    contracts: Mapping[int, LedgerContract] = _LEDGER_CONTRACTS,
) -> tuple[str, tuple[LedgerEntry, ...]]:
    if _HEX_32.fullmatch(expected_digest) is None:
        raise ValueError("LEDGER_DIGEST_INVALID")
    raw = _read_file(path, minimum=1, maximum=_MAX_LEDGER_BYTES)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_digest):
        raise ValueError("LEDGER_DIGEST_MISMATCH")
    try:
        records = tuple(
            json.loads(line, object_pairs_hook=_unique_object)
            for line in raw.decode("utf-8").splitlines()
            if line
        )
    except UnicodeError, json.JSONDecodeError, ValueError, RecursionError:
        raise ValueError("LEDGER_FORMAT_INVALID") from None
    if not 1 <= len(records) <= _MAX_LEDGER_ENTRIES + 1:
        raise ValueError("LEDGER_INVENTORY_INVALID")
    header = records[0]
    if not isinstance(header, dict) or type(header.get("schema_version")) is not int:
        raise ValueError("LEDGER_HEADER_INVALID")
    contract = contracts.get(header["schema_version"])
    if contract is None:
        raise ValueError("LEDGER_VERSION_UNSUPPORTED")
    if set(header) != contract.header_fields:
        raise ValueError("LEDGER_HEADER_INVALID")
    expected_account_hmac = hmac.new(scope_secret, account_id.bytes, "sha256").hexdigest()
    if (
        header["kind"] != "header"
        or header["schema_version"] != contract.schema_version
        or header["deployment_id"] != deployment_id
        or not isinstance(header["account_scope_hmac"], str)
        or not hmac.compare_digest(header["account_scope_hmac"], expected_account_hmac)
        or not isinstance(header["snapshot_id"], str)
        or _DEPLOYMENT.fullmatch(header["snapshot_id"]) is None
        or not isinstance(header["exported_at"], str)
    ):
        raise ValueError("LEDGER_HEADER_INVALID")
    entries: list[LedgerEntry] = []
    seen_requests: set[UUID] = set()
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not isinstance(record, dict) or set(record) != contract.entry_fields:
            raise ValueError("LEDGER_ENTRY_INVALID")
        try:
            request_id = UUID(str(record["request_id"]))
            completed_at = datetime.fromisoformat(str(record["completed_at"]))
        except ValueError:
            raise ValueError("LEDGER_ENTRY_INVALID") from None
        target_hmac = record["target_scope_hmac"]
        idempotency_key = record["request_idempotency_key"]
        scope_type = record["scope_type"]
        if not isinstance(scope_type, str) or scope_type not in supported_scopes:
            raise ValueError("LEDGER_SCOPE_UNSUPPORTED")
        if (
            record["kind"] != "erasure"
            or not isinstance(target_hmac, str)
            or _HEX_32.fullmatch(target_hmac) is None
            or not isinstance(idempotency_key, str)
            or _HEX_32.fullmatch(idempotency_key) is None
            or type(record["policy_version"]) is not int
            or not 1 <= record["policy_version"] <= 2**31 - 1
            or completed_at.tzinfo is None
            or request_id in seen_requests
            or idempotency_key in seen_keys
        ):
            raise ValueError("LEDGER_ENTRY_INVALID")
        seen_requests.add(request_id)
        seen_keys.add(idempotency_key)
        entries.append(record)
    return str(header["snapshot_id"]), tuple(entries)


def _ledger_document(
    *,
    deployment_id: str,
    account_id: UUID,
    scope_secret: bytes,
    snapshot_id: str,
    rows: Sequence[Mapping[str, object]],
) -> bytes:
    """Serialize the cumulative ledger without reconstructing any content."""

    if _DEPLOYMENT.fullmatch(deployment_id) is None or _DEPLOYMENT.fullmatch(snapshot_id) is None:
        raise ValueError("LEDGER_EXPORT_ID_INVALID")
    if len(scope_secret) < 32:
        raise ValueError("LEDGER_EXPORT_SECRET_INVALID")
    if account_id.int == 0:
        raise ValueError("LEDGER_EXPORT_ACCOUNT_INVALID")
    if len(rows) > _MAX_LEDGER_ENTRIES:
        raise ValueError("LEDGER_INVENTORY_INVALID")
    expected_account_hmac = hmac.new(scope_secret, account_id.bytes, "sha256").digest()
    records: list[dict[str, object]] = [
        {
            "account_scope_hmac": expected_account_hmac.hex(),
            "deployment_id": deployment_id,
            "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "kind": "header",
            "schema_version": 1,
            "snapshot_id": snapshot_id,
        }
    ]
    seen_requests: set[UUID] = set()
    seen_keys: set[bytes] = set()
    for row in rows:
        row_account_hmac = row.get("account_scope_hmac")
        target_hmac = row.get("target_scope_hmac")
        request_id = row.get("request_id")
        request_key = row.get("request_idempotency_key")
        scope_type = row.get("scope_type")
        policy_version = row.get("policy_version")
        completed_at = row.get("completed_at")
        if (
            not isinstance(row_account_hmac, bytes)
            or len(row_account_hmac) != 32
            or not isinstance(target_hmac, bytes)
            or len(target_hmac) != 32
            or not isinstance(request_id, UUID)
            or not isinstance(request_key, bytes)
            or len(request_key) != 32
            or not isinstance(scope_type, str)
            or scope_type not in {"memory", "contact", "account"}
            or type(policy_version) is not int
            or not 1 <= policy_version <= 2**31 - 1
            or not isinstance(completed_at, datetime)
            or completed_at.tzinfo is None
            or expected_account_hmac != row_account_hmac
            or request_id in seen_requests
            or request_key in seen_keys
        ):
            raise ValueError("LEDGER_EXPORT_ROW_INVALID")
        seen_requests.add(request_id)
        seen_keys.add(request_key)
        records.append(
            {
                "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
                "kind": "erasure",
                "policy_version": policy_version,
                "request_id": str(request_id),
                "request_idempotency_key": request_key.hex(),
                "scope_type": scope_type,
                "target_scope_hmac": target_hmac.hex(),
            }
        )
    return b"".join(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        + b"\n"
        for record in records
    )


def _write_ledger_document(path: Path, payload: bytes) -> None:
    """Atomically replace one export file without following a symlink."""

    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.parent.is_dir()
        or path.parent.resolve() != path.parent
    ):
        raise ValueError("LEDGER_EXPORT_PATH_INVALID")
    if len(payload) > _MAX_LEDGER_BYTES:
        raise ValueError("LEDGER_EXPORT_TOO_LARGE")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o640)
        temporary.replace(path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _export_connection() -> psycopg.Connection[dict[str, Any]]:
    """Use the existing export identity; the restore writer is never mounted here."""

    if any(
        os.environ.get(key) != value
        for key, value in {
            "DATABASE_HOST": "postgres",
            "DATABASE_PORT": "5432",
            "DATABASE_NAME": "telegram_userbot",
            "DATABASE_USER": "telegram_userbot_exporter_login",
            "DATABASE_RUNTIME_ROLE": "telegram_userbot_export_runtime",
            "DATABASE_PASSWORD_FILE": "/run/secrets/export_database_password",
        }.items()
    ):
        raise ValueError("DATABASE_CONFIG_INVALID")
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname="telegram_userbot",
        user="telegram_userbot_exporter_login",
        password=_read_secret(Path("/run/secrets/export_database_password")).decode("ascii"),
        options=(
            "-c role=telegram_userbot_export_runtime -c default_transaction_read_only=on "
            "-c statement_timeout=300000 -c lock_timeout=5000"
        ),
        connect_timeout=10,
        application_name="telegram_userbot_erasure_export",
        row_factory=dict_row,
    )


def export_ledger(
    *,
    deployment_id: str,
    account_id: UUID,
    output_path: Path,
    snapshot_id: str,
) -> int:
    """Export completed erasures for one account for a later restore overlay."""

    scope_secret = _read_secret(Path("/run/secrets/erasure_hmac_key"))
    with _export_connection() as connection:
        # Serialize scheduled/manual exports through the atomic file replacement.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"telegram-userbot:ledger-export:{deployment_id}:{account_id}",),
        )
        if (
            connection.execute("SELECT id FROM accounts WHERE id = %s", (account_id,)).fetchone()
            is None
        ):
            raise ValueError("LEDGER_EXPORT_ACCOUNT_UNKNOWN")
        rows = connection.execute(
            """
            SELECT l.account_scope_hmac, l.scope_type, l.target_scope_hmac,
                   l.request_id, l.policy_version, l.completed_at,
                   r.request_idempotency_key
            FROM erasure_ledger AS l
            JOIN data_erasure_requests AS r ON r.id = l.request_id
            WHERE r.account_id = %s AND r.state = 'completed'
            ORDER BY l.completed_at, l.request_id
            LIMIT %s
            """,
            (account_id, _MAX_LEDGER_ENTRIES + 1),
        ).fetchall()
        payload = _ledger_document(
            deployment_id=deployment_id,
            account_id=account_id,
            scope_secret=scope_secret,
            snapshot_id=snapshot_id,
            rows=rows,
        )
        if output_path.exists() or output_path.is_symlink():
            previous = _read_file(output_path, minimum=1, maximum=_MAX_LEDGER_BYTES)
            _, previous_entries = _load_ledger(
                output_path,
                expected_digest=hashlib.sha256(previous).hexdigest(),
                deployment_id=deployment_id,
                account_id=account_id,
                scope_secret=scope_secret,
                supported_scopes=frozenset({"memory", "contact", "account"}),
            )
            current_entries = {
                entry["request_id"]: entry for entry in map(json.loads, payload.splitlines()[1:])
            }
            if any(current_entries.get(entry["request_id"]) != entry for entry in previous_entries):
                raise ValueError("LEDGER_EXPORT_HISTORY_REGRESSED")
        _write_ledger_document(output_path, payload)
    return len(rows)


def _redact_memory_scope(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    account_id: UUID,
    memory: dict[str, Any],
    completed_at: datetime,
) -> None:
    memory_id = memory["id"]
    connection.execute(
        "UPDATE memories SET status = 'forgotten', forgotten_at = %s, updated_at = %s "
        "WHERE account_id = %s AND id = %s",
        (completed_at, completed_at, account_id, memory_id),
    )
    connection.execute(
        "UPDATE memory_versions SET payload = '{}'::jsonb, rendered_text = NULL, "
        "redacted_at = %s, redaction_reason = 'restore_ledger_overlay' "
        "WHERE account_id = %s AND memory_id = %s",
        (completed_at, account_id, memory_id),
    )
    connection.execute(
        "UPDATE embedding_records SET state = 'invalidated', invalidated_at = %s "
        "WHERE account_id = %s AND memory_version_id IN "
        "(SELECT id FROM memory_versions WHERE account_id = %s AND memory_id = %s)",
        (completed_at, account_id, account_id, memory_id),
    )
    if memory["contact_id"] is not None:
        connection.execute(
            "UPDATE memory_proposals SET proposed_payload = '{}'::jsonb, "
            "proposed_text = NULL, "
            "state = CASE WHEN state = 'accepted' THEN state ELSE 'invalidated' END, "
            "validation_code = CASE WHEN state = 'accepted' THEN validation_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decision_reason_code = CASE WHEN state = 'accepted' THEN decision_reason_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decided_at = CASE WHEN state = 'accepted' THEN decided_at ELSE %s END "
            "WHERE account_id = %s AND contact_id = %s",
            (completed_at, account_id, memory["contact_id"]),
        )
    elif memory["conversation_id"] is not None:
        connection.execute(
            "UPDATE memory_proposals SET proposed_payload = '{}'::jsonb, "
            "proposed_text = NULL, "
            "state = CASE WHEN state = 'accepted' THEN state ELSE 'invalidated' END, "
            "validation_code = CASE WHEN state = 'accepted' THEN validation_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decision_reason_code = CASE WHEN state = 'accepted' THEN decision_reason_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decided_at = CASE WHEN state = 'accepted' THEN decided_at ELSE %s END "
            "WHERE account_id = %s AND conversation_id = %s",
            (completed_at, account_id, memory["conversation_id"]),
        )
    else:
        connection.execute(
            "UPDATE memory_proposals SET proposed_payload = '{}'::jsonb, "
            "proposed_text = NULL, "
            "state = CASE WHEN state = 'accepted' THEN state ELSE 'invalidated' END, "
            "validation_code = CASE WHEN state = 'accepted' THEN validation_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decision_reason_code = CASE WHEN state = 'accepted' THEN decision_reason_code "
            "ELSE 'restore_ledger_overlay' END, "
            "decided_at = CASE WHEN state = 'accepted' THEN decided_at ELSE %s END "
            "WHERE account_id = %s",
            (completed_at, account_id),
        )
    if memory["conversation_id"] is not None:
        conversation_ids = (memory["conversation_id"],)
    elif memory["contact_id"] is not None:
        conversation_ids = tuple(
            row["id"]
            for row in connection.execute(
                "SELECT id FROM conversations WHERE account_id = %s AND contact_id = %s",
                (account_id, memory["contact_id"]),
            ).fetchall()
        )
    else:
        conversation_ids = tuple(
            row["id"]
            for row in connection.execute(
                "SELECT id FROM conversations WHERE account_id = %s", (account_id,)
            ).fetchall()
        )
    if conversation_ids:
        connection.execute(
            "UPDATE summaries SET status = 'invalidated', updated_at = %s "
            "WHERE account_id = %s AND conversation_id = ANY(%s)",
            (completed_at, account_id, list(conversation_ids)),
        )
        connection.execute(
            "UPDATE summary_versions SET content_text = NULL, content_sha256 = NULL, "
            "invalidation_state = 'invalidated', redacted_at = %s WHERE account_id = %s "
            "AND summary_id IN (SELECT id FROM summaries WHERE account_id = %s "
            "AND conversation_id = ANY(%s))",
            (completed_at, account_id, account_id, list(conversation_ids)),
        )
        connection.execute(
            "UPDATE embedding_records SET state = 'invalidated', invalidated_at = %s "
            "WHERE account_id = %s AND summary_version_id IN "
            "(SELECT id FROM summary_versions WHERE account_id = %s AND summary_id IN "
            "(SELECT id FROM summaries WHERE account_id = %s AND conversation_id = ANY(%s)))",
            (completed_at, account_id, account_id, account_id, list(conversation_ids)),
        )


def _replay_memory_entries(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    scope_secret: bytes,
    entries: tuple[LedgerEntry, ...],
) -> None:
    memories = connection.execute(
        "SELECT id, contact_id, conversation_id, scope_erased_at FROM memories "
        "WHERE account_id = %s FOR UPDATE",
        (account_id,),
    ).fetchall()
    by_hmac = {
        hmac.new(scope_secret, row["id"].bytes, "sha256").hexdigest(): row for row in memories
    }
    for entry in entries:
        memory = by_hmac.get(str(entry["target_scope_hmac"]))
        if memory is None:
            continue
        request_id = UUID(str(entry["request_id"]))
        completed_at = datetime.fromisoformat(str(entry["completed_at"]))
        if memory.get("scope_erased_at") is None:
            _redact_memory_scope(
                connection,
                account_id=account_id,
                memory=memory,
                completed_at=completed_at,
            )
        connection.execute(
            """
            INSERT INTO data_erasure_requests (
              id, account_id, scope_type, memory_id, contact_id, state, requested_by,
              request_idempotency_key, policy_version, created_at, updated_at, completed_at
            ) VALUES (%s, %s, 'memory', %s, NULL, 'completed', 'restore_ledger_overlay',
                      %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET state = 'completed', updated_at = EXCLUDED.updated_at,
              completed_at = EXCLUDED.completed_at, last_error_code = NULL
            """,
            (
                request_id,
                account_id,
                memory["id"],
                bytes.fromhex(str(entry["request_idempotency_key"])),
                entry["policy_version"],
                completed_at,
                completed_at,
                completed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO erasure_ledger (
              account_scope_hmac, scope_type, target_scope_hmac, request_id,
              policy_version, completed_at
            ) VALUES (%s, 'memory', %s, %s, %s, %s)
            ON CONFLICT (request_id) DO NOTHING
            """,
            (
                hmac.new(scope_secret, account_id.bytes, "sha256").digest(),
                bytes.fromhex(str(entry["target_scope_hmac"])),
                request_id,
                entry["policy_version"],
                completed_at,
            ),
        )


def _replay_ledger(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    account_id: UUID,
    scope_secret: bytes,
    entries: tuple[LedgerEntry, ...],
    replayers: Mapping[str, LedgerScopeReplayer],
) -> None:
    grouped: dict[str, list[LedgerEntry]] = {scope: [] for scope in replayers}
    for entry in entries:
        scope_type = entry.get("scope_type")
        if not isinstance(scope_type, str) or scope_type not in replayers:
            raise ValueError("LEDGER_SCOPE_UNSUPPORTED")
        grouped[scope_type].append(entry)
    for scope_type in sorted(grouped):
        scoped_entries = grouped[scope_type]
        if scoped_entries:
            replayers[scope_type](
                connection,
                account_id,
                scope_secret,
                tuple(scoped_entries),
            )


def _verify_credentials(connection: psycopg.Connection[dict[str, Any]], deployment_id: str) -> None:
    keyring = parse_credential_keyring(
        SensitiveValue(_read_secret(Path("/run/secrets/credential_master_keyring"), minimum=1)),
        expected_deployment_id=deployment_id,
    )
    rows = connection.execute(
        """
        SELECT profile.logical_role, version.profile_id, version.credential_id,
               version.version_no, version.algorithm, version.key_version,
               version.aad_schema_version, version.nonce, version.ciphertext,
               version.secret_fingerprint
        FROM model_credentials AS credential
        JOIN model_profiles AS profile ON profile.id = credential.profile_id
        JOIN model_credential_versions AS version
          ON version.credential_id = credential.id
         AND version.profile_id = credential.profile_id
         AND version.version_no = credential.active_version_no
        WHERE credential.status = 'active'
        """
    ).fetchall()
    for row in rows:
        plaintext = keyring.decrypt(
            CredentialEnvelope(
                algorithm=row["algorithm"],
                key_version=row["key_version"],
                aad_schema_version=row["aad_schema_version"],
                nonce=bytes(row["nonce"]),
                ciphertext=bytes(row["ciphertext"]),
                secret_fingerprint=bytes(row["secret_fingerprint"]),
            ),
            binding=CredentialBinding(
                logical_role=LogicalRole(row["logical_role"]),
                profile_id=row["profile_id"],
                credential_id=row["credential_id"],
                version_no=row["version_no"],
            ),
        )
        if not plaintext.reveal_for_use():
            raise ValueError("CREDENTIAL_VERIFICATION_FAILED")


def _verify_session() -> None:
    root = Path("/session")
    session = root / _SESSION_FILENAME
    candidates = tuple(root.glob("*.session")) if root.is_dir() and not root.is_symlink() else ()
    if candidates != (session,) or session.is_symlink() or not session.is_file():
        raise ValueError("SESSION_VERIFICATION_FAILED")
    connection = sqlite3.connect(f"file:{session.as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SESSION_VERIFICATION_FAILED")
        tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
        sessions = connection.execute("SELECT auth_key FROM sessions").fetchall()
        if (
            not tables >= _EXPECTED_SESSION_TABLES
            or len(sessions) != 1
            or not isinstance(sessions[0][0], bytes)
            or len(sessions[0][0]) < 128
        ):
            raise ValueError("SESSION_VERIFICATION_FAILED")
    finally:
        connection.close()


def _verify_database_integrity(
    connection: psycopg.Connection[dict[str, Any]], account_id: UUID
) -> None:
    checks = (
        (
            "RESTORE_ACCOUNT_IDENTITY_INVALID",
            "SELECT count(*) AS invalid FROM accounts WHERE id = %s",
            (account_id,),
            1,
        ),
        (
            "RESTORE_CONSTRAINT_STATE_INVALID",
            "SELECT count(*) AS invalid FROM pg_constraint AS c "
            "JOIN pg_namespace AS n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' AND NOT c.convalidated",
            (),
            0,
        ),
        (
            "RESTORE_INDEX_STATE_INVALID",
            "SELECT count(*) AS invalid FROM pg_index AS i "
            "JOIN pg_class AS t ON t.oid = i.indrelid "
            "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND (NOT i.indisvalid OR NOT i.indisready)",
            (),
            0,
        ),
        (
            "RESTORE_MEMORY_INTEGRITY_INVALID",
            "SELECT count(*) AS invalid FROM memories AS m WHERE m.account_id = %s "
            "AND m.status = 'active' AND (m.forgotten_at IS NOT NULL OR NOT EXISTS "
            "(SELECT 1 FROM memory_versions AS v WHERE v.account_id = m.account_id "
            "AND v.memory_id = m.id AND v.version_no = m.current_version_no "
            "AND v.redacted_at IS NULL))",
            (account_id,),
            0,
        ),
        (
            "RESTORE_SUMMARY_INTEGRITY_INVALID",
            "SELECT count(*) AS invalid FROM summaries AS s WHERE s.account_id = %s "
            "AND s.status = 'active' AND NOT EXISTS (SELECT 1 FROM summary_versions AS v "
            "WHERE v.account_id = s.account_id AND v.summary_id = s.id "
            "AND v.version_no = s.current_version_no AND v.redacted_at IS NULL "
            "AND v.invalidation_state = 'active')",
            (account_id,),
            0,
        ),
        (
            "RESTORE_ERASURE_INCOMPLETE",
            "SELECT count(*) AS invalid FROM data_erasure_requests "
            "WHERE account_id = %s AND state <> 'completed'",
            (account_id,),
            0,
        ),
    )
    for code, query, parameters, expected in checks:
        row = connection.execute(query, parameters).fetchone()
        if row is None or row["invalid"] != expected:
            raise ValueError(code)


def _verify_side_effect_reconciliation(
    connection: psycopg.Connection[dict[str, Any]], account_id: UUID
) -> None:
    checks = (
        (
            "OUTBOUND_GROUP_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM outbound_delivery_groups WHERE account_id = %s "
            "AND state IN ('sending','partial','unknown')",
        ),
        (
            "OUTBOUND_INTENT_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM outbound_intents WHERE account_id = %s "
            "AND state IN ('sending','unknown')",
        ),
        (
            "OUTBOUND_ATTEMPT_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM outbound_attempts WHERE account_id = %s "
            "AND state IN ('started','unknown')",
        ),
        (
            "COPILOT_SEND_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM copilot_drafts WHERE account_id = %s "
            "AND state = 'send_unknown'",
        ),
        (
            "PROACTIVE_BUDGET_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM proactive_budget_reservations "
            "WHERE account_id = %s AND state = 'send_unknown'",
        ),
        (
            "PREVIEW_DELIVERY_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM context_preview_requests WHERE account_id = %s "
            "AND state IN ('delivering','send_unknown','delete_partial')",
        ),
        (
            "PREVIEW_MESSAGE_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM context_preview_deliveries AS d "
            "JOIN context_preview_requests AS r ON r.id = d.request_id "
            "WHERE r.account_id = %s AND (d.state IN "
            "('sending','send_unknown','delete_pending','delete_failed') OR "
            "(d.state = 'sent' AND d.delete_after IS NOT NULL AND d.delete_after <= now()))",
        ),
        (
            "MODEL_ATTEMPT_RECONCILIATION_REQUIRED",
            "SELECT count(*) AS pending FROM model_run_attempts AS a "
            "JOIN model_runs AS r ON r.id = a.model_run_id WHERE r.account_id = %s "
            "AND a.state IN ('started','unknown')",
        ),
    )
    for code, query in checks:
        row = connection.execute(query, (account_id,)).fetchone()
        if row is None or row["pending"] != 0:
            raise ValueError(code)

    receipt = connection.execute(
        "SELECT count(*) AS invalid FROM control_bot_update_receipts AS r "
        "JOIN control_bot_cursors AS c ON c.deployment_id = r.deployment_id "
        "AND c.bot_user_id = r.bot_user_id WHERE r.state = 'claimed' "
        "AND (r.update_id < c.next_offset OR r.lease_expires_at > now())"
    ).fetchone()
    if receipt is None or receipt["invalid"] != 0:
        raise ValueError("CONTROL_RECEIPT_LEASE_RECONCILIATION_REQUIRED")


def _write_marker(snapshot_id: str) -> None:
    root = Path("/ops-state")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("OPS_STATE_INVALID")
    info = root.stat()
    if (
        info.st_uid != 0
        or info.st_gid != _OPS_STATE_GID
        or info.st_mode & 0o007
        or not info.st_mode & stat.S_IWGRP
        or not info.st_mode & stat.S_ISGID
    ):
        raise ValueError("OPS_STATE_PERMISSION_INVALID")
    payload = (
        '{"schema_version":1,"kind":"restore-drill","completed_at":"'
        + datetime.now(UTC).isoformat().replace("+00:00", "Z")
        + f'","result":"PASS","ledger_snapshot_id":"{snapshot_id}"}}\n'
    ).encode("ascii")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=root, prefix=".restore-drill.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o640)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(root / "restore-drill.json")
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _resolve_replayers(
    value: Mapping[str, LedgerScopeReplayer] | None,
) -> Mapping[str, LedgerScopeReplayer]:
    replayers: Mapping[str, LedgerScopeReplayer] = (
        {
            "memory": _replay_memory_entries,
            "contact": _verify_scope_entries,
            "account": _verify_scope_entries,
        }
        if value is None
        else dict(value)
    )
    if not replayers or any(
        not isinstance(scope, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", scope) is None
        or not callable(replayer)
        for scope, replayer in replayers.items()
    ):
        raise ValueError("LEDGER_REPLAYER_REGISTRY_INVALID")
    return replayers


def _scope_target(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    scope_secret: bytes,
    entry: LedgerEntry,
) -> UUID | None:
    if entry["scope_type"] == "account":
        if not hmac.compare_digest(
            hmac.digest(scope_secret, account_id.bytes, "sha256").hex(),
            str(entry["target_scope_hmac"]),
        ):
            raise ValueError("LEDGER_TARGET_IDENTITY_INVALID")
        return account_id
    rows = connection.execute(
        "SELECT id FROM contacts WHERE account_id = %s", (account_id,)
    ).fetchall()
    return next(
        (
            row["id"]
            for row in rows
            if hmac.compare_digest(
                hmac.digest(scope_secret, row["id"].bytes, "sha256").hex(),
                str(entry["target_scope_hmac"]),
            )
        ),
        None,
    )


def _stage_scope_entries(  # noqa: PLR0913 - restore generation and scope are explicit
    connection: psycopg.Connection[dict[str, Any]],
    *,
    deployment_id: str,
    account_id: UUID,
    generation: int,
    scope_secret: bytes,
    entries: tuple[LedgerEntry, ...],
) -> None:
    for entry in entries:
        if entry["scope_type"] == "memory":
            _replay_memory_entries(connection, account_id, scope_secret, (entry,))
            continue
        target = _scope_target(connection, account_id, scope_secret, entry)
        if target is None:
            raise ValueError("LEDGER_TARGET_MISSING")
        request_id = UUID(str(entry["request_id"]))
        contact = target if entry["scope_type"] == "contact" else None
        identity = (
            account_id,
            entry["scope_type"],
            contact,
            bytes.fromhex(str(entry["request_idempotency_key"])),
            entry["policy_version"],
        )
        previous = connection.execute(
            "SELECT account_id, scope_type, contact_id, request_idempotency_key, policy_version "
            "FROM data_erasure_requests WHERE id = %s FOR UPDATE",
            (request_id,),
        ).fetchone()
        if previous is not None and tuple(previous.values()) != identity:
            raise ValueError("LEDGER_REQUEST_IDENTITY_CONFLICT")
        if previous is None:
            connection.execute(
                "INSERT INTO data_erasure_requests (id, account_id, scope_type, contact_id, "
                "request_idempotency_key, policy_version, state, requested_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,'requested','restore_ledger_overlay')",
                (request_id, *identity),
            )
        inserted = connection.execute(
            "INSERT INTO erasure_restore_replays (deployment_id,restore_generation,request_id) "
            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id",
            (deployment_id, generation, request_id),
        ).fetchone()
        if inserted is None:
            continue
        # Restore invalidates all old filesystem acknowledgments, even if the
        # backup happened to contain a completed request. Repeated staging in the
        # same generation does not undo work already performed by cleanup owners.
        connection.execute("DELETE FROM erasure_media_checks WHERE request_id = %s", (request_id,))
        connection.execute("DELETE FROM erasure_progress WHERE request_id = %s", (request_id,))
        connection.execute(
            "UPDATE data_erasure_requests SET state='requested',completed_at=NULL,"
            "last_error_code=NULL,updated_at=now() WHERE id=%s",
            (request_id,),
        )
        # The independent ledger retains the historical completion fact while
        # this restore generation must separately prove local cleanup again.
        connection.execute(
            "INSERT INTO erasure_ledger (account_scope_hmac,scope_type,target_scope_hmac,"
            "request_id,policy_version,completed_at) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (request_id) DO NOTHING",
            (
                hmac.digest(scope_secret, account_id.bytes, "sha256"),
                entry["scope_type"],
                bytes.fromhex(str(entry["target_scope_hmac"])),
                request_id,
                entry["policy_version"],
                datetime.fromisoformat(str(entry["completed_at"])),
            ),
        )


def _verify_scope_entries(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    scope_secret: bytes,
    entries: tuple[LedgerEntry, ...],
) -> None:
    for entry in entries:
        target = _scope_target(connection, account_id, scope_secret, entry)
        row = connection.execute(
            "SELECT r.scope_type,r.contact_id,r.state,r.policy_version,r.request_idempotency_key "
            "FROM data_erasure_requests r JOIN erasure_media_checks m ON m.request_id=r.id "
            "JOIN erasure_restore_replays p ON p.request_id=r.id "
            "JOIN deployment_restore_state g ON g.deployment_id=p.deployment_id "
            "AND g.restore_generation=p.restore_generation AND g.account_id=r.account_id "
            "WHERE r.id=%s AND r.account_id=%s AND g.gate_state='validating'",
            (UUID(str(entry["request_id"])), account_id),
        ).fetchone()
        if (
            target is None
            or row is None
            or row
            != {
                "scope_type": entry["scope_type"],
                "contact_id": target if entry["scope_type"] == "contact" else None,
                "state": "completed",
                "policy_version": entry["policy_version"],
                "request_idempotency_key": bytes.fromhex(str(entry["request_idempotency_key"])),
            }
        ):
            raise ValueError("RESTORE_ERASURE_INCOMPLETE")


def stage_erasure_replay(
    *,
    deployment_id: str,
    account_id: UUID,
    schema_revision: str,
    ledger_path: Path,
) -> None:
    secret = _read_secret(Path("/run/secrets/erasure_hmac_key"))
    _, entries = _load_ledger(
        ledger_path,
        expected_digest=os.environ.get("ERASURE_LEDGER_SHA256", ""),
        deployment_id=deployment_id,
        account_id=account_id,
        scope_secret=secret,
        supported_scopes=frozenset({"memory", "contact", "account"}),
    )
    with _connection() as connection, connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"telegram-userbot:restore:{deployment_id}",),
        )
        gate = connection.execute(
            "SELECT account_id,gate_state,restore_generation FROM deployment_restore_state "
            "WHERE deployment_id=%s FOR UPDATE",
            (deployment_id,),
        ).fetchone()
        if gate is None or gate["account_id"] != account_id or gate["gate_state"] != "validating":
            raise ValueError("RESTORE_GATE_NOT_VALIDATING")
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if version is None or version["version_num"] != schema_revision:
            raise ValueError("SCHEMA_REVISION_INVALID")
        _stage_scope_entries(
            connection,
            deployment_id=deployment_id,
            account_id=account_id,
            generation=gate["restore_generation"],
            scope_secret=secret,
            entries=entries,
        )


def verify_and_open(
    *,
    deployment_id: str,
    account_id: UUID,
    schema_revision: str,
    ledger_path: Path,
    ledger_replayers: Mapping[str, LedgerScopeReplayer] | None = None,
) -> None:
    replayers = _resolve_replayers(ledger_replayers)
    scope_secret = _read_secret(Path("/run/secrets/erasure_hmac_key"))
    snapshot_id, entries = _load_ledger(
        ledger_path,
        expected_digest=os.environ.get("ERASURE_LEDGER_SHA256", ""),
        deployment_id=deployment_id,
        account_id=account_id,
        scope_secret=scope_secret,
        supported_scopes=frozenset(replayers),
    )
    _verify_session()
    marker = Path("/ops-state/restore-drill.json")
    marker_written = False
    try:
        with _connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"telegram-userbot:restore:{deployment_id}",),
            )
            gate = connection.execute(
                "SELECT account_id, gate_state FROM deployment_restore_state "
                "WHERE deployment_id = %s FOR UPDATE",
                (deployment_id,),
            ).fetchone()
            if gate != {"account_id": account_id, "gate_state": "validating"}:
                raise ValueError("RESTORE_GATE_NOT_VALIDATING")  # noqa: TRY301
            actual_revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            if actual_revision is None or actual_revision["version_num"] != schema_revision:
                raise ValueError("SCHEMA_REVISION_INVALID")  # noqa: TRY301
            _replay_ledger(
                connection,
                account_id=account_id,
                scope_secret=scope_secret,
                entries=entries,
                replayers=replayers,
            )
            _verify_database_integrity(connection, account_id)
            _verify_side_effect_reconciliation(connection, account_id)
            _verify_credentials(connection, deployment_id)
            # The evidence marker is written before the authoritative database gate opens.
            # If the marker or commit fails, the transaction remains validating. A brief
            # marker/gate mismatch is fail-closed because runtime readiness trusts the gate.
            _write_marker(snapshot_id)
            marker_written = True
            updated = connection.execute(
                """
                UPDATE deployment_restore_state SET gate_state = 'open',
                  erasure_replay_verified = true, unknown_send_reconciled = true,
                  credentials_verified = true, session_verified = true,
                  verified_at = now(), version = version + 1, updated_at = now()
                WHERE deployment_id = %s AND account_id = %s AND gate_state = 'validating'
                RETURNING deployment_id
                """,
                (deployment_id, account_id),
            ).fetchone()
            if updated is None:
                raise ValueError("RESTORE_GATE_OPEN_FAILED")  # noqa: TRY301
    except BaseException:
        if marker_written and marker.is_file() and not marker.is_symlink():
            with suppress(OSError):
                marker.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--close", action="store_true")
    operation.add_argument("--verify-and-open", action="store_true")
    operation.add_argument("--export-ledger", action="store_true")
    operation.add_argument("--stage-erasure-replay", action="store_true")
    parser.add_argument(
        "--deployment-config",
        type=Path,
        default=Path("/etc/telegram-userbot/config/deployment.json"),
    )
    parser.add_argument("--ledger", type=Path, default=Path("/erasure-ledger/ledger.jsonl"))
    parser.add_argument("--ledger-output", type=Path, default=Path("/erasure-ledger/ledger.jsonl"))
    parser.add_argument("--snapshot-id")
    args = parser.parse_args(argv)
    try:
        deployment_id, account_id, schema_revision = _load_identity(args.deployment_config)
        if args.close:
            close_gate(deployment_id=deployment_id, account_id=account_id)
        elif args.stage_erasure_replay:
            stage_erasure_replay(
                deployment_id=deployment_id,
                account_id=account_id,
                schema_revision=schema_revision,
                ledger_path=args.ledger,
            )
        elif args.verify_and_open:
            verify_and_open(
                deployment_id=deployment_id,
                account_id=account_id,
                schema_revision=schema_revision,
                ledger_path=args.ledger,
            )
        else:
            snapshot_id = args.snapshot_id or (
                "ledger-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
            )
            count = export_ledger(
                deployment_id=deployment_id,
                account_id=account_id,
                output_path=args.ledger_output,
                snapshot_id=snapshot_id,
            )
    except ValueError as error:
        code = str(error)
        return _fail(code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "OPERATION_FAILED")
    except (
        OSError,
        UnicodeError,
        RuntimeError,
        sqlite3.Error,
        psycopg.Error,
    ):
        return _fail("OPERATION_FAILED")
    if args.export_ledger:
        print(f"ERASURE_LEDGER_EXPORTED:{count}")
    else:
        print("RESTORE_GATE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
