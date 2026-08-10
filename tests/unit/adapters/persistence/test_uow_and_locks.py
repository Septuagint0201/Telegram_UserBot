from typing import cast
from uuid import uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.locks import try_advisory_scope_lock
from telegram_userbot.adapters.persistence.uow import SqlAlchemyUnitOfWork


class FakeSession:
    def __init__(self) -> None:
        self.began = False
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.scalar_value: object = True

    async def begin(self) -> None:
        self.began = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True

    async def scalar(self, statement: object, parameters: object) -> object:
        return self.scalar_value


@pytest.mark.unit
async def test_uow_requires_entry_and_rolls_back_uncommitted_work() -> None:
    fake = FakeSession()
    factory = cast(async_sessionmaker[AsyncSession], lambda: fake)
    uow = SqlAlchemyUnitOfWork(factory)
    with pytest.raises(RuntimeError, match="not been entered"):
        await uow.commit()
    with pytest.raises(RuntimeError, match="not been entered"):
        await uow.rollback()
    await uow.__aexit__(None, None, None)

    async with uow:
        assert fake.began
    assert fake.rolled_back
    assert fake.closed


@pytest.mark.unit
async def test_uow_commit_and_exception_paths() -> None:
    committed = FakeSession()
    async with SqlAlchemyUnitOfWork(
        cast(async_sessionmaker[AsyncSession], lambda: committed)
    ) as uow:
        await uow.commit()
    assert committed.committed
    assert not committed.rolled_back

    failed = FakeSession()
    with pytest.raises(ValueError, match="stop"):
        async with SqlAlchemyUnitOfWork(cast(async_sessionmaker[AsyncSession], lambda: failed)):
            raise ValueError("stop")
    assert failed.rolled_back
    assert failed.closed


@pytest.mark.unit
async def test_explicit_rollback_and_advisory_lock_validation() -> None:
    fake = FakeSession()
    async with SqlAlchemyUnitOfWork(cast(async_sessionmaker[AsyncSession], lambda: fake)) as uow:
        await uow.rollback()
    assert fake.rolled_back

    account_id = uuid7()
    entity_id = uuid7()
    session = cast(AsyncSession, fake)
    assert await try_advisory_scope_lock(
        session, scope="conversation", account_id=account_id, entity_id=entity_id
    )
    fake.scalar_value = False
    assert not await try_advisory_scope_lock(
        session, scope="conversation", account_id=account_id, entity_id=entity_id
    )
    with pytest.raises(ValueError, match="stable identifier"):
        await try_advisory_scope_lock(
            session, scope="bad scope", account_id=account_id, entity_id=entity_id
        )
