"""Real PostgreSQL deletion-ledger export, grants, and replay evidence."""

import hashlib
import hmac
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.operations.test_m8_restore_and_systemd import _load_script

KEY = b"synthetic-erasure-export-test-key!"
NOW = datetime(2030, 1, 1, tzinfo=UTC)
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _seed_memory(connection: psycopg.Connection[dict[str, Any]]) -> tuple[UUID, UUID]:
    account_id, memory_id = uuid4(), uuid4()
    connection.execute(
        "INSERT INTO accounts (id, telegram_user_id, display_label, status) "
        "VALUES (%s, %s, 'synthetic-ledger', 'active')",
        (account_id, account_id.int % (2**63 - 1)),
    )
    connection.execute(
        "INSERT INTO memories (id, account_id, memory_type, semantic_key_hash, status, "
        "current_version_no) VALUES (%s, %s, 'fact', %s, 'active', 1)",
        (memory_id, account_id, b"m" * 32),
    )
    connection.execute(
        "INSERT INTO memory_versions (id, account_id, memory_id, version_no, operation, "
        "payload_schema_version, payload, rendered_text, importance, confidence, "
        "time_precision, validator_policy_version, acceptance_kind) "
        "VALUES (%s, %s, %s, 1, 'create', 1, '{\"text\":\"synthetic-body\"}', "
        "'synthetic-body', 0.5, 0.5, 'unknown', 'test-v1', 'migration')",
        (uuid4(), account_id, memory_id),
    )
    return account_id, memory_id


def _seed_request(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: UUID,
    memory_id: UUID,
    *,
    completed: bool = True,
) -> UUID:
    request_id = uuid4()
    connection.execute(
        "INSERT INTO data_erasure_requests (id, account_id, scope_type, memory_id, state, "
        "requested_by, request_idempotency_key, policy_version, completed_at) "
        "VALUES (%s, %s, 'memory', %s, %s, 'synthetic', %s, 1, %s)",
        (
            request_id,
            account_id,
            memory_id,
            "completed" if completed else "requested",
            hashlib.sha256(request_id.bytes).digest(),
            NOW if completed else None,
        ),
    )
    if completed:
        connection.execute(
            "INSERT INTO erasure_ledger (account_scope_hmac, scope_type, target_scope_hmac, "
            "request_id, policy_version, completed_at) VALUES (%s, 'memory', %s, %s, 1, %s)",
            (
                hmac.digest(KEY, account_id.bytes, "sha256"),
                hmac.digest(KEY, memory_id.bytes, "sha256"),
                request_id,
                NOW,
            ),
        )
    return request_id


@pytest.mark.integration
def test_cumulative_export_is_account_scoped_and_replays_repeated_targets(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("restore_gate")
    dsn = migrated_database.replace("postgresql+psycopg://", "postgresql://", 1)
    with (
        psycopg.connect(dsn, row_factory=dict_row) as connection,
        connection.transaction(force_rollback=True),
    ):
        account_id, memory_id = _seed_memory(connection)
        request_ids = {_seed_request(connection, account_id, memory_id) for _ in range(2)}
        pending_id = _seed_request(connection, account_id, memory_id, completed=False)
        other_account, other_memory = _seed_memory(connection)
        other_request = _seed_request(connection, other_account, other_memory)
        monkeypatch.setattr(module, "_export_connection", lambda: nullcontext(connection))
        monkeypatch.setattr(module, "_read_secret", lambda _path: KEY)
        connection.execute("SET LOCAL ROLE telegram_userbot_export_runtime")
        output = tmp_path / "ledger.jsonl"
        assert (
            module.export_ledger(
                deployment_id="test-primary",
                account_id=account_id,
                output_path=output,
                snapshot_id="ledger-real-db",
            )
            == 2
        )
        payload = output.read_bytes()
        assert b"synthetic-body" not in payload
        assert str(account_id).encode() not in payload
        assert str(memory_id).encode() not in payload
        assert str(pending_id).encode() not in payload
        assert str(other_request).encode() not in payload
        _, entries = module._load_ledger(
            output,
            expected_digest=hashlib.sha256(payload).hexdigest(),
            deployment_id="test-primary",
            account_id=account_id,
            scope_secret=KEY,
        )
        assert {UUID(entry["request_id"]) for entry in entries} == request_ids
        for forbidden in (
            "SELECT requested_by FROM data_erasure_requests",
            "DELETE FROM erasure_ledger WHERE false",
            "UPDATE data_erasure_requests SET state = 'completed' WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute(forbidden)
        connection.execute("RESET ROLE")
        # Model an older restored database: its memory body exists and its ledger
        # predates these two completed requests. Overlay and retry are one-way.
        connection.execute(
            "DELETE FROM erasure_ledger WHERE request_id = ANY(%s)", (list(request_ids),)
        )
        connection.execute(
            "DELETE FROM data_erasure_requests WHERE id = ANY(%s)", (list(request_ids),)
        )
        with pytest.raises(ValueError, match="LEDGER_EXPORT_HISTORY_REGRESSED"):
            module.export_ledger(
                deployment_id="test-primary",
                account_id=account_id,
                output_path=output,
                snapshot_id="ledger-regressed",
            )
        assert output.read_bytes() == payload
        for _ in range(2):
            module._replay_memory_entries(connection, account_id, KEY, entries)
        row = connection.execute(
            "SELECT m.status, v.payload, v.rendered_text FROM memories m JOIN memory_versions v "
            "ON v.memory_id = m.id WHERE m.id = %s",
            (memory_id,),
        ).fetchone()
        assert row == {"status": "forgotten", "payload": {}, "rendered_text": None}
        assert connection.execute(
            "SELECT count(*) AS n FROM erasure_ledger WHERE request_id = ANY(%s)",
            (list(request_ids),),
        ).fetchone() == {"n": 2}
        assert connection.execute(
            "SELECT status FROM memories WHERE id = %s",
            (other_memory,),
        ).fetchone() == {"status": "active"}
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.integration
def test_ledger_export_connection_enforces_read_only_transactions(migrated_database: str) -> None:
    dsn = migrated_database.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(
        dsn,
        options="-c role=telegram_userbot_export_runtime -c default_transaction_read_only=on",
    ) as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("UPDATE data_export_requests SET state = 'pending' WHERE false")
        connection.rollback()
