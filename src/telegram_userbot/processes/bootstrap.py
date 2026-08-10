"""M0 entrypoint: validate safe local configuration and exit."""

import os
import sys
from collections.abc import Mapping
from typing import TextIO

from telegram_userbot.platform.config.settings import AppSettings, ConfigurationError
from telegram_userbot.platform.logging.safe import SafeLogger
from telegram_userbot.platform.time.system import SystemClock


def run(
    values: Mapping[str, str], *, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr
) -> int:
    try:
        settings = AppSettings.from_mapping(values)
    except ConfigurationError as error:
        logger = SafeLogger(component="bootstrap", clock=SystemClock().now, sink=stderr)
        logger.failure("startup_configuration_rejected", error, error_code="CONFIG_INVALID")
        return 2

    logger = SafeLogger(component="bootstrap", clock=SystemClock().now, sink=stdout)
    logger.event("startup_configuration_valid", settings.safe_log_fields())
    return 0


def main() -> int:
    return run(os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
