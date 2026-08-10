"""Framework-neutral application ports."""

from telegram_userbot.application.ports.clock import Clock, MonotonicClock
from telegram_userbot.application.ports.ids import IdFactory
from telegram_userbot.application.ports.model import EmbeddingGateway, ModelGateway
from telegram_userbot.application.ports.queue import JobQueue
from telegram_userbot.application.ports.random import RandomSource
from telegram_userbot.application.ports.telegram import TelegramGateway
from telegram_userbot.application.ports.uow import AsyncUnitOfWork

__all__ = [
    "AsyncUnitOfWork",
    "Clock",
    "EmbeddingGateway",
    "IdFactory",
    "JobQueue",
    "ModelGateway",
    "MonotonicClock",
    "RandomSource",
    "TelegramGateway",
]
