"""Telegram user-client adapters."""

from telegram_userbot.adapters.telegram_user.fake import FakeSendOutcome, ReplayTelegramGateway
from telegram_userbot.adapters.telegram_user.normalizer import (
    PeerAdmission,
    RawMedia,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.adapters.telegram_user.telethon import TelethonTelegramGateway

__all__ = [
    "FakeSendOutcome",
    "PeerAdmission",
    "RawMedia",
    "RawTelegramUpdate",
    "ReplayTelegramGateway",
    "TelethonTelegramGateway",
    "normalize_update",
]
