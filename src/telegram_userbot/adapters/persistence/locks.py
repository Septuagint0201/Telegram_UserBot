"""Transaction-scoped PostgreSQL advisory locks."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def try_advisory_scope_lock(
    session: AsyncSession,
    *,
    scope: str,
    account_id: UUID,
    entity_id: UUID,
) -> bool:
    if not scope or not scope.replace("_", "").isalnum():
        raise ValueError("scope must be a stable identifier")
    lock_key = f"{scope}:{account_id}:{entity_id}"
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )
    return acquired is True
