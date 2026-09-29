from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

from telegram_userbot.adapters.persistence import migration
from telegram_userbot.adapters.persistence.migration import (
    MIGRATION_LOCK_KEY,
    MigrationError,
    MigrationStatus,
    migrate_to_head,
)
from telegram_userbot.adapters.persistence.role_closure import (
    ROLE_CLOSURE_FILENAMES,
    RoleClosurePlan,
    RoleClosureScript,
)
from telegram_userbot.platform.compatibility import EXPECTED_SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[4]
EXPECTED_REVISION = EXPECTED_SCHEMA_REVISION
EXPECTED_VECTOR_VERSION = "0.8.6"


def _role_closure() -> RoleClosurePlan:
    return RoleClosurePlan(
        tuple(RoleClosureScript(name, "SELECT 1;") for name in ROLE_CLOSURE_FILENAMES)
    )


class _ScalarRows:
    def __init__(self, rows: tuple[str, ...]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[str]:
        return list(self._rows)


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.transaction_active = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self._connection.rollbacks += 1
        self._connection.transaction_active = False


class _Connection:
    def __init__(
        self,
        *,
        lock_acquired: bool = True,
        heads: tuple[str, ...] = (EXPECTED_REVISION,),
        vector_version: str | None = EXPECTED_VECTOR_VERSION,
    ) -> None:
        self.lock_acquired = lock_acquired
        self.heads = heads
        self.vector_version = vector_version
        self.version_table_exists = True
        self.statements: list[str] = []
        self.transaction_active = False
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement: object, parameters: object | None = None) -> object:
        sql = str(statement)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            assert parameters == {"lock_key": MIGRATION_LOCK_KEY}
            self.transaction_active = True
            return self.lock_acquired
        if "pg_advisory_unlock" in sql:
            assert parameters == {"lock_key": MIGRATION_LOCK_KEY}
            self.transaction_active = True
            return True
        if "to_regclass" in sql:
            return self.version_table_exists
        if "extversion" in sql:
            return self.vector_version
        if "FROM pg_tables" in sql:
            return 0
        raise AssertionError(f"unexpected scalar statement: {sql}")

    def execute(self, statement: object, parameters: object | None = None) -> _ScalarRows:
        sql = str(statement)
        self.statements.append(sql)
        assert parameters is None
        if "public.alembic_version" in sql:
            return _ScalarRows(self.heads)
        raise AssertionError(f"unexpected execute statement: {sql}")

    def begin(self) -> _Transaction:
        assert not self.transaction_active
        return _Transaction(self)

    def commit(self) -> None:
        self.commits += 1
        self.transaction_active = False

    def rollback(self) -> None:
        self.rollbacks += 1
        self.transaction_active = False

    def in_transaction(self) -> bool:
        return self.transaction_active


class _ConnectionContext(AbstractContextManager[_Connection]):
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> _Connection:
        return self._connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._connection.transaction_active = False


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.connect_calls = 0

    def connect(self) -> _ConnectionContext:
        self.connect_calls += 1
        return _ConnectionContext(self.connection)


class _FailingEngine:
    def __init__(self, error_factory: Callable[[], Exception]) -> None:
        self.error_factory = error_factory

    def connect(self) -> _ConnectionContext:
        raise self.error_factory()


def _config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _accept_source_head(config: Config, expected_revision: str) -> str:
    assert isinstance(config, Config)
    assert expected_revision == EXPECTED_REVISION
    return expected_revision


@pytest.fixture(autouse=True)
def _accept_bootstrapped_role_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration, "_verify_role_contract", lambda connection: None)
    monkeypatch.setattr(
        migration,
        "_run_role_closure",
        lambda connection, plan: None,
    )


@pytest.mark.unit
def test_busy_lock_returns_before_extension_or_alembic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(lock_acquired=False)
    engine = _Engine(connection)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("migration work must not run without the lock")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_ensure_vector_extension", forbidden)
    monkeypatch.setattr(migration, "_verify_role_contract", forbidden)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", forbidden)
    monkeypatch.setattr(migration, "_run_role_closure", forbidden)

    result = migrate_to_head(
        cast(Engine, engine),
        _config(),
        expected_revision=EXPECTED_REVISION,
        expected_vector_version=EXPECTED_VECTOR_VERSION,
        role_closure=_role_closure(),
    )

    assert result.status is MigrationStatus.BUSY
    assert result.from_revision is None
    assert result.to_revision is None
    assert connection.statements == ["SELECT pg_try_advisory_lock(:lock_key)"]
    assert connection.rollbacks == 1


@pytest.mark.unit
def test_role_drift_fails_before_alembic_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()

    def reject_contract(supplied: Connection) -> None:
        assert supplied is cast(Connection, connection)
        raise MigrationError("MIGRATION_ROLE_CONTRACT_INVALID")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("schema and grant DDL must not run after role drift")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_verify_role_contract", reject_contract)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", forbidden)
    monkeypatch.setattr(migration, "_run_role_closure", forbidden)

    with pytest.raises(MigrationError, match=r"^MIGRATION_ROLE_CONTRACT_INVALID$"):
        migrate_to_head(
            cast(Engine, _Engine(connection)),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"


@pytest.mark.unit
def test_success_uses_fixed_session_lock_and_verifies_exact_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(heads=("0023_m7_proactive_snapshot",))

    calls: list[str] = []

    def upgrade(config: Config, supplied: Connection) -> None:
        assert isinstance(config, Config)
        assert supplied is cast(Connection, connection)
        calls.append("alembic")
        connection.heads = (EXPECTED_REVISION,)

    def close_roles(supplied: Connection, plan: RoleClosurePlan) -> None:
        assert supplied is cast(Connection, connection)
        assert plan == _role_closure()
        assert connection.transaction_active is True
        calls.append("roles")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", upgrade)
    monkeypatch.setattr(migration, "_run_role_closure", close_roles)

    result = migrate_to_head(
        cast(Engine, _Engine(connection)),
        _config(),
        expected_revision=EXPECTED_REVISION,
        expected_vector_version=EXPECTED_VECTOR_VERSION,
        role_closure=_role_closure(),
    )

    assert result.status is MigrationStatus.APPLIED
    assert result.from_revision == "0023_m7_proactive_snapshot"
    assert result.to_revision == EXPECTED_REVISION
    assert connection.statements[0] == "SELECT pg_try_advisory_lock(:lock_key)"
    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"
    assert all("CREATE EXTENSION" not in statement for statement in connection.statements)
    assert all("ALTER EXTENSION" not in statement for statement in connection.statements)
    assert connection.commits == 2
    assert calls == ["alembic", "roles"]


@pytest.mark.unit
def test_role_closure_failure_rolls_back_and_releases_session_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(heads=(EXPECTED_REVISION,))

    def fail_closure(supplied: Connection, plan: RoleClosurePlan) -> None:
        assert supplied is cast(Connection, connection)
        assert plan == _role_closure()
        assert connection.transaction_active is True
        raise RuntimeError("synthetic role closure failure")

    def accept_upgrade(config: Config, supplied: Connection) -> None:
        assert isinstance(config, Config)
        assert supplied is cast(Connection, connection)

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", accept_upgrade)
    monkeypatch.setattr(migration, "_run_role_closure", fail_closure)

    with pytest.raises(MigrationError, match=r"^MIGRATION_FAILED$"):
        migrate_to_head(
            cast(Engine, _Engine(connection)),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert connection.rollbacks >= 1
    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"


@pytest.mark.unit
def test_incompatible_vector_extension_fails_without_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(vector_version="0.8.5")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Alembic must not run with an incompatible extension")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", forbidden)

    with pytest.raises(MigrationError, match=r"^MIGRATION_EXTENSION_VERSION_MISMATCH$"):
        migrate_to_head(
            cast(Engine, _Engine(connection)),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"
    assert all("ALTER EXTENSION" not in statement for statement in connection.statements)


@pytest.mark.unit
def test_missing_vector_extension_fails_without_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(vector_version=None)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Alembic must not run without the bootstrapped extension")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", forbidden)

    with pytest.raises(MigrationError, match=r"^MIGRATION_EXTENSION_UNAVAILABLE$"):
        migrate_to_head(
            cast(Engine, _Engine(connection)),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert all("CREATE EXTENSION" not in statement for statement in connection.statements)
    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"


@pytest.mark.unit
def test_post_migration_multiple_heads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(heads=("0023_m7_proactive_snapshot",))

    def create_multiple_heads(config: Config, supplied: Connection) -> None:
        assert isinstance(config, Config)
        assert supplied is cast(Connection, connection)
        connection.heads = (EXPECTED_REVISION, "synthetic_second_head")

    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    monkeypatch.setattr(migration, "_run_alembic_upgrade", create_multiple_heads)

    with pytest.raises(MigrationError, match=r"^MIGRATION_DATABASE_HEAD_INVALID$"):
        migrate_to_head(
            cast(Engine, _Engine(connection)),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert connection.statements[-1] == "SELECT pg_advisory_unlock(:lock_key)"


@pytest.mark.unit
def test_source_head_must_exactly_match_image_revision() -> None:
    engine = _Engine(_Connection())

    with pytest.raises(MigrationError, match=r"^MIGRATION_SOURCE_HEAD_INVALID$"):
        migrate_to_head(
            cast(Engine, engine),
            _config(),
            expected_revision="0023_m7_proactive_snapshot",
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert engine.connect_calls == 0


@pytest.mark.unit
def test_driver_error_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    private_value = "SYNTHETIC_MIGRATOR_PASSWORD"
    monkeypatch.setattr(migration, "_source_head", _accept_source_head)
    engine = _FailingEngine(
        lambda: RuntimeError(f"postgresql://migrator:{private_value}@postgres/app")
    )

    with pytest.raises(MigrationError) as error:
        migrate_to_head(
            cast(Engine, engine),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )

    assert error.value.code == "MIGRATION_FAILED"
    assert private_value not in str(error.value)
    assert private_value not in repr(error.value)


@pytest.mark.unit
def test_alembic_upgrade_receives_connection_and_restores_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    connection = cast(Connection, _Connection())
    previous = object()
    config.attributes["connection"] = previous

    def upgrade(received: Config, revision: str) -> None:
        assert received.attributes["connection"] is connection
        assert revision == "head"

    monkeypatch.setattr(alembic_command, "upgrade", upgrade)

    migration._run_alembic_upgrade(config, connection)

    assert config.attributes["connection"] is previous


@pytest.mark.unit
@pytest.mark.parametrize(
    ("revision", "vector_version"),
    [
        ("", EXPECTED_VECTOR_VERSION),
        ("invalid-revision", EXPECTED_VECTOR_VERSION),
        (EXPECTED_REVISION, "0.8"),
    ],
)
def test_migration_policy_rejects_invalid_revision_or_vector_before_connect(
    revision: str, vector_version: str
) -> None:
    engine = _Engine(_Connection())

    with pytest.raises(MigrationError, match=r"^MIGRATION_POLICY_INVALID$"):
        migrate_to_head(
            cast(Engine, engine),
            _config(),
            expected_revision=revision,
            expected_vector_version=vector_version,
            role_closure=_role_closure(),
        )

    assert engine.connect_calls == 0


@pytest.mark.unit
def test_unreadable_alembic_source_fails_before_database_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_Connection())

    def fail_source(_config: Config) -> object:
        raise OSError("synthetic source failure")

    monkeypatch.setattr(ScriptDirectory, "from_config", fail_source)

    with pytest.raises(MigrationError, match=r"^MIGRATION_SOURCE_UNAVAILABLE$"):
        migrate_to_head(
            cast(Engine, engine),
            _config(),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=_role_closure(),
        )
    assert engine.connect_calls == 0


@pytest.mark.unit
def test_fresh_database_has_no_revision_but_nonfresh_check_requires_one() -> None:
    connection = _Connection()
    connection.version_table_exists = False

    assert (
        migration._single_database_revision(cast(Connection, connection), allow_empty=True) is None
    )
    with pytest.raises(MigrationError, match=r"^MIGRATION_DATABASE_HEAD_INVALID$"):
        migration._single_database_revision(cast(Connection, connection), allow_empty=False)


@pytest.mark.unit
def test_vector_probe_driver_failure_is_mapped_to_stable_unavailable_code() -> None:
    class FailingVectorConnection(_Connection):
        def scalar(self, statement: object, parameters: object | None = None) -> object:
            if "extversion" in str(statement):
                raise OSError("private driver detail")
            return super().scalar(statement, parameters)

    with pytest.raises(MigrationError, match=r"^MIGRATION_EXTENSION_UNAVAILABLE$") as error:
        migration._ensure_vector_extension(
            cast(Connection, FailingVectorConnection()), EXPECTED_VECTOR_VERSION
        )
    assert "private driver detail" not in str(error.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("vector_version", "wrong_owner_count", "code"),
    [
        ("0.8.5", 0, "MIGRATION_EXTENSION_VERSION_MISMATCH"),
        (EXPECTED_VECTOR_VERSION, 1, "MIGRATION_OWNERSHIP_INVALID"),
    ],
)
def test_post_migration_vector_and_table_owner_drift_fail_closed(
    vector_version: str, wrong_owner_count: int, code: str
) -> None:
    class DriftedPostMigrationConnection(_Connection):
        def scalar(self, statement: object, parameters: object | None = None) -> object:
            if "FROM pg_tables" in str(statement):
                return wrong_owner_count
            return super().scalar(statement, parameters)

    connection = DriftedPostMigrationConnection(vector_version=vector_version)

    with pytest.raises(MigrationError, match=f"^{code}$"):
        migration._verify_post_migration(
            cast(Connection, connection),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
        )


@pytest.mark.unit
def test_alembic_upgrade_removes_temporary_connection_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    connection = cast(Connection, _Connection())

    def upgrade(received: Config, revision: str) -> None:
        assert received.attributes["connection"] is connection
        assert revision == "head"

    monkeypatch.setattr(alembic_command, "upgrade", upgrade)

    migration._run_alembic_upgrade(config, connection)
    assert "connection" not in config.attributes


@pytest.mark.unit
def test_unlock_cleanup_swallows_driver_failure_and_rolls_back() -> None:
    class FailingUnlockConnection(_Connection):
        def scalar(self, statement: object, parameters: object | None = None) -> object:
            if "pg_advisory_unlock" in str(statement):
                self.transaction_active = True
                raise OSError("synthetic unlock failure")
            return super().scalar(statement, parameters)

    connection = FailingUnlockConnection()
    connection.transaction_active = True

    migration._unlock_safely(cast(Connection, connection))

    assert connection.rollbacks == 2
    assert connection.transaction_active is False
