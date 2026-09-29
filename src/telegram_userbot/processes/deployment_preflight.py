"""Host-operator deployment preflight CLI; never run inside a business container."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from telegram_userbot.platform.config.deployment_preflight import (
    DeploymentPreflightError,
    DeploymentPreflightInputs,
    validate_deployment_preflight,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-userbot-deployment-preflight")
    parser.add_argument("--deployment-config", required=True, type=Path)
    parser.add_argument("--compose-config", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    return parser


def run(
    argv: Sequence[str],
    *,
    values: dict[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    environment = values if values is not None else os.environ
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    secret_root = environment.get("SECRET_ROOT")
    if secret_root is None:
        print("DEPLOYMENT_PREFLIGHT_FAILED:DEPLOYMENT_PREFLIGHT_SECRET_ROOT_MISSING", file=errors)
        return 2
    try:
        result = validate_deployment_preflight(
            DeploymentPreflightInputs(
                deployment_config=arguments.deployment_config,
                compose_config=arguments.compose_config,
                source_root=arguments.source_root,
                secret_root=secret_root,
            )
        )
    except DeploymentPreflightError as error:
        print(f"DEPLOYMENT_PREFLIGHT_FAILED:{error.code}", file=errors)
        return 1
    print(
        "DEPLOYMENT_PREFLIGHT_PASS:"
        f"services={result.checked_services}:secrets={result.checked_secrets}",
        file=output,
    )
    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
