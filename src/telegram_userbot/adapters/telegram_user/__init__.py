"""Telegram user-client adapters."""

from telegram_userbot.adapters.telegram_user.fake import FakeSendOutcome, ReplayTelegramGateway
from telegram_userbot.adapters.telegram_user.media import ReplayImageSource, TelethonImageSource
from telegram_userbot.adapters.telegram_user.normalizer import (
    PeerAdmission,
    RawMedia,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.adapters.telegram_user.telethon import TelethonTelegramGateway
from telegram_userbot.adapters.telegram_user.telethon_runtime import (
    TelethonSessionRuntime,
    TelethonSessionRuntimeError,
    TelethonSessionSettings,
    default_telethon_client_factory,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import (
    TelegramUpdateWatermark,
    TelethonUpdateCandidate,
    TelethonUpdateScope,
    convert_telethon_update,
)

__all__ = [
    "FakeSendOutcome",
    "PeerAdmission",
    "RawMedia",
    "RawTelegramUpdate",
    "ReplayImageSource",
    "ReplayTelegramGateway",
    "TelegramUpdateWatermark",
    "TelethonImageSource",
    "TelethonSessionRuntime",
    "TelethonSessionRuntimeError",
    "TelethonSessionSettings",
    "TelethonTelegramGateway",
    "TelethonUpdateCandidate",
    "TelethonUpdateScope",
    "convert_telethon_update",
    "default_telethon_client_factory",
    "normalize_update",
]
