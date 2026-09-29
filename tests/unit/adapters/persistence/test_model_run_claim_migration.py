import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest


class _Rows:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> Sequence[Mapping[str, object]]:
        return self._rows


class _Connection:
    def __init__(
        self,
        run: Mapping[str, object],
        membership: Sequence[Mapping[str, object]],
    ) -> None:
        self._run = run
        self._membership = membership
        self.updates: list[Mapping[str, object]] = []

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> _Rows:
        sql = str(statement)
        if sql.startswith("SELECT id, logical_role"):
            return _Rows((self._run,))
        if sql.startswith("SELECT message_id"):
            return _Rows(self._membership)
        if sql.startswith("UPDATE model_runs SET orchestration_claim_fingerprint"):
            assert parameters is not None
            self.updates.append(parameters)
            return _Rows(())
        raise AssertionError(sql)


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[4] / "alembic" / "versions" / "0027_m8_model_run_claim.py"
    )
    spec = importlib.util.spec_from_file_location("m8_model_run_claim_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _membership() -> tuple[dict[str, object], ...]:
    return (
        {"message_id": UUID(int=11), "message_revision_no": 2},
        {"message_id": UUID(int=12), "message_revision_no": 3},
    )


@pytest.mark.unit
def test_prepared_legacy_run_backfills_claim_without_copying_canonical_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    run_id = UUID(int=1)
    turn_id = UUID(int=2)
    connection = _Connection(
        {
            "id": run_id,
            "logical_role": "main_ai",
            "turn_id": turn_id,
            "context_manifest_id": UUID(int=3),
            "input_fingerprint": b"canonical-model-input-hmac".ljust(32, b"!"),
        },
        _membership(),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    migration._backfill_orchestration_claims()

    expected = migration._membership_claim(((UUID(int=11), 2), (UUID(int=12), 3)))
    assert connection.updates == [{"claim": expected, "run_id": run_id}]
    assert connection.updates[0]["claim"] != b"canonical-model-input-hmac".ljust(32, b"!")


@pytest.mark.unit
def test_unprepared_legacy_run_requires_input_to_equal_membership_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    connection = _Connection(
        {
            "id": UUID(int=1),
            "logical_role": "main_ai",
            "turn_id": UUID(int=2),
            "context_manifest_id": None,
            "input_fingerprint": b"wrong".ljust(32, b"!"),
        },
        _membership(),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    with pytest.raises(
        RuntimeError,
        match="M8_MODEL_RUN_CLAIM_BACKFILL_INPUT_MISMATCH",
    ):
        migration._backfill_orchestration_claims()


@pytest.mark.unit
def test_legacy_background_run_without_manifest_claim_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    connection = _Connection(
        {
            "id": UUID(int=1),
            "logical_role": "memory_agent",
            "turn_id": None,
            "context_manifest_id": None,
            "input_fingerprint": b"opaque".ljust(32, b"!"),
        },
        (),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    with pytest.raises(
        RuntimeError,
        match="M8_MODEL_RUN_CLAIM_BACKFILL_UNSUPPORTED_OWNER",
    ):
        migration._backfill_orchestration_claims()
