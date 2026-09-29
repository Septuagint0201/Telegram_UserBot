"""Persistence adapter placeholder for M1."""

from telegram_userbot.adapters.persistence.context_repository import ContextRepository
from telegram_userbot.adapters.persistence.data_export_repository import DataExportRepository
from telegram_userbot.adapters.persistence.media_repository import (
    MediaDeletionLease,
    MediaRepository,
)
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.runtime_cursors import RuntimeCursorRepository
from telegram_userbot.adapters.persistence.service_status import (
    RestoreGateRepository,
    ServiceStatusRepository,
)
from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)

__all__ = [
    "ContextRepository",
    "DataExportRepository",
    "MediaDeletionLease",
    "MediaRepository",
    "ProactiveRepository",
    "RestoreGateRepository",
    "RuntimeCursorRepository",
    "ServiceStatusRepository",
    "TelegramDeliveryService",
    "TelegramLifecycleRepository",
]
