from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    ContextSummaryRecord,
    PreviewChallenge,
    PreviewDeletionRecord,
    PreviewDeliveryRecord,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.telegram_bot import (
    DurableContextControlBackend,
    ExactManifestPreviewRebuilder,
    PreviewDeliveryResult,
    PreviewSendRejectedError,
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
        self.deliveries: list[tuple[int, str, int | None]] = []
        self.deletion_results: list[tuple[bool, str | None]] = []
        self.claim_result = True
        self.commit_count = 0
        self.initial_states: tuple[str, ...] = ()
        self.prepared_count = 0
        self.summary_record: ContextSummaryRecord | None = None
        self.preview_challenge = PreviewChallenge(
            UUID(int=3), "00000000", NOW + timedelta(minutes=1), SensitiveValue("challenge")
        )
        self.due_deletions: tuple[PreviewDeletionRecord, ...] = (
            PreviewDeletionRecord(7, REQUEST.request_id, REQUEST.bot_identity, 42, 99, 1),
        )

    async def latest_summary(self, **kwargs: object) -> ContextSummaryRecord | None:
        return self.summary_record

    async def issue_preview(self, **kwargs: object) -> PreviewChallenge:
        return self.preview_challenge

    async def consume_preview(self, **kwargs: object) -> PreviewRequestRecord | None:
        return REQUEST

    async def commit_preview_boundary(self) -> None:
        self.commit_count += 1

    async def begin_preview_delivery(
        self,
        *,
        request: PreviewRequestRecord,
        chunk_count: int,
        now: datetime,
        delete_after: datetime,
    ) -> tuple[PreviewDeliveryRecord, ...] | None:
        if not self.claim_result or request != REQUEST:
            return None
        self.prepared_count = chunk_count
        states = self.initial_states or ("pending",) * chunk_count
        return tuple(
            PreviewDeliveryRecord(index, index, state, 100 + index if state == "sent" else None)
            for index, state in enumerate(states, 1)
        )

    async def claim_preview_delivery_chunk(
        self, *, request: PreviewRequestRecord, ordinal: int
    ) -> bool:
        return self.claim_result and request == REQUEST and ordinal > 0

    async def retry_preview_delivery_chunk(
        self, *, request: PreviewRequestRecord, ordinal: int
    ) -> None:
        self.deliveries.append((ordinal, "pending", None))

    async def record_preview_delivery_chunk(  # noqa: PLR0913 - mirrors the repository boundary
        self,
        *,
        request: PreviewRequestRecord,
        ordinal: int,
        state: str,
        message_id: int | None,
        now: datetime,
        delete_after: datetime,
    ) -> None:
        self.deliveries.append((ordinal, state, message_id))

    async def finish_preview_delivery(
        self, *, request: PreviewRequestRecord, now: datetime
    ) -> tuple[str, int, int]:
        states = self.initial_states or ()
        sent = sum(state == "sent" for state in states) + sum(
            state == "sent" for _, state, _ in self.deliveries
        )
        unknown = "send_unknown" in states or any(
            state == "send_unknown" for _, state, _ in self.deliveries
        )
        total = len(states) if states else self.prepared_count
        return ("send_unknown" if unknown else "delivered", sent, total)

    async def due_preview_deletions(
        self,
        *,
        bot_identity: str,
        now: datetime,
        limit: int = 50,
        lease: timedelta = timedelta(minutes=1),
    ) -> tuple[PreviewDeletionRecord, ...]:
        return self.due_deletions

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
    def __init__(
        self,
        *,
        send_unknown: bool = False,
        send_rejected: bool = False,
        send_unexpected: bool = False,
        delete_fails: bool = False,
    ) -> None:
        self.send_unknown = send_unknown
        self.send_rejected = send_rejected
        self.send_unexpected = send_unexpected
        self.delete_fails = delete_fails
        self.sent = 0

    async def send_text(self, *, bot_chat_id: int, text: SensitiveValue[str]) -> int:
        self.sent += 1
        if self.send_rejected:
            raise PreviewSendRejectedError("synthetic")
        if self.send_unknown:
            raise PreviewSendUnknownError("synthetic")
        if self.send_unexpected:
            raise RuntimeError("synthetic unexpected failure")
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
        target_tokens=target_codec(),
        rebuilder=rebuilder,
        gateway=gateway,
        max_chunks=max_chunks,
        on_delete_failure=on_delete_failure,
    )


def target_codec() -> ConversationTargetTokenCodec:
    return ConversationTargetTokenCodec(SensitiveValue(b"k" * 32), deployment_id="m5-test")


@pytest.mark.unit
def test_durable_preview_settings_are_bounded_before_side_effects() -> None:
    repository = RepositoryFake()
    rebuilder = RebuilderFake()
    gateway = GatewayFake()
    invalid = (
        {"bot_identity": " "},
        {"max_chunk_chars": 4_097},
        {"max_chunks": 9},
        {"delete_after": timedelta(0)},
        {"delete_after": timedelta(minutes=10, seconds=1)},
    )
    for overrides in invalid:
        with pytest.raises(ValueError, match="settings are invalid"):
            DurableContextControlBackend(
                repository=repository,
                target_tokens=ConversationTargetTokenCodec(
                    SensitiveValue(b"k" * 32), deployment_id="m5-test"
                ),
                rebuilder=rebuilder,
                gateway=gateway,
                **overrides,
            )
    with pytest.raises(ValueError, match="chunk size must be positive"):
        ExactManifestPreviewRebuilder(cast(AsyncSession, object()), max_chunk_chars=0)


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
async def test_durable_preview_issue_requires_current_manifest() -> None:
    repository = RepositoryFake()
    codec = target_codec()
    token = codec.issue(
        account_id=UUID(int=10),
        conversation_id=UUID(int=11),
        admin_id=42,
        bot_chat_id=42,
        expires_at=NOW + timedelta(minutes=1),
    )
    service = DurableContextControlBackend(
        repository=repository,
        target_tokens=codec,
        rebuilder=RebuilderFake(),
        gateway=GatewayFake(),
    )
    with pytest.raises(ValueError, match="manifest_unavailable"):
        await service.issue_preview(admin_id=42, bot_chat_id=42, target_token=token, now=NOW)
    repository.summary_record = ContextSummaryRecord(
        UUID(int=12),
        "00000000",
        "main_response",
        "main",
        100,
        50,
        0,
        0,
        "fresh",
        "builder-v1",
        "context-v1",
        "retrieval-v1",
        "tokens-v1",
        {},
        {},
        NOW,
    )
    assert (
        await service.issue_preview(admin_id=42, bot_chat_id=42, target_token=token, now=NOW)
        == repository.preview_challenge
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
    assert repository.deliveries == [(1, "send_unknown", None)]
    assert repository.commit_count == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_claim_conflict_prevents_bot_side_effect() -> None:
    repository = RepositoryFake()
    repository.claim_result = False
    gateway = GatewayFake()
    with pytest.raises(RuntimeError, match="delivery_conflict"):
        await backend(repository, RebuilderFake(), gateway).deliver_preview(
            request=REQUEST,
            chunks=(SensitiveValue("one"),),
            now=NOW,
        )
    assert gateway.sent == 0
    assert repository.deliveries == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_reentry_never_replays_sent_or_inflight_chunks() -> None:
    repository = RepositoryFake()
    repository.initial_states = ("sent", "sending", "pending")
    gateway = GatewayFake()
    result = await backend(repository, RebuilderFake(), gateway).deliver_preview(
        request=REQUEST,
        chunks=(SensitiveValue("one"), SensitiveValue("two"), SensitiveValue("three")),
        now=NOW,
    )
    assert result == PreviewDeliveryResult("send_unknown", 1, 3)
    assert gateway.sent == 0
    assert repository.deliveries == [(2, "send_unknown", None)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_explicit_rejection_restores_retryable_ordinal() -> None:
    repository = RepositoryFake()
    gateway = GatewayFake(send_rejected=True)
    with pytest.raises(PreviewSendRejectedError):
        await backend(repository, RebuilderFake(), gateway).deliver_preview(
            request=REQUEST,
            chunks=(SensitiveValue("one"),),
            now=NOW,
        )
    assert repository.deliveries == [(1, "pending", None)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_preview_unexpected_gateway_failure_is_fail_closed() -> None:
    repository = RepositoryFake()
    result = await backend(
        repository, RebuilderFake(), GatewayFake(send_unexpected=True)
    ).deliver_preview(
        request=REQUEST,
        chunks=(SensitiveValue("one"), SensitiveValue("two")),
        now=NOW,
    )
    assert result.state == "send_unknown"
    assert repository.deliveries == [(1, "send_unknown", None)]


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
    assert repository.commit_count == 2

    repository.due_deletions = ()
    assert await service.delete_due(now=NOW) == 0
