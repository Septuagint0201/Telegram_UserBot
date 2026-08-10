"""Deterministic port fakes with no external I/O."""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import TracebackType
from typing import TypeVar
from uuid import UUID

from telegram_userbot.application.ports.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    ModelRequest,
    ModelResponse,
)
from telegram_userbot.application.ports.queue import JobEnvelope
from telegram_userbot.application.ports.telegram import TelegramSendReceipt, TelegramTextRequest
from telegram_userbot.domain.shared.ids import EntityId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant, UtcTimestamp

EntityIdT = TypeVar("EntityIdT", bound=EntityId)


@dataclass(slots=True)
class VirtualClock:
    wall: UtcTimestamp
    monotonic: MonotonicInstant = field(default_factory=lambda: MonotonicInstant(0.0))

    def now(self) -> UtcTimestamp:
        return self.wall

    def monotonic_now(self) -> MonotonicInstant:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.wall = self.wall.add(timedelta(seconds=seconds))
        self.monotonic = MonotonicInstant(self.monotonic.value + seconds)


class DeterministicRandom:
    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 - deterministic test fake

    def random_bytes(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("size must be non-negative")
        return self._random.getrandbits(size * 8).to_bytes(size, "big")

    def random_below(self, upper_bound: int) -> int:
        if upper_bound <= 0:
            raise ValueError("upper_bound must be positive")
        return self._random.randrange(upper_bound)


class DeterministicIdFactory:
    def __init__(self) -> None:
        self._next = 1

    def new(self, identifier_type: type[EntityIdT]) -> EntityIdT:
        identifier = identifier_type(UUID(int=self._next))
        self._next += 1
        return identifier


@dataclass(slots=True)
class FakeUnitOfWork:
    commits: int = 0
    rollbacks: int = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@dataclass(slots=True)
class FakeJobQueue:
    jobs: list[JobEnvelope] = field(default_factory=list)

    async def enqueue(self, job: JobEnvelope) -> None:
        self.jobs.append(job)


@dataclass(slots=True)
class FakeTelegramGateway:
    requests: list[TelegramTextRequest] = field(default_factory=list)

    async def send_text(self, request: TelegramTextRequest) -> TelegramSendReceipt:
        self.requests.append(request)
        return TelegramSendReceipt(telegram_message_id=len(self.requests))


@dataclass(slots=True)
class FakeModelGateway:
    requests: list[ModelRequest] = field(default_factory=list)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(SensitiveValue("SYNTHETIC_MODEL_OUTPUT"), "0" * 64)


@dataclass(slots=True)
class FakeEmbeddingGateway:
    requests: list[EmbeddingRequest] = field(default_factory=list)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.requests.append(request)
        return EmbeddingResponse((0.0, 1.0))


def fixed_utc() -> UtcTimestamp:
    return UtcTimestamp(datetime.fromisoformat("2030-01-02T03:04:05+00:00"))
