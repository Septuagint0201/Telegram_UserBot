"""Explicit async SQLAlchemy transaction boundary."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> Self:
        self.session = self._session_factory()
        self._committed = False
        await self.session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None

    async def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work has not been entered")
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work has not been entered")
        await self.session.rollback()
        self._committed = False
