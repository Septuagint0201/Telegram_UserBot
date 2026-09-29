"""Hourly, bounded calendar-summary sweeps under Worker scheduler leadership."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.memory_periods import MemoryPeriodRepository


class MemoryPeriodPublisher:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, timezone: str) -> None:
        self.sessions = sessions
        self.timezone = timezone
        self.after: UUID | None = None
        self.next_scan: datetime | None = None

    async def publish(self, *, now: datetime) -> int:
        if self.next_scan is not None and now < self.next_scan:
            return 0
        async with self.sessions() as session, session.begin():
            count, after = await MemoryPeriodRepository(session).scan(
                now=now,
                deployment_timezone=self.timezone,
                after=self.after,
            )
        # Advance only after commit. Restart/leadership changes safely rescan;
        # deterministic durable job identities preserve retry budgets.
        self.after = after
        self.next_scan = now + timedelta(hours=1) if after is None else None
        return count
