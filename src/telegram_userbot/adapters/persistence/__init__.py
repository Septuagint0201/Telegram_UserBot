"""Persistence adapter placeholder for M1."""

from telegram_userbot.adapters.persistence.context_repository import ContextRepository
from telegram_userbot.adapters.persistence.media_repository import MediaRepository
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)

__all__ = [
    "ContextRepository",
    "MediaRepository",
    "ProactiveRepository",
    "TelegramDeliveryService",
    "TelegramLifecycleRepository",
]
