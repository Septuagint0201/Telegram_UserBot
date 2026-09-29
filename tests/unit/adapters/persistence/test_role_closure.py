from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Connection

from telegram_userbot.adapters.persistence import migration
from telegram_userbot.adapters.persistence.migration import MigrationError
from telegram_userbot.adapters.persistence.role_closure import (
    ROLE_CLOSURE_FILENAMES,
    RoleClosureError,
    RoleClosurePlan,
    RoleClosureScript,
    load_role_closure_plan,
)

ROOT = Path(__file__).resolve().parents[4]


def _synthetic_plan() -> RoleClosurePlan:
    return RoleClosurePlan(
        tuple(
            RoleClosureScript(name, f"SELECT '{position}';")
            for position, name in enumerate(ROLE_CLOSURE_FILENAMES, start=1)
        )
    )


@pytest.mark.unit
def test_repository_role_assets_load_in_fixed_m1_through_m8_order() -> None:
    plan = load_role_closure_plan(ROOT / "deploy/postgres")

    assert tuple(script.name for script in plan.scripts) == ROLE_CLOSURE_FILENAMES
    assert all(script.sql.startswith("-- M") for script in plan.scripts)
    assert all("password" not in repr(script).lower() for script in plan.scripts)


@pytest.mark.unit
def test_role_asset_loader_fails_closed_without_disclosing_path(tmp_path: Path) -> None:
    private_path = tmp_path / "private-role-assets"
    private_path.mkdir()

    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ASSET_UNAVAILABLE$") as error:
        load_role_closure_plan(private_path)

    assert str(private_path) not in str(error.value)
    assert str(private_path) not in repr(error.value)


@pytest.mark.unit
def test_role_plan_rejects_missing_or_reordered_scripts() -> None:
    scripts = _synthetic_plan().scripts

    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ORDER_INVALID$"):
        RoleClosurePlan(scripts[:-1])
    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ORDER_INVALID$"):
        RoleClosurePlan(tuple(reversed(scripts)))


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.closed = False

    def execute(self, sql: str, *, prepare: bool) -> None:
        self.calls.append((sql, prepare))

    def close(self) -> None:
        self.closed = True


class _DriverConnection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class _ConnectionFairy:
    def __init__(self, driver_connection: _DriverConnection) -> None:
        self.driver_connection = driver_connection


class _ClosureConnection:
    def __init__(self, cursor: _Cursor) -> None:
        self.connection = _ConnectionFairy(_DriverConnection(cursor))


@pytest.mark.unit
def test_role_closure_uses_one_driver_session_fixed_order_and_unprepared_queries() -> None:
    cursor = _Cursor()
    plan = _synthetic_plan()

    migration._run_role_closure(cast(Connection, _ClosureConnection(cursor)), plan)

    assert cursor.calls == [(script.sql, False) for script in plan.scripts]
    assert cursor.closed is True


class _Rows:
    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self._rows = rows

    def one(self) -> tuple[object, ...]:
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self) -> tuple[tuple[object, ...], ...]:
        return self._rows


class _ContractConnection:
    def __init__(self, *, omit_last_role: bool = False) -> None:
        self.omit_last_role = omit_last_role

    def execute(self, statement: object, parameters: Any = None) -> _Rows:
        del parameters
        sql = str(statement)
        if "session_user" in sql:
            return _Rows((("telegram_userbot_migrator_login", "telegram_userbot_migrator"),))
        if "current_database" in sql:
            return _Rows(
                (
                    (
                        "telegram_userbot",
                        "telegram_userbot_migrator",
                        "telegram_userbot_migrator",
                    ),
                )
            )
        if "FROM pg_roles" in sql:
            roles = migration._EXPECTED_ROLES
            return _Rows(roles[:-1] if self.omit_last_role else roles)
        if "FROM pg_auth_members" in sql:
            return _Rows(migration._EXPECTED_MEMBERSHIPS)
        raise AssertionError(f"unexpected contract query: {sql}")


@pytest.mark.unit
def test_role_contract_requires_exact_identity_ownership_roles_and_memberships() -> None:
    migration._verify_role_contract(cast(Connection, _ContractConnection()))

    with pytest.raises(MigrationError, match=r"^MIGRATION_ROLE_CONTRACT_INVALID$"):
        migration._verify_role_contract(cast(Connection, _ContractConnection(omit_last_role=True)))


class _DriftedContractConnection(_ContractConnection):
    def __init__(self, drift: str) -> None:
        super().__init__()
        self.drift = drift

    def execute(self, statement: object, parameters: Any = None) -> _Rows:
        sql = str(statement)
        if self.drift == "identity" and "session_user" in sql:
            return _Rows((("telegram_userbot_app_login", "telegram_userbot_app_runtime"),))
        if self.drift == "ownership" and "current_database" in sql:
            return _Rows((("telegram_userbot", "telegram_userbot_migrator", "postgres"),))
        if self.drift == "membership" and "FROM pg_auth_members" in sql:
            return _Rows(migration._EXPECTED_MEMBERSHIPS[:-1])
        return super().execute(statement, parameters)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("identity", "MIGRATION_IDENTITY_INVALID"),
        ("ownership", "MIGRATION_OWNERSHIP_INVALID"),
        ("membership", "MIGRATION_ROLE_CONTRACT_INVALID"),
    ],
)
def test_role_contract_fails_closed_on_identity_ownership_or_membership_drift(
    drift: str, code: str
) -> None:
    with pytest.raises(MigrationError, match=f"^{code}$"):
        migration._verify_role_contract(cast(Connection, _DriftedContractConnection(drift)))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "sql"),
    [
        ("unexpected.sql", "SELECT 1;"),
        (ROLE_CLOSURE_FILENAMES[0], ""),
        (ROLE_CLOSURE_FILENAMES[0], "SELECT '\x00';"),
    ],
)
def test_role_script_rejects_unknown_empty_or_nul_content(name: str, sql: str) -> None:
    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ASSET_INVALID$"):
        RoleClosureScript(name, sql)


def _write_synthetic_role_assets(directory: Path) -> None:
    directory.mkdir()
    for filename in ROLE_CLOSURE_FILENAMES:
        (directory / filename).write_text("SELECT 1;\n", encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("payload", [b"", b"SELECT '\x00';", b"\xff"])
def test_role_asset_loader_rejects_empty_nul_or_non_utf8_content(
    tmp_path: Path, payload: bytes
) -> None:
    directory = tmp_path / "roles"
    _write_synthetic_role_assets(directory)
    (directory / ROLE_CLOSURE_FILENAMES[0]).write_bytes(payload)

    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ASSET_INVALID$"):
        load_role_closure_plan(directory)


@pytest.mark.unit
def test_role_asset_loader_rejects_size_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "roles"
    _write_synthetic_role_assets(directory)
    real_read_bytes = Path.read_bytes

    def changed_read(path: Path) -> bytes:
        payload = real_read_bytes(path)
        if path.name == ROLE_CLOSURE_FILENAMES[0]:
            return payload + b" "
        return payload

    monkeypatch.setattr(Path, "read_bytes", changed_read)

    with pytest.raises(RoleClosureError, match=r"^ROLE_CLOSURE_ASSET_INVALID$"):
        load_role_closure_plan(directory)


@pytest.mark.unit
def test_role_closure_rejects_missing_driver_connection() -> None:
    class MissingDriverFairy:
        driver_connection = None

    class MissingDriverConnection:
        connection = MissingDriverFairy()

    with pytest.raises(MigrationError, match=r"^MIGRATION_DRIVER_INVALID$"):
        migration._run_role_closure(
            cast(Connection, MissingDriverConnection()),
            _synthetic_plan(),
        )
