"""Explicit non-production process used only by the loopback Compose validation override."""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health import (
    DEFAULT_HEALTH_SNAPSHOT_PATH,
    HEALTH_SNAPSHOT_VERSION,
    HealthState,
    ServiceName,
)
from telegram_userbot.platform.runtime import ManagedProcess
from telegram_userbot.processes.healthcheck import HEALTH_SNAPSHOT_PATH_ENV

VALIDATION_ACK_ENV = "TUDT_SYNTHETIC_READY_VALIDATION"
VALIDATION_ACK = "explicit-loopback-non-production"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-userbot-synthetic-ready")
    parser.add_argument(
        "--service", required=True, choices=tuple(item.value for item in ServiceName)
    )
    return parser


def synthetic_ready_state(service: ServiceName, observed_at: UtcTimestamp) -> HealthState:
    """Return a visibly synthetic all-ready state for Compose wiring tests only."""

    service_fields: dict[str, bool | None] = {
        "account_ready": None,
        "session_owned": None,
        "telegram_ready": None,
        "control_bot_ready": None,
        "web_api_ready": None,
        "consumer_ready": None,
    }
    if service is ServiceName.APP:
        service_fields.update(account_ready=True, session_owned=True, telegram_ready=True)
    elif service is ServiceName.CONTROL:
        service_fields.update(control_bot_ready=True, web_api_ready=True)
    else:
        service_fields["consumer_ready"] = True
    return HealthState(
        version=HEALTH_SNAPSHOT_VERSION,
        service=service,
        observed_at=observed_at,
        heartbeat_at=observed_at,
        process_loop_ok=True,
        maintenance=False,
        draining=False,
        required_config_ok=True,
        disk_safety_ok=True,
        database_ok=True,
        redis_ok=True,
        schema_ok=True,
        restore_gate_open=True,
        **service_fields,
    )


async def _run(service: ServiceName, snapshot_path: Path) -> None:
    async def health_provider(observed_at: UtcTimestamp) -> HealthState:
        return synthetic_ready_state(service, observed_at)

    async def serve(process: ManagedProcess) -> None:
        await process.wait_for_drain()

    process = ManagedProcess(
        service=service,
        snapshot_path=snapshot_path,
        health_provider=health_provider,
    )
    await process.run(serve)


def run(
    argv: Sequence[str],
    *,
    values: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    environment = values if values is not None else os.environ
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if environment.get(VALIDATION_ACK_ENV) != VALIDATION_ACK:
        print("SYNTHETIC_READY_REFUSED", file=errors)
        return 2
    print("SYNTHETIC_READY_VALIDATION_ONLY", file=output, flush=True)
    snapshot_path = Path(
        environment.get(HEALTH_SNAPSHOT_PATH_ENV, str(DEFAULT_HEALTH_SNAPSHOT_PATH))
    )
    asyncio.run(_run(ServiceName(arguments.service), snapshot_path))
    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
