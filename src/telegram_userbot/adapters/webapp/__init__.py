"""Key-only Telegram Web App adapter."""

from telegram_userbot.adapters.webapp.app import ModelKeyMutationPort, create_key_web_app
from telegram_userbot.adapters.webapp.auth import (
    IssuedLaunchToken,
    LaunchTokenCodec,
    TelegramInitDataVerifier,
    TelegramWebIdentity,
    WebAppAuthenticationError,
)
from telegram_userbot.adapters.webapp.key_service import ModelKeyMutationService

__all__ = [
    "IssuedLaunchToken",
    "LaunchTokenCodec",
    "ModelKeyMutationPort",
    "ModelKeyMutationService",
    "TelegramInitDataVerifier",
    "TelegramWebIdentity",
    "WebAppAuthenticationError",
    "create_key_web_app",
]
