"""Content-free local health probe CLI."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health import (
    DEFAULT_HEALTH_SNAPSHOT_PATH,
    HealthKind,
    HealthSnapshotError,
    ReadinessPolicy,
    ServiceName,
    read_health_snapshot,
)
from telegram_userbot.platform.time.system import SystemClock

HEALTH_SNAPSHOT_PATH_ENV = "TUDT_HEALTH_SNAPSHOT_PATH"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-userbot-healthcheck")
    parser.add_argument(
        "--service", required=True, choices=tuple(item.value for item in ServiceName)
    )
    parser.add_argument(
        "--kind", default=HealthKind.READY.value, choices=tuple(item.value for item in HealthKind)
    )
    return parser


def run(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    snapshot_path: Path | None = None,
    now: UtcTimestamp | None = None,
    policy: ReadinessPolicy | None = None,
) -> int:
    """Read one local snapshot and print exactly one stable result code."""

    arguments = _parser().parse_args(argv)
    service = ServiceName(arguments.service)
    kind = HealthKind(arguments.kind)
    output = stdout or sys.stdout
    resolved_path = snapshot_path or Path(
        os.environ.get(HEALTH_SNAPSHOT_PATH_ENV, DEFAULT_HEALTH_SNAPSHOT_PATH)
    )
    observed_now = now or SystemClock().now()
    try:
        state = read_health_snapshot(resolved_path, expected_service=service, now=observed_now)
    except HealthSnapshotError as error:
        print(error.reason.value, file=output)
        return 1

    readiness_policy = policy or ReadinessPolicy()
    decision = (
        readiness_policy.liveness(state, now=observed_now)
        if kind is HealthKind.LIVE
        else readiness_policy.readiness(state, now=observed_now)
    )
    print(decision.reason.value, file=output)
    return 0 if decision.healthy else 1


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
