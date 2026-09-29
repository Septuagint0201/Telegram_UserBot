from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import NullPool

from telegram_userbot.adapters.persistence.engine import PostgresConnectionSettings
from telegram_userbot.adapters.persistence.ownership import (
    OwnershipScope,
    PostgresSessionOwnership,
    SessionOwnershipError,
    SessionOwnershipTarget,
    create_dedicated_ownership_engine,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


def postgres_settings(
    *, runtime_role: str | None = "telegram_userbot_app_runtime"
) -> PostgresConnectionSettings:
    return PostgresConnectionSettings(
        host="postgres",
        port=5432,
        database="telegram_userbot",
        login_role="telegram_userbot_app_login",
        runtime_role=runtime_role,
        password=SensitiveValue("SYNTHETIC_DATABASE_PASSWORD"),
        sslmode="disable",
    )


class FakeConnection:
    def __init__(
        self,
        *,
        backend_pid: object = 321,
        acquired: object = True,
        released: object = True,
    ) -> None:
        self.backend_pid = backend_pid
        self.lock_acquired = acquired
        self.lock_released = released
        self.lock_visible: object = True
        self.raise_on: str | None = None
        self.closed: int = 0
        self.statements: list[tuple[str, object | None]] = []

    async def execute(self, statement: object) -> None:
        query = str(statement)
        self.statements.append((query, None))
        if self.raise_on == "execute":
            raise RuntimeError("SYNTHETIC_DATABASE_PASSWORD")

    async def scalar(self, statement: object, parameters: object | None = None) -> object:
        query = str(statement)
        self.statements.append((query, parameters))
        if self.raise_on is not None and self.raise_on in query:
            raise RuntimeError("SYNTHETIC_DATABASE_PASSWORD")
        if "FROM pg_locks" in query:
            return self.lock_visible
        if "pg_backend_pid" in query:
            return self.backend_pid
        if "pg_try_advisory_lock" in query:
            return self.lock_acquired
        if "pg_advisory_unlock" in query:
            return self.lock_released
        raise AssertionError("unexpected statement")

    async def close(self) -> None:
        self.closed += 1
        if self.raise_on == "close":
            raise RuntimeError("SYNTHETIC_DATABASE_PASSWORD")


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.connects: int = 0
        self.disposals: int = 0
        self.raise_on_connect = False
        self.raise_on_dispose = False

    async def connect(self) -> AsyncConnection:
        self.connects += 1
        if self.raise_on_connect:
            raise RuntimeError("SYNTHETIC_DATABASE_PASSWORD")
        return cast(AsyncConnection, self.connection)

    async def dispose(self) -> None:
        self.disposals += 1
        if self.raise_on_dispose:
            raise RuntimeError("SYNTHETIC_DATABASE_PASSWORD")


def ownership(
    fake: FakeEngine,
    *,
    settings: PostgresConnectionSettings | None = None,
) -> PostgresSessionOwnership:
    return PostgresSessionOwnership(
        settings or postgres_settings(),
        SessionOwnershipTarget.telegram_session(
            deployment_id="personal-ai",
            telegram_account_id=123456789,
        ),
        engine_factory=lambda _settings: cast(AsyncEngine, fake),
    )


def assert_not_acquired(holder: PostgresSessionOwnership) -> None:
    assert holder.acquired is False


@pytest.mark.unit
def test_targets_are_stable_separated_and_content_free() -> None:
    first = SessionOwnershipTarget.telegram_session(
        deployment_id="personal-ai", telegram_account_id=123456789
    )
    same = SessionOwnershipTarget.telegram_session(
        deployment_id="personal-ai", telegram_account_id=123456789
    )
    account_changed = SessionOwnershipTarget.telegram_session(
        deployment_id="personal-ai", telegram_account_id=987654321
    )
    deployment_changed = SessionOwnershipTarget.telegram_session(
        deployment_id="other-ai", telegram_account_id=123456789
    )
    scheduler = SessionOwnershipTarget.worker_scheduler(deployment_id="personal-ai")

    assert first == same
    assert len({first.lock_key, account_changed.lock_key, deployment_changed.lock_key}) == 3
    assert scheduler.scope is OwnershipScope.WORKER_SCHEDULER
    assert scheduler.lock_key != first.lock_key
    assert "123456789" not in repr(first)
    assert "lock_key=<derived>" in repr(first)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("deployment_id", "account_id"),
    [
        ("Bad", 1),
        ("ab", 1),
        ("personal-ai", 0),
        ("personal-ai", True),
        ("personal-ai", 1 << 63),
    ],
)
def test_target_rejects_invalid_identity(deployment_id: str, account_id: int) -> None:
    with pytest.raises(ValueError, match="identity is invalid"):
        SessionOwnershipTarget.telegram_session(
            deployment_id=deployment_id,
            telegram_account_id=account_id,
        )


@pytest.mark.unit
async def test_acquire_probe_release_and_repeat_are_idempotent() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)

    await holder.acquire()
    assert holder.acquired
    assert holder.scope is OwnershipScope.TELEGRAM_SESSION
    assert engine.connects == 1
    assert str(connection.statements[0][0]).startswith('SET ROLE "telegram_userbot_app_runtime"')
    assert await holder.probe()

    await holder.acquire()
    assert engine.connects == 1
    await holder.release()
    assert_not_acquired(holder)
    assert connection.closed == 1
    assert engine.disposals == 1
    await holder.close()
    assert connection.closed == 1
    assert engine.disposals == 1


@pytest.mark.unit
async def test_login_only_ownership_does_not_issue_set_role() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine, settings=postgres_settings(runtime_role=None))
    await holder.acquire()
    try:
        assert all("SET ROLE" not in query for query, _ in connection.statements)
    finally:
        await holder.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("backend_pid", "acquired", "code"),
    [
        ("321", True, "SESSION_OWNERSHIP_IDENTITY_INVALID"),
        (321, False, "SESSION_OWNERSHIP_CONTENDED"),
        (321, None, "SESSION_OWNERSHIP_CONTENDED"),
    ],
)
async def test_acquire_fails_closed_and_closes_dedicated_session(
    backend_pid: object,
    acquired: object,
    code: str,
) -> None:
    connection = FakeConnection(backend_pid=backend_pid, acquired=acquired)
    engine = FakeEngine(connection)
    holder = ownership(engine)

    with pytest.raises(SessionOwnershipError) as captured:
        await holder.acquire()
    assert captured.value.code == code
    assert str(captured.value) == code
    assert "SYNTHETIC_DATABASE_PASSWORD" not in repr(captured.value)
    assert not holder.acquired
    assert connection.closed == 1
    assert engine.disposals == 1


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["connect", "execute", "SELECT pg_backend_pid"])
async def test_connection_errors_are_redacted(failure: str) -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    if failure == "connect":
        engine.raise_on_connect = True
    else:
        connection.raise_on = failure

    with pytest.raises(SessionOwnershipError) as captured:
        await ownership(engine).acquire()
    assert captured.value.code == "SESSION_OWNERSHIP_UNAVAILABLE"
    assert "SYNTHETIC_DATABASE_PASSWORD" not in repr(captured.value)
    assert engine.disposals == 1


@pytest.mark.unit
async def test_failed_or_changed_probe_immediately_loses_ownership() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)
    await holder.acquire()

    connection.backend_pid = 999
    assert not await holder.probe()
    assert not holder.acquired
    assert connection.closed == 1
    assert engine.disposals == 1
    assert not await holder.probe()


@pytest.mark.unit
async def test_probe_fails_closed_when_session_no_longer_holds_lock() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)
    await holder.acquire()

    connection.lock_visible = False
    assert not await holder.probe()
    assert holder.acquired is False
    assert connection.closed == 1
    assert engine.disposals == 1


@pytest.mark.unit
async def test_probe_exception_immediately_loses_ownership() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)
    await holder.acquire()

    connection.raise_on = "SELECT pg_backend_pid"
    assert not await holder.probe()
    assert not holder.acquired
    assert connection.closed == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("released", "failure", "code"),
    [
        (False, None, "SESSION_OWNERSHIP_LOST"),
        (True, "SELECT pg_advisory_unlock", "SESSION_OWNERSHIP_RELEASE_FAILED"),
        (True, "close", "SESSION_OWNERSHIP_CLOSE_FAILED"),
    ],
)
async def test_release_errors_use_stable_codes_and_still_clear_state(
    released: object,
    failure: str | None,
    code: str,
) -> None:
    connection = FakeConnection(released=released)
    engine = FakeEngine(connection)
    holder = ownership(engine)
    await holder.acquire()
    connection.raise_on = failure

    with pytest.raises(SessionOwnershipError) as captured:
        await holder.release()
    assert captured.value.code == code
    assert not holder.acquired
    await holder.close()


@pytest.mark.unit
async def test_context_manager_closes_after_body_error_without_replacing_it() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)

    async def run_body() -> None:
        async with holder:
            connection.raise_on = "SELECT pg_advisory_unlock"
            raise ValueError("body")

    with pytest.raises(ValueError, match="body"):
        await run_body()
    assert connection.closed == 1


@pytest.mark.unit
async def test_context_manager_surfaces_cleanup_failure_after_successful_body() -> None:
    connection = FakeConnection()
    engine = FakeEngine(connection)
    holder = ownership(engine)

    with pytest.raises(SessionOwnershipError) as captured:
        async with holder:
            connection.raise_on = "SELECT pg_advisory_unlock"

    assert captured.value.code == "SESSION_OWNERSHIP_RELEASE_FAILED"
    assert connection.closed == 1


@pytest.mark.unit
async def test_production_ownership_engine_uses_null_pool() -> None:
    engine = create_dedicated_ownership_engine(postgres_settings(runtime_role=None))
    try:
        assert isinstance(engine.sync_engine.pool, NullPool)
    finally:
        await engine.dispose()
