"""Injectable, fail-closed executor registry for durable worker jobs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.records import JobRecord
from telegram_userbot.adapters.persistence.schema import (
    conversation_turns,
    message_events,
    messages,
    turn_messages,
)
from telegram_userbot.domain.memory.trigger import EventRange
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.processes.memory_runtime import MemoryReviewRuntimeService

_JOB_TYPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,5}\Z")


class JobExecutionError(RuntimeError):
    """Stable error code with an explicit PostgreSQL retry decision."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    job: JobRecord
    sessions: async_sessionmaker[AsyncSession]
    lease_lost: asyncio.Event
    cpu_heavy: asyncio.Semaphore


class JobExecutor(Protocol):
    async def __call__(self, context: JobExecutionContext) -> None: ...


class WorkerExecutorRegistry:
    """Exact job-type dispatch; unknown work is never reported as success."""

    def __init__(self, executors: Mapping[str, JobExecutor]) -> None:
        copied = dict(executors)
        if any(
            _JOB_TYPE.fullmatch(name) is None or not callable(executor)
            for name, executor in copied.items()
        ):
            raise ValueError("worker executor registry is invalid")
        self._executors = MappingProxyType(copied)

    @property
    def job_types(self) -> frozenset[str]:
        return frozenset(self._executors)

    async def execute(self, context: JobExecutionContext) -> None:
        executor = self._executors.get(context.job.job_type)
        if executor is None:
            raise JobExecutionError("WORKER_EXECUTOR_UNAVAILABLE", retryable=False)
        await executor(context)


class MemoryRefreshExecutor:
    """Translate M3/M4 wakeups into the M6 durable memory-domain queue."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    async def __call__(self, context: JobExecutionContext) -> None:
        if context.lease_lost.is_set():
            raise JobExecutionError("WORKER_JOB_FENCE_LOST", retryable=True)
        if context.job.account_id is None:
            raise JobExecutionError("WORKER_JOB_SCOPE_INVALID", retryable=False)
        if context.job.job_type == "memory.refresh_completed_turn":
            await self._refresh_turn(context)
            return
        if context.job.job_type == "memory.reconcile_message_delete":
            await self._reconcile_delete(context)
            return
        raise JobExecutionError("WORKER_EXECUTOR_MISMATCH", retryable=False)

    async def _refresh_turn(self, context: JobExecutionContext) -> None:
        turn_id = _single_uuid_payload(context.job, "turn_id")
        async with context.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(
                        conversation_turns.c.conversation_id,
                        func.min(turn_messages.c.source_event_id),
                        func.max(turn_messages.c.source_event_id),
                    )
                    .select_from(
                        conversation_turns.join(
                            turn_messages,
                            (turn_messages.c.turn_id == conversation_turns.c.id)
                            & (turn_messages.c.account_id == conversation_turns.c.account_id),
                        )
                    )
                    .where(
                        conversation_turns.c.id == turn_id,
                        conversation_turns.c.account_id == context.job.account_id,
                    )
                    .group_by(conversation_turns.c.conversation_id)
                )
            ).one_or_none()
            if row is None or row[1] is None or row[2] is None:
                raise JobExecutionError("WORKER_MEMORY_SOURCE_MISSING", retryable=False)
            account_id = context.job.account_id
            if account_id is None:
                raise JobExecutionError("WORKER_JOB_SCOPE_INVALID", retryable=False)
            await MemoryRepository(session).refresh_pending_job(
                account_id=account_id,
                conversation_id=row[0],
                job_kind="episode",
                event_range=EventRange(row[1], row[2]),
                estimated_input_tokens=0,
                now=self._now(),
            )

    async def _reconcile_delete(self, context: JobExecutionContext) -> None:
        message_id = _single_uuid_payload(context.job, "message_id")
        async with context.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(messages.c.conversation_id, func.max(message_events.c.id))
                    .select_from(
                        messages.join(
                            message_events,
                            (message_events.c.account_id == messages.c.account_id)
                            & (message_events.c.conversation_id == messages.c.conversation_id)
                            & (
                                message_events.c.telegram_message_id
                                == messages.c.telegram_message_id
                            ),
                        )
                    )
                    .where(
                        messages.c.id == message_id,
                        messages.c.account_id == context.job.account_id,
                        message_events.c.event_kind == "message.deleted",
                    )
                    .group_by(messages.c.conversation_id)
                )
            ).one_or_none()
            if row is None or row[1] is None:
                raise JobExecutionError("WORKER_MEMORY_SOURCE_MISSING", retryable=False)
            account_id = context.job.account_id
            if account_id is None:
                raise JobExecutionError("WORKER_JOB_SCOPE_INVALID", retryable=False)
            await MemoryRepository(session).refresh_pending_job(
                account_id=account_id,
                conversation_id=row[0],
                job_kind="reconciliation",
                event_range=EventRange(row[1], row[1]),
                estimated_input_tokens=0,
                now=self._now(),
            )


class MemoryReviewExecutor:
    """Apply a confirmed memory review action in its existing transaction service."""

    def __init__(self, *, erasure_scope_secret: SensitiveValue[bytes]) -> None:
        self._secret = erasure_scope_secret

    async def __call__(self, context: JobExecutionContext) -> None:
        if context.job.account_id is None or context.job.payload:
            raise JobExecutionError("WORKER_JOB_SCOPE_INVALID", retryable=False)
        if context.lease_lost.is_set():
            raise JobExecutionError("WORKER_JOB_FENCE_LOST", retryable=True)
        await MemoryReviewRuntimeService(
            context.sessions,
            erasure_scope_secret=self._secret,
        ).run_once(account_id=context.job.account_id)


class ErasureReconciliationExecutor:
    """Advance durable erasure stages without exposing content to the queue."""

    def __init__(self, *, erasure_scope_secret: SensitiveValue[bytes]) -> None:
        self._secret = erasure_scope_secret

    async def __call__(self, context: JobExecutionContext) -> None:
        if context.job.account_id is None or context.lease_lost.is_set():
            raise JobExecutionError(
                "WORKER_JOB_FENCE_LOST"
                if context.lease_lost.is_set()
                else "WORKER_JOB_SCOPE_INVALID",
                retryable=context.lease_lost.is_set(),
            )
        request_id = _single_uuid_payload(context.job, "request_id")
        async with context.sessions() as session, session.begin():
            await MemoryRepository(session).reconcile_erasure_request(
                account_id=context.job.account_id,
                request_id=request_id,
                erasure_scope_secret=self._secret.reveal_for_use(),
                now=datetime.now(UTC),
            )


def build_worker_executor_registry(
    *,
    erasure_scope_secret: SensitiveValue[bytes],
    embedding_executor: JobExecutor | None = None,
    memory_executor: JobExecutor | None = None,
) -> WorkerExecutorRegistry:
    refresh = MemoryRefreshExecutor()
    review = MemoryReviewExecutor(erasure_scope_secret=erasure_scope_secret)
    erasure = ErasureReconciliationExecutor(erasure_scope_secret=erasure_scope_secret)
    return WorkerExecutorRegistry(
        {
            "memory.refresh_completed_turn": refresh,
            "memory.reconcile_message_delete": refresh,
            "memory.review_action": review,
            "memory.reconcile_erasure": erasure,
            **({"embedding.compute": embedding_executor} if embedding_executor is not None else {}),
            **({"memory.generate": memory_executor} if memory_executor is not None else {}),
        }
    )


def _single_uuid_payload(job: JobRecord, name: str) -> UUID:
    if set(job.payload) != {name}:
        raise JobExecutionError("WORKER_JOB_PAYLOAD_INVALID", retryable=False)
    raw = job.payload[name]
    if not isinstance(raw, str):
        raise JobExecutionError("WORKER_JOB_PAYLOAD_INVALID", retryable=False)
    try:
        value = UUID(raw)
    except ValueError:
        raise JobExecutionError("WORKER_JOB_PAYLOAD_INVALID", retryable=False) from None
    if value.int == 0:
        raise JobExecutionError("WORKER_JOB_PAYLOAD_INVALID", retryable=False)
    return value


__all__ = [
    "ErasureReconciliationExecutor",
    "JobExecutionContext",
    "JobExecutionError",
    "JobExecutor",
    "MemoryRefreshExecutor",
    "MemoryReviewExecutor",
    "WorkerExecutorRegistry",
    "build_worker_executor_registry",
]
