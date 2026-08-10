"""Production-safe local clock adapter with no I/O side effects."""

import time
from datetime import UTC, datetime

from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp


class SystemClock:
    def now(self) -> UtcTimestamp:
        return UtcTimestamp(datetime.now(UTC))

    def monotonic_now(self) -> MonotonicInstant:
        return MonotonicInstant(time.monotonic())
