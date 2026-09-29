import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import telegram_userbot.processes.control as control_module
from telegram_userbot.adapters.queue.redis import RedisRuntime
from telegram_userbot.adapters.telegram_bot.http import TelegramBotAPI, TelegramBotIdentity
from telegram_userbot.adapters.telegram_bot.polling import ControlBotPoller
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.compatibility import RESOURCE_PROFILE
from telegram_userbot.platform.config.production import (
    ProductionProcess,
    ProductionSettings,
    SecretBundle,
)
from telegram_userbot.platform.health import ServiceName
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.control import (
    ControlComponents,
    ControlProcessError,
    ControlWebServer,
    ProductionControlApplication,
    build_control_application,
    run,
)

NOW = datetime(2026, 8, 28, 8, tzinfo=UTC)


class _RedisFake:
    def __init__(self, events: list[str]) -> None:
        self.started = False
        self._events = events

    async def connect(self, *, with_arq: bool) -> None:
        assert not with_arq
        self.started = True
        self._events.append("redis-started")

    async def clear_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.CONTROL

    async def publish_heartbeat(self, service: ServiceName) -> None:
        assert service is ServiceName.CONTROL

    async def close(self) -> None:
        self.started = False
        self._events.append("redis-closed")


class _APIFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def aclose(self) -> None:
        self._events.append("api-closed")


class _PollerFake:
    identity_verified = True

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()


class _PreviewMaintenanceFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def run_once(self, *, now: datetime) -> int:
        assert now.tzinfo is not None
        self._events.append("preview-maintenance")
        return 0


class _WebServerFake:
    def __init__(self, events: list[str], *, exit_immediately: bool = False) -> None:
        self.started = False
        self._should_exit = False
        self.started_event = asyncio.Event()
        self._exit_event = asyncio.Event()
        self._events = events
        self._exit_immediately = exit_immediately

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self._exit_event.set()

    async def serve(self) -> None:
        self.started = True
        self.started_event.set()
        self._events.append("web-started")
        if not self._exit_immediately:
            await self._exit_event.wait()
        self._events.append("web-stopped")


class _EngineFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("engine-disposed")


class _ProcessFake:
    def __init__(self) -> None:
        self.accepting_new_work = True
        self.draining = False
        self._drain = asyncio.Event()

    async def wait_for_drain(self) -> None:
        await self._drain.wait()

    def request_drain(self) -> None:
        self.accepting_new_work = False
        self.draining = True
        self._drain.set()


class _AdmissionFake:
    def __init__(self) -> None:
        self.maintenance = False
        self.process: ManagedProcess | None = None

    def __call__(self) -> bool:
        return self.process is not None and self.process.accepting_new_work


class _HealthApplication(ProductionControlApplication):
    async def _database_available(self) -> bool:
        return True

    async def _schema_ready(self) -> bool:
        return True

    async def _restore_gate_open(self) -> bool:
        return True

    async def _redis_available(self) -> bool:
        return True

    async def _persist_status(self, state: Any) -> bool:
        del state
        return True


def _components(
    events: list[str], *, exit_immediately: bool = False
) -> tuple[ControlComponents, _WebServerFake]:
    deployment = SimpleNamespace(
        deployment_id="synthetic-deployment",
        source_commit="1" * 40,
    )
    settings = cast(
        ProductionSettings,
        SimpleNamespace(bootstrap_maintenance=False, deployment=deployment),
    )

    def sessions_unavailable() -> AsyncSession:
        raise RuntimeError("synthetic status store unavailable")

    web = _WebServerFake(events, exit_immediately=exit_immediately)
    components = ControlComponents(
        settings=settings,
        engine=cast(AsyncEngine, _EngineFake(events)),
        sessions=cast(async_sessionmaker[AsyncSession], sessions_unavailable),
        redis=cast(RedisRuntime, _RedisFake(events)),
        api=cast(TelegramBotAPI, _APIFake(events)),
        poller=cast(ControlBotPoller, _PollerFake()),
        web_server=cast(ControlWebServer, web),
        preview_maintenance=_PreviewMaintenanceFake(events),
        account_id=UUID(int=1),
        admission=cast(Any, _AdmissionFake()),
        instance_id=uuid7(),
        started_at=NOW,
    )
    return components, web


@pytest.mark.unit
async def test_control_serve_waits_for_web_drain_before_disposing_dependencies() -> None:
    events: list[str] = []
    components, web = _components(events)
    application = ProductionControlApplication(components)
    process = _ProcessFake()
    task = asyncio.create_task(application.serve(cast(ManagedProcess, process)))
    await web.started_event.wait()

    process.request_drain()
    await task

    assert events.index("web-stopped") < events.index("engine-disposed")
    assert events.index("preview-maintenance") < events.index("api-closed")
    assert events[-3:] == ["redis-closed", "api-closed", "engine-disposed"]
    assert components.admission.process is None


@pytest.mark.unit
async def test_control_serve_fails_when_a_child_exits_before_drain() -> None:
    events: list[str] = []
    components, _ = _components(events, exit_immediately=True)
    application = ProductionControlApplication(components)

    with pytest.raises(ControlProcessError, match=r"^CONTROL_CHILD_EXITED$"):
        await application.serve(cast(ManagedProcess, _ProcessFake()))

    assert "engine-disposed" in events


@pytest.mark.unit
async def test_control_preview_maintenance_retries_without_stopping_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    components, _ = _components(events)
    calls = 0
    retried = asyncio.Event()

    class _TransientMaintenance:
        async def run_once(self, *, now: datetime) -> int:
            nonlocal calls
            assert now.tzinfo is not None
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic transient failure")
            retried.set()
            return 0

    components = replace(components, preview_maintenance=_TransientMaintenance())
    monkeypatch.setattr(control_module, "PREVIEW_MAINTENANCE_INTERVAL_SECONDS", 0.01)
    application = ProductionControlApplication(components)

    task = asyncio.create_task(application._run_preview_maintenance())
    await asyncio.wait_for(retried.wait(), timeout=1)
    application._stop.set()
    await task

    assert calls == 2


@pytest.mark.unit
async def test_control_health_reports_local_liveness_separately_from_draining() -> None:
    events: list[str] = []
    components, web = _components(events)
    process = _ProcessFake()
    process.request_drain()
    components.admission.process = cast(ManagedProcess, process)
    web.started = True
    application = _HealthApplication(components)
    application._serve_loop_running = True

    state = await application.health(UtcTimestamp(NOW))

    assert state.service is ServiceName.CONTROL
    assert state.process_loop_ok
    assert state.draining
    assert state.control_bot_ready
    assert state.web_api_ready


@pytest.mark.unit
def test_control_entrypoint_maps_arguments_configuration_and_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = StringIO()
    assert run(["unexpected"], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "CONTROL_ARGUMENT_INVALID\n"

    sentinel_settings = cast(ProductionSettings, object())
    sentinel_secrets = cast(SecretBundle, object())
    monkeypatch.setattr(
        control_module,
        "load_control_settings",
        lambda values: (sentinel_settings, sentinel_secrets),
    )

    def reject_build(**kwargs: object) -> None:
        del kwargs
        raise ControlProcessError("CONTROL_SYNTHETIC_CONFIGURATION_INVALID")

    monkeypatch.setattr(control_module, "build_control_application", reject_build)
    stderr = StringIO()
    assert run([], {}, stderr=stderr) == 2
    assert stderr.getvalue() == "CONTROL_CONFIGURATION_REJECTED\n"

    application = cast(ProductionControlApplication, object())
    monkeypatch.setattr(
        control_module,
        "build_control_application",
        lambda **_: application,
    )

    async def fail_runtime(_application: ProductionControlApplication) -> None:
        raise RuntimeError("synthetic runtime failure")

    monkeypatch.setattr(control_module, "run_control_application", fail_runtime)
    stderr = StringIO()
    assert run([], {}, stderr=stderr) == 1
    assert stderr.getvalue() == "CONTROL_RUNTIME_FAILED\n"

    async def run_successfully(_application: ProductionControlApplication) -> None:
        return None

    monkeypatch.setattr(control_module, "run_control_application", run_successfully)
    stderr = StringIO()
    assert run([], {}, stderr=stderr) == 0
    assert stderr.getvalue() == ""


@pytest.mark.unit
def test_control_build_constructs_the_production_capability_probe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_id = "synthetic-deployment"
    identity = SimpleNamespace(
        account_id=UUID(int=1),
        telegram_user_id=10,
        control_bot_user_id=12345,
        control_bot_username="SyntheticControlBot",
        control_admin_user_ids=(99,),
    )
    deployment = SimpleNamespace(
        deployment_id=deployment_id,
        source_commit="1" * 40,
        public_host="control.example.test",
        runtime_identity=identity,
    )
    database = SimpleNamespace(
        host="postgres",
        port=5432,
        database="telegram_userbot",
        login_role="control_login",
        runtime_role="control_runtime",
        password_secret_id="control_database_password",  # noqa: S106 - secret identifier
        sslmode="require",
    )
    settings = cast(
        ProductionSettings,
        SimpleNamespace(
            process=ProductionProcess.CONTROL,
            bootstrap_maintenance=False,
            deployment=deployment,
            database=database,
            redis=None,
        ),
    )
    keyring_json = json.dumps(
        {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "active_key_version": 1,
            "keys": {"1": base64.b64encode(b"k" * 32).decode("ascii")},
        },
        separators=(",", ":"),
    ).encode()
    secrets = SecretBundle(
        (
            ("control_database_password", SensitiveValue(b"d" * 32)),
            ("control_bot_token", SensitiveValue(b"12345:" + b"t" * 32)),
            ("credential_master_keyring", SensitiveValue(keyring_json)),
        )
    )
    events: list[str] = []
    engine = cast(AsyncEngine, _EngineFake(events))
    redis = cast(RedisRuntime, _RedisFake(events))
    bot_identity = TelegramBotIdentity(12345, "SyntheticControlBot")
    api = TelegramBotAPI(
        SensitiveValue("12345:" + "t" * 32),
        bot_identity,
        cast(Any, object()),
    )
    probe = cast(Any, object())
    captured: dict[str, object] = {}

    def build_probe(sessions: object, **kwargs: object) -> object:
        captured["sessions"] = sessions
        captured.update(kwargs)
        return probe

    monkeypatch.setattr(control_module, "build_production_capability_probe", build_probe)
    web = _WebServerFake(events)

    application = build_control_application(
        settings=settings,
        secrets=secrets,
        engine=engine,
        redis=redis,
        api=api,
        web_server_factory=lambda _: cast(ControlWebServer, web),
        now=NOW,
    )

    assert isinstance(application, ProductionControlApplication)
    assert captured["sessions"] is application._components.sessions
    assert "keyring" in captured
    assert control_module._status_metadata(settings).resource_profile == RESOURCE_PROFILE
