"""Shared domain value objects."""

from telegram_userbot.domain.shared.ids import (
    AccountId,
    ConversationId,
    CorrelationId,
    EntityId,
    JobId,
    MessageId,
    PeerId,
    RunId,
)
from telegram_userbot.domain.shared.result import AppError, Err, EvidenceStatus, Ok, Result
from telegram_userbot.domain.shared.time import MonotonicDeadline, MonotonicInstant, UtcTimestamp
from telegram_userbot.domain.shared.version import Revision, Version

__all__ = [
    "AccountId",
    "AppError",
    "ConversationId",
    "CorrelationId",
    "EntityId",
    "Err",
    "EvidenceStatus",
    "JobId",
    "MessageId",
    "MonotonicDeadline",
    "MonotonicInstant",
    "Ok",
    "PeerId",
    "Result",
    "Revision",
    "RunId",
    "UtcTimestamp",
    "Version",
]
