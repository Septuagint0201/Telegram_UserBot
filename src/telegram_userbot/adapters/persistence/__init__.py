"""Persistence adapter placeholder for M1."""

from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)

__all__ = ["TelegramDeliveryService", "TelegramLifecycleRepository"]
