"""Process-runtime lifecycle primitives without global side effects."""

from telegram_userbot.platform.runtime.cursors import (
    ControlBotCursor,
    ControlUpdateClaim,
    ControlUpdateClaimOutcome,
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
    TelegramIngestWatermark,
)
from telegram_userbot.platform.runtime.drain import DrainController, TerminationDeadline
from telegram_userbot.platform.runtime.managed import (
    HEALTH_HEARTBEAT_SECONDS,
    SERVICE_DRAIN_GRACE_SECONDS,
    AsyncioSignalRegistrar,
    DrainHook,
    HealthProvider,
    ManagedProcess,
    SignalRegistrar,
)
from telegram_userbot.platform.runtime.watchdog import (
    EVENT_LOOP_STALL_SECONDS,
    EVENT_LOOP_WATCHDOG_EXIT_CODE,
    EventLoopWatchdog,
)

__all__ = [
    "EVENT_LOOP_STALL_SECONDS",
    "EVENT_LOOP_WATCHDOG_EXIT_CODE",
    "HEALTH_HEARTBEAT_SECONDS",
    "SERVICE_DRAIN_GRACE_SECONDS",
    "AsyncioSignalRegistrar",
    "ControlBotCursor",
    "ControlUpdateClaim",
    "ControlUpdateClaimOutcome",
    "ControlUpdateDisposition",
    "ControlUpdateReceipt",
    "ControlUpdateSendState",
    "ControlUpdateState",
    "DrainController",
    "DrainHook",
    "EventLoopWatchdog",
    "HealthProvider",
    "ManagedProcess",
    "SignalRegistrar",
    "TelegramIngestWatermark",
    "TerminationDeadline",
]
