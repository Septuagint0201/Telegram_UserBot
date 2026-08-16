"""Transaction-owned runtime entrypoint for confirmed memory review actions."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.memory_repository import (
    MemoryRepository,
    ReviewActionExecution,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


class MemoryReviewRuntimeService:
    """Apply one version-bound Control Bot action in a worker transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        erasure_scope_secret: SensitiveValue[bytes],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if len(erasure_scope_secret.reveal_for_use()) < 32:
            raise ValueError("erasure scope secret must contain at least 32 bytes")
        self._session_factory = session_factory
        self._erasure_scope_secret = erasure_scope_secret
        self._now = now or (lambda: datetime.now(UTC))

    async def run_once(self, *, account_id: UUID) -> ReviewActionExecution | None:
        async with self._session_factory() as session, session.begin():
            return await MemoryRepository(session).execute_next_review_action(
                account_id=account_id,
                now=self._now(),
                erasure_scope_secret=self._erasure_scope_secret.reveal_for_use(),
            )


__all__ = ["MemoryReviewRuntimeService"]
