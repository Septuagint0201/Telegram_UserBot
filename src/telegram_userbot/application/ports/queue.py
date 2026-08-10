"""Durable job dispatch port."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.ids import JobId
from telegram_userbot.domain.shared.time import UtcTimestamp


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    job_id: JobId
    topic: str
    payload_hash: str
    available_at: UtcTimestamp


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, job: JobEnvelope) -> None: ...
