import hashlib
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Self, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    PreviewDeletionRecord,
    PreviewRequestRecord,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FakeResult:
    def __init__(self, *, rows: Sequence[Any] = (), scalars: Sequence[object] = ()) -> None:
        self.rows = list(rows)
        self.scalar_rows = tuple(scalars)

    def mappings(self) -> Self:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def scalars(self) -> tuple[object, ...]:
        return self.scalar_rows


class FakeSession:
    def __init__(
        self,
        *,
        results: Sequence[FakeResult] = (),
        scalars: Sequence[object | None] = (),
        scalar_sequences: Sequence[Sequence[object]] = (),
    ) -> None:
        self.results = deque(results)
        self.scalar_values = deque(scalars)
        self.scalar_sequences = deque(tuple(item) for item in scalar_sequences)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.popleft() if self.results else FakeResult()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_values.popleft() if self.scalar_values else None

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.statements.append(statement)
        return self.scalar_sequences.popleft() if self.scalar_sequences else ()


@pytest.mark.unit
async def test_context_preview_consume_delivery_and_deletion_state_branches() -> None:
    request_id = UUID(int=1)
    manifest_id = UUID(int=2)
    token_id = UUID(int=3)
    token_hash = hashlib.sha256(b"token").digest()
    matched = {
        "id": token_id,
        "request_id": request_id,
        "context_manifest_id": manifest_id,
        "manifest_sha256": b"m" * 32,
        "source_revision_vector_sha256": b"s" * 32,
        "token_hash": token_hash,
    }
    fake = FakeSession(
        results=(FakeResult(rows=(matched,)),),
        scalars=(token_id, request_id),
    )
    repository = ContextRepository(cast(AsyncSession, fake))
    consumed = await repository.consume_preview(
        token=SensitiveValue("token"),
        admin_user_id=42,
        bot_chat_id=42,
        bot_identity="control-bot",
        now=NOW,
    )
    assert consumed is not None
    assert consumed.request_id == request_id

    missing = ContextRepository(
        cast(AsyncSession, FakeSession(results=(FakeResult(rows=(matched,)),)))
    )
    assert (
        await missing.consume_preview(
            token=SensitiveValue("wrong"),
            admin_user_id=42,
            bot_chat_id=42,
            bot_identity="control-bot",
            now=NOW,
        )
        is None
    )

    request = PreviewRequestRecord(
        request_id,
        manifest_id,
        b"m" * 32,
        b"s" * 32,
        "confirmed",
        42,
        42,
        "control-bot",
    )
    delivery_fake = FakeSession()
    delivery_repository = ContextRepository(cast(AsyncSession, delivery_fake))
    await delivery_repository.record_preview_delivery(
        request=request,
        states=(("sent", 100), ("send_unknown", None)),
        now=NOW,
        delete_after=NOW,
    )
    assert len(delivery_fake.statements) == 3

    deletion = PreviewDeletionRecord(7, request_id, 42, 100)
    for states in (("deleted",), ("delete_failed",), ("sent",)):
        deletion_fake = FakeSession(scalar_sequences=(states,))
        await ContextRepository(cast(AsyncSession, deletion_fake)).finish_preview_deletion(
            deletion=deletion,
            deleted=states == ("deleted",),
            now=NOW,
            error_code="synthetic" if states == ("delete_failed",) else None,
        )
        assert len(deletion_fake.statements) == 3
