import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import PeerAdmission
from telegram_userbot.adapters.telegram_user.telethon_runtime import (
    TelethonSessionRuntime,
    TelethonSessionRuntimeError,
    TelethonSessionSettings,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import (
    TelegramUpdateWatermark,
    TelethonUpdateScope,
)
from telegram_userbot.domain.messaging import NormalizedTelegramEvent, PeerKind
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant
from telegram_userbot.platform.runtime import TerminationDeadline

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000001")
CONVERSATION_ID = UUID(int=2)
SQLITE_HEADER = b"SQLite format 3\x00"


def _write_session(path: Path) -> None:
    path.write_bytes(SQLITE_HEADER)
    if os.name == "posix":
        path.chmod(0o600)


@dataclass(slots=True)
class FakeClient:
    authorized: bool = True
    identity: object = field(default_factory=lambda: SimpleNamespace(id=1000, bot=False))
    catch_up_update: object | None = None
    connect_gate: asyncio.Event | None = None
    connect_started: asyncio.Event | None = None
    disconnect_gate: asyncio.Event | None = None
    disconnect_ignore_cancel: bool = False
    disconnect_error: BaseException | None = None
    run_gate: asyncio.Event | None = None
    run_started: asyncio.Event | None = None
    run_error: BaseException | None = None
    calls: list[str] = field(default_factory=list)
    handler: Callable[[object], Awaitable[None]] | None = None
    builder: object | None = None

    async def __call__(self, request: object) -> object:
        return object()

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.connect_started is not None:
            self.connect_started.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        if self.disconnect_gate is not None:
            try:
                await self.disconnect_gate.wait()
            except asyncio.CancelledError:
                if not self.disconnect_ignore_cancel:
                    raise
                await self.disconnect_gate.wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error

    async def is_user_authorized(self) -> bool:
        self.calls.append("authorized")
        return self.authorized

    async def get_me(self) -> object:
        self.calls.append("get_me")
        return self.identity

    def add_event_handler(
        self, callback: Callable[[object], Awaitable[None]], event: object
    ) -> None:
        self.calls.append("add_handler")
        self.handler = callback
        self.builder = event

    def remove_event_handler(
        self, callback: Callable[[object], Awaitable[None]], event: object | None = None
    ) -> int:
        self.calls.append("remove_handler")
        if self.handler is None:
            return 0
        assert callback == self.handler
        assert event == self.builder
        self.handler = None
        return 1

    async def catch_up(self) -> None:
        self.calls.append("catch_up")
        if self.catch_up_update is not None:
            assert self.handler is not None
            await self.handler(self.catch_up_update)

    async def run_until_disconnected(self) -> None:
        self.calls.append("run")
        if self.run_started is not None:
            self.run_started.set()
        if self.run_gate is not None:
            await self.run_gate.wait()
        if self.run_error is not None:
            raise self.run_error


@dataclass(slots=True)
class Factory:
    clients: list[FakeClient]
    calls: list[tuple[Path, int, str]] = field(default_factory=list)

    def __call__(self, path: Path, api_id: int, api_hash: str) -> FakeClient:
        self.calls.append((path, api_id, api_hash))
        return self.clients.pop(0)


def _settings(session_path: Path) -> TelethonSessionSettings:
    return TelethonSessionSettings(
        account_id=ACCOUNT_ID,
        telegram_user_id=1000,
        session_path=session_path,
        api_id=12345,
        api_hash=SensitiveValue("0123456789abcdef0123456789abcdef"),
    )


def _update(message: str = "hello") -> object:
    return types.UpdateShortMessage(
        id=10,
        user_id=42,
        message=message,
        pts=100,
        pts_count=1,
        date=NOW,
        out=False,
    )


@pytest.mark.unit
def test_session_settings_validate_account_and_api_credential_format(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "account.session")
    with pytest.raises(ValueError, match="ACCOUNT_ID_INVALID"):
        replace(settings, account_id=UUID(int=0))
    with pytest.raises(ValueError, match="API_ID_INVALID"):
        replace(settings, api_id=2_147_483_648)
    with pytest.raises(ValueError, match="API_HASH_INVALID"):
        replace(settings, api_hash=SensitiveValue("not-a-telegram-api-hash"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_uses_explicit_pre_authorized_identity_and_catchup(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient(catch_up_update=_update())
    factory = Factory([client])
    ingested: list[NormalizedTelegramEvent] = []
    recorded: list[TelegramUpdateWatermark] = []
    known = TelegramUpdateWatermark("account", 99, 1, "previous")

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        assert scope.telegram_chat_id == 42
        assert runtime.intake_client is client
        return PeerAdmission(ACCOUNT_ID, CONVERSATION_ID, PeerKind.PRIVATE_USER, 42)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        ingested.append(event)
        return object()

    async def load_watermark() -> TelegramUpdateWatermark | None:
        client.calls.append("load_watermark")
        return known

    async def record_watermark(watermark: TelegramUpdateWatermark) -> None:
        assert ingested
        client.calls.append("record_watermark")
        recorded.append(watermark)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=factory,
        load_watermark=load_watermark,
        record_watermark=record_watermark,
        new_uuid=lambda: UUID(int=len(ingested) + 10),
        now=lambda: NOW,
    )

    await runtime.start()

    assert runtime.started
    assert runtime.client is client
    assert runtime.last_catch_up_watermark == known
    assert factory.calls == [(session_path, 12345, "0123456789abcdef0123456789abcdef")]
    assert client.calls == [
        "add_handler",
        "connect",
        "authorized",
        "get_me",
        "load_watermark",
        "catch_up",
        "record_watermark",
    ]
    assert len(ingested) == 1
    assert ingested[0].body is not None
    assert ingested[0].body.text == "hello"
    assert recorded[0].pts == 100

    await runtime.catch_up()
    assert len(ingested) == 2
    assert ingested[0].update_fingerprint == ingested[1].update_fingerprint
    await runtime.close()
    assert client.calls[-2:] == ["remove_handler", "disconnect"]
    assert not runtime.started


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorized", "identity", "code"),
    [
        (False, SimpleNamespace(id=1000, bot=False), "SESSION_UNAUTHORIZED"),
        (True, SimpleNamespace(id=999, bot=False), "IDENTITY_MISMATCH"),
        (True, SimpleNamespace(id=1000, bot=True), "IDENTITY_MISMATCH"),
    ],
)
async def test_session_runtime_fails_closed_on_authorization_or_identity(
    tmp_path: Path, authorized: bool, identity: object, code: str
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient(authorized=authorized, identity=identity)

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
    )

    with pytest.raises(TelethonSessionRuntimeError, match=code):
        await runtime.start()
    assert not runtime.started
    assert client.calls[-2:] == ["remove_handler", "disconnect"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_cancellation_disconnects_partial_connect(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    gate = asyncio.Event()
    started = asyncio.Event()
    client = FakeClient(connect_gate=gate, connect_started=started)

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
    )
    task = asyncio.create_task(runtime.start())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.calls[-2:] == ["remove_handler", "disconnect"]
    assert not runtime.started


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_start_fails_closed_when_lifecycle_lock_deadline_expires(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient()
    factory = Factory([client])

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=factory,
        monotonic_clock=lambda: MonotonicInstant(2.0),
    )
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(0.0),
        grace_seconds=1.0,
    )
    await runtime._lifecycle_lock.acquire()
    try:
        with pytest.raises(TelethonSessionRuntimeError, match="DEADLINE_EXCEEDED"):
            await runtime.start(deadline=deadline)
    finally:
        runtime._lifecycle_lock.release()
    assert factory.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_close_waits_for_ingest_only_within_deadline(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient()

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    clock_values = iter((MonotonicInstant(0.0), MonotonicInstant(2.0)))
    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        monotonic_clock=lambda: next(clock_values),
    )
    await runtime.start()
    await runtime._ingest_lock.acquire()
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(0.0),
        grace_seconds=1.0,
    )
    with pytest.raises(TelethonSessionRuntimeError, match="DEADLINE_EXCEEDED"):
        await runtime.close(deadline=deadline)
    assert not runtime.started
    assert runtime.teardown_pending
    runtime._ingest_lock.release()
    await runtime.close()
    assert not runtime.teardown_pending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_close_detaches_cancellation_resistant_disconnect(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    disconnect_gate = asyncio.Event()
    client = FakeClient(
        disconnect_gate=disconnect_gate,
        disconnect_ignore_cancel=True,
    )

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        monotonic_clock=lambda: MonotonicInstant(0.0),
    )
    await runtime.start()
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(0.0),
        grace_seconds=0.02,
    )
    with pytest.raises(TelethonSessionRuntimeError, match="DEADLINE_EXCEEDED"):
        await runtime.close(deadline=deadline)
    assert runtime.teardown_pending
    disconnect_gate.set()
    await asyncio.sleep(0)
    await runtime.close()
    assert not runtime.teardown_pending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_reuses_detached_disconnect_and_surfaces_late_failure(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    disconnect_gate = asyncio.Event()
    client = FakeClient(
        disconnect_gate=disconnect_gate,
        disconnect_ignore_cancel=True,
        disconnect_error=RuntimeError("synthetic disconnect failure"),
    )

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        monotonic_clock=lambda: MonotonicInstant(0.0),
    )
    await runtime.start()
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(0.0),
        grace_seconds=0.02,
    )
    with pytest.raises(TelethonSessionRuntimeError, match="DEADLINE_EXCEEDED"):
        await runtime.close(deadline=deadline)
    assert runtime.teardown_pending

    disconnect_gate.set()
    await asyncio.sleep(0)
    with pytest.raises(TelethonSessionRuntimeError, match="DISCONNECT_FAILED"):
        await runtime.close()
    # The retry observes the original in-flight operation rather than invoking
    # a second Telethon disconnect concurrently.
    assert client.calls.count("disconnect") == 1
    assert runtime.teardown_pending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_failed_start_cleanup_is_bounded_without_masking_error(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    disconnect_gate = asyncio.Event()
    client = FakeClient(
        authorized=False,
        disconnect_gate=disconnect_gate,
        disconnect_ignore_cancel=True,
    )

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        monotonic_clock=lambda: MonotonicInstant(0.0),
    )
    deadline = TerminationDeadline(
        started_at=MonotonicInstant(0.0),
        grace_seconds=0.02,
    )
    with pytest.raises(TelethonSessionRuntimeError, match="SESSION_UNAUTHORIZED"):
        await runtime.start(deadline=deadline)
    assert not runtime.started
    disconnect_gate.set()
    await asyncio.sleep(0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_run_cancellation_drains_and_disconnects(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    run_gate = asyncio.Event()
    run_started = asyncio.Event()
    client = FakeClient(run_gate=run_gate, run_started=run_started)

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
    )
    task = asyncio.create_task(runtime.run_until_disconnected())
    await run_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.calls[-3:] == ["run", "remove_handler", "disconnect"]
    assert not runtime.started


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_run_preserves_primary_error_when_close_fails(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient(
        run_error=RuntimeError("synthetic run failure"),
        disconnect_error=RuntimeError("synthetic disconnect failure"),
    )

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
    )
    with pytest.raises(RuntimeError, match="synthetic run failure"):
        await runtime.run_until_disconnected()
    assert runtime.teardown_pending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_runtime_reconnects_with_fresh_client_and_watermark(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    first = FakeClient()
    second = FakeClient()
    factory = Factory([first, second])
    loads = 0

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    async def load() -> TelegramUpdateWatermark | None:
        nonlocal loads
        loads += 1
        return TelegramUpdateWatermark("account", loads, 1, f"load:{loads}")

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=factory,
        load_watermark=load,
    )
    await runtime.start()
    await runtime.reconnect()

    assert runtime.client is second
    assert loads == 2
    assert first.calls[-2:] == ["remove_handler", "disconnect"]
    assert second.calls[-1] == "catch_up"
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bot_and_group_updates_remain_content_free(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient()
    ingested: list[NormalizedTelegramEvent] = []

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        if scope.peer_kind_hint is PeerKind.GROUP:
            return PeerAdmission(ACCOUNT_ID, None, PeerKind.GROUP, scope.telegram_chat_id)
        return PeerAdmission(ACCOUNT_ID, None, PeerKind.BOT, scope.telegram_chat_id)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        ingested.append(event)
        return object()

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        now=lambda: NOW,
    )
    await runtime.start()
    assert client.handler is not None
    await client.handler(_update("bot content"))
    await client.handler(
        types.UpdateShortChatMessage(
            id=11,
            from_id=42,
            chat_id=99,
            message="group content",
            pts=101,
            pts_count=1,
            date=NOW,
        )
    )

    assert [event.peer_kind for event in ingested] == [PeerKind.BOT, PeerKind.GROUP]
    assert all(event.body is None and event.media == () for event in ingested)
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_private_delete_uses_message_lookup_when_telegram_omits_chat(tmp_path: Path) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient()
    ingested: list[NormalizedTelegramEvent] = []

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        assert scope.telegram_chat_id is None
        assert scope.telegram_message_id == 50
        return PeerAdmission(ACCOUNT_ID, CONVERSATION_ID, PeerKind.PRIVATE_USER, 42)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        ingested.append(event)
        return object()

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        now=lambda: NOW,
    )
    await runtime.start()
    assert client.handler is not None
    await client.handler(types.UpdateDeleteMessages([50], pts=500, pts_count=1))

    assert ingested[0].conversation_id == CONVERSATION_ID
    assert ingested[0].telegram_chat_id == 42
    assert ingested[0].event_kind.value == "message.deleted"
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_delete_advances_watermark_only_after_whole_batch_then_replays(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "account.session"
    _write_session(session_path)
    client = FakeClient()
    attempts: list[int] = []
    recorded: list[TelegramUpdateWatermark] = []
    fail_second_once = True

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        return PeerAdmission(ACCOUNT_ID, CONVERSATION_ID, PeerKind.PRIVATE_USER, 42)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        nonlocal fail_second_once
        assert event.telegram_message_id is not None
        attempts.append(event.telegram_message_id)
        if event.telegram_message_id == 51 and fail_second_once:
            fail_second_once = False
            raise RuntimeError("synthetic second projection failure")
        return object()

    async def record(watermark: TelegramUpdateWatermark) -> None:
        recorded.append(watermark)

    runtime = TelethonSessionRuntime(
        _settings(session_path),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([client]),
        record_watermark=record,
        now=lambda: NOW,
    )
    await runtime.start()
    assert client.handler is not None
    update = types.UpdateDeleteMessages([50, 51], pts=500, pts_count=2)

    with pytest.raises(RuntimeError, match="second projection"):
        await client.handler(update)
    assert recorded == []

    await client.handler(update)
    assert attempts == [50, 51, 50, 51]
    assert len(recorded) == 1
    assert recorded[0].scope == "account"
    assert recorded[0].pts == 500
    assert recorded[0].pts_count == 2
    assert recorded[0].update_identity.endswith(":51:500")
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_path_must_be_existing_absolute_regular_session(tmp_path: Path) -> None:
    missing = tmp_path / "missing.session"

    async def resolve(scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError(scope)

    async def ingest(event: NormalizedTelegramEvent) -> object:
        raise AssertionError(event)

    runtime = TelethonSessionRuntime(
        _settings(missing),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([FakeClient()]),
    )
    with pytest.raises(TelethonSessionRuntimeError, match="SESSION_MISSING"):
        await runtime.start()

    invalid = tmp_path / "invalid.session"
    invalid.write_bytes(b"not a SQLite session")
    invalid_runtime = TelethonSessionRuntime(
        _settings(invalid),
        resolve_admission=resolve,
        ingest=ingest,
        client_factory=Factory([FakeClient()]),
    )
    with pytest.raises(TelethonSessionRuntimeError, match="SESSION_FORMAT_INVALID"):
        await invalid_runtime.start()

    if os.name == "posix":
        unsafe_mode = tmp_path / "unsafe.session"
        unsafe_mode.write_bytes(SQLITE_HEADER)
        unsafe_mode.chmod(0o644)
        unsafe_runtime = TelethonSessionRuntime(
            _settings(unsafe_mode),
            resolve_admission=resolve,
            ingest=ingest,
            client_factory=Factory([FakeClient()]),
        )
        with pytest.raises(TelethonSessionRuntimeError, match="SESSION_MODE_INVALID"):
            await unsafe_runtime.start()
