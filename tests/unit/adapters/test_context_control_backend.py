from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    PreviewDeletionRecord,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.telegram_bot import (
    DurableContextControlBackend,
    ExactManifestPreviewRebuilder,
    PreviewSendUnknownError,
)
from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationTargetTokenCodec,
)
from telegram_userbot.domain.shared.hashing import JsonValue, stable_json_bytes
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 1, tzinfo=UTC)
REQUEST = PreviewRequestRecord(
    UUID(int=1),
    UUID(int=2),
    b"m" * 32,
    b"s" * 32,
    "confirmed",
    42,
    42,
    "control-bot",
)


class RepositoryFake(ContextRepository):
    def __init__(self) -> None:
        self.deliveries: list[tuple[tuple[str, int | None], ...]] = []
        self.deletion_results: list[tuple[bool, str | None]] = []

    async def consume_preview(self, **kwargs: object) -> PreviewRequestRecord | None:
        return REQUEST

    async def record_preview_delivery(
        self,
        *,
        request: PreviewRequestRecord,
        states: tuple[tuple[str, int | None], ...],
        now: datetime,
        delete_after: datetime,
    ) -> None:
        self.deliveries.append(states)

    async def due_preview_deletions(
        self, *, bot_identity: str, now: datetime, limit: int = 50
    ) -> tuple[PreviewDeletionRecord, ...]:
        return (PreviewDeletionRecord(7, REQUEST.request_id, 42, 99),)

    async def finish_preview_deletion(
        self,
        *,
        deletion: PreviewDeletionRecord,
        deleted: bool,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        self.deletion_results.append((deleted, error_code))


class RebuilderFake:
    def __init__(self, chunks: tuple[str, ...] = ("SYNTHETIC_REDACTED",)) -> None:
        self.chunks = chunks
        self.error: ValueError | None = None

    async def rebuild_redacted(
        self, *, request: PreviewRequestRecord
    ) -> tuple[SensitiveValue[str], ...]:
        if self.error is not None:
            raise self.error
        return tuple(SensitiveValue(item) for item in self.chunks)


class GatewayFake:
    def __init__(self, *, send_unknown: bool = False, delete_fails: bool = False) -> None:
        self.send_unknown = send_unknown
        self.delete_fails = delete_fails
        self.sent = 0

    async def send_text(self, *, bot_chat_id: int, text: SensitiveValue[str]) -> int:
        self.sent += 1
        if self.send_unknown:
            raise PreviewSendUnknownError("synthetic")
        return 100 + self.sent

    async def delete_message(self, *, bot_chat_id: int, bot_message_id: int) -> None:
        if self.delete_fails:
            raise RuntimeError("synthetic delete failure")


class MappingResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> MappingResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class RebuildSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def execute(self, statement: object, parameters: object) -> MappingResult:
        return MappingResult(self.rows)


def backend(
    repository: RepositoryFake,
    rebuilder: RebuilderFake,
    gateway: GatewayFake,
    *,
    max_chunks: int = 8,
    on_delete_failure: Callable[[str], None] | None = None,
) -> DurableContextControlBackend:
    return DurableContextControlBackend(
        repository=repository,
        target_tokens=ConversationTargetTokenCodec(
            SensitiveValue(b"k" * 32), deployment_id="m5-test"
        ),
        rebuilder=rebuilder,
        gateway=gateway,
        max_chunks=max_chunks,
        on_delete_failure=on_delete_failure,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_revalidates_sources_and_rejects_chunk_overflow() -> None:
    repository = RepositoryFake()
    rebuilder = RebuilderFake(("1234", "5678"))
    service = backend(repository, rebuilder, GatewayFake(), max_chunks=1)
    with pytest.raises(ValueError, match="chunk_overflow"):
        await service.confirm_preview(
            admin_id=42,
            bot_chat_id=42,
            confirmation_token=SensitiveValue("token"),
            now=NOW,
        )
    rebuilder.error = ValueError("context_source_unavailable")
    service = backend(repository, rebuilder, GatewayFake())
    with pytest.raises(ValueError, match="source_unavailable"):
        await service.confirm_preview(
            admin_id=42,
            bot_chat_id=42,
            confirmation_token=SensitiveValue("token"),
            now=NOW,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_manifest_rebuilder_checks_vector_content_render_and_eligibility() -> None:
    source_id = UUID(int=9)
    content = "SYNTHETIC_REDACTED"
    rendered = (
        f"[CONTEXT_DATA layer=current source={source_id} trust=untrusted_user]\n"
        f"{content}\n[/CONTEXT_DATA]"
    )
    vector = [
        {
            "source_type": "message_revision",
            "source_id": str(source_id),
            "revision": "revision-1",
        }
    ]
    vector_hash = sha256(stable_json_bytes(cast(JsonValue, vector))).digest()
    request = PreviewRequestRecord(
        REQUEST.request_id,
        REQUEST.manifest_id,
        REQUEST.manifest_sha256,
        vector_hash,
        REQUEST.state,
        REQUEST.admin_user_id,
        REQUEST.bot_chat_id,
        REQUEST.bot_identity,
    )
    row: dict[str, object] = {
        "ordinal": 1,
        "layer": "current",
        "canonical_role": "user",
        "source_actor": "contact",
        "source_type": "message_revision",
        "source_id": source_id,
        "source_revision": "revision-1",
        "trust_level": "untrusted_user",
        "image_detail": None,
        "content_sha256": sha256(content.encode()).digest(),
        "rendered_part_sha256": sha256(rendered.encode()).digest(),
        "source_content": content,
        "source_eligible": True,
        "source_revision_vector_sha256": vector_hash,
    }
    session = RebuildSession([row])
    chunks = await ExactManifestPreviewRebuilder(
        cast("AsyncSession", session), max_chunk_chars=10_000
    ).rebuild_redacted(request=request)
    assert chunks[0].reveal_for_use() == f"[user]\n{rendered}"
    row["source_eligible"] = False
    with pytest.raises(ValueError, match="source_revision_changed"):
        await ExactManifestPreviewRebuilder(cast("AsyncSession", session)).rebuild_redacted(
            request=request
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_send_unknown_stops_without_retry() -> None:
    repository = RepositoryFake()
    gateway = GatewayFake(send_unknown=True)
    result = await backend(repository, RebuilderFake(), gateway).deliver_preview(
        request=REQUEST,
        chunks=(SensitiveValue("one"), SensitiveValue("two")),
        now=NOW,
    )
    assert result.state == "send_unknown"
    assert gateway.sent == 1
    assert repository.deliveries == [(("send_unknown", None),)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_delete_failure_is_persisted_and_alerted() -> None:
    repository = RepositoryFake()
    alerts: list[str] = []
    service = backend(
        repository,
        RebuilderFake(),
        GatewayFake(delete_fails=True),
        on_delete_failure=alerts.append,
    )
    assert await service.delete_due(now=NOW) == 0
    assert repository.deletion_results == [(False, "preview_delete_failed")]
    assert alerts == ["preview_delete_failed"]
