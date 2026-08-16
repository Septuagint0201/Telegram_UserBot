import hashlib
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    PreviewDeletionRecord,
    PreviewRequestRecord,
    _scores_match,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.unit
def test_manifest_score_replay_distinguishes_missing_and_quantized_values() -> None:
    assert _scores_match(None, None)
    assert not _scores_match("0.5", None)
    assert not _scores_match(None, 0.5)
    assert _scores_match("0.5000000", 0.5)


class FakeResult:
    def __init__(self, *, rows: Sequence[Any] = (), scalars: Sequence[object] = ()) -> None:
        self.rows = list(rows)
        self.scalar_rows = tuple(scalars)

    def mappings(self) -> Self:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def one_or_none(self) -> Any | None:
        if len(self.rows) > 1:
            raise AssertionError("fake result contains multiple rows")
        return self.rows[0] if self.rows else None

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
        self.commit_count = 0

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.popleft() if self.results else FakeResult()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_values.popleft() if self.scalar_values else None

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.statements.append(statement)
        return self.scalar_sequences.popleft() if self.scalar_sequences else ()

    async def commit(self) -> None:
        self.commit_count += 1


def source_item(
    source_type: str,
    source_id: UUID,
    source_revision: str,
    content: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_type=source_type,
        source_id=str(source_id),
        source_revision=source_revision,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


@pytest.mark.unit
async def test_manifest_sources_must_be_current_and_content_bound() -> None:
    account_id = uuid4()

    instruction_id = uuid4()
    instruction = "SYNTHETIC_INSTRUCTION"
    instruction_hash = hashlib.sha256(instruction.encode()).digest()
    repository = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(
                        rows=(
                            {
                                "version_no": 2,
                                "template_sha256": instruction_hash,
                                "template_body": instruction,
                            },
                        )
                    ),
                )
            ),
        )
    )
    await repository._require_manifest_source_current(
        source_item("trusted_instruction", instruction_id, "version-2", instruction),
        account_id=account_id,
        now=NOW,
    )

    revision_id = uuid4()
    for text_content, caption, content in (
        ("SYNTHETIC_TEXT", None, "SYNTHETIC_TEXT"),
        (None, "SYNTHETIC_CAPTION", "SYNTHETIC_CAPTION"),
    ):
        content_hash = hashlib.sha256(content.encode()).digest()
        repository = ContextRepository(
            cast(
                AsyncSession,
                FakeSession(
                    results=(
                        FakeResult(
                            rows=(
                                {
                                    "revision_no": 3,
                                    "content_sha256": content_hash,
                                    "text_content": text_content,
                                    "caption": caption,
                                    "redacted_at": None,
                                    "current_revision_no": 3,
                                    "deleted_at": None,
                                    "is_tombstone": False,
                                },
                            )
                        ),
                    )
                ),
            )
        )
        await repository._require_manifest_source_current(
            source_item("message_revision", revision_id, "revision-3", content),
            account_id=account_id,
            now=NOW,
        )

    media_id, parent_id = uuid4(), uuid4()
    media_sha = b"m" * 32
    media_content = (
        f"[IMAGE media_object_id={media_id} sha256={media_sha.hex()} "
        "mime=image/png width=640 height=480 detail=auto]"
    )
    repository = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(
                        rows=(
                            {
                                "id": media_id,
                                "parent_object_id": parent_id,
                                "status": "ready",
                                "sha256": media_sha,
                                "validated_mime": "image/png",
                                "width": 640,
                                "height": 480,
                                "expires_at": NOW + timedelta(minutes=1),
                            },
                        )
                    ),
                ),
                scalars=(1,),
            ),
        )
    )
    await repository._require_manifest_source_current(
        source_item("media_object", media_id, f"sha256-{media_sha.hex()}", media_content),
        account_id=account_id,
        now=NOW,
    )

    memory_id = uuid4()
    memory_content = "SYNTHETIC_MEMORY"
    repository = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(
                        rows=(
                            {
                                "version_no": 4,
                                "rendered_text": memory_content,
                                "redacted_at": None,
                                "current_version_no": 4,
                                "status": "active",
                            },
                        )
                    ),
                )
            ),
        )
    )
    await repository._require_manifest_source_current(
        source_item("memory_version", memory_id, "version-4", memory_content),
        account_id=account_id,
        now=NOW,
    )

    summary_id = uuid4()
    summary_content = "SYNTHETIC_SUMMARY"
    summary_hash = hashlib.sha256(summary_content.encode()).digest()
    repository = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(
                        rows=(
                            {
                                "version_no": 5,
                                "content_text": summary_content,
                                "content_sha256": summary_hash,
                                "invalidation_state": "active",
                                "redacted_at": None,
                                "current_version_no": 5,
                                "status": "active",
                            },
                        )
                    ),
                )
            ),
        )
    )
    await repository._require_manifest_source_current(
        source_item("summary_version", summary_id, "version-5", summary_content),
        account_id=account_id,
        now=NOW,
    )

    stale = ContextRepository(cast(AsyncSession, FakeSession()))
    with pytest.raises(ValueError, match="context_manifest_source_stale"):
        await stale._require_manifest_source_current(
            source_item("unsupported", uuid4(), "version-1", "SYNTHETIC"),
            account_id=account_id,
            now=NOW,
        )


@pytest.mark.unit
async def test_manifest_sources_fail_closed_when_durable_rows_disappear() -> None:
    account_id = uuid4()
    for source_type, source_revision in (
        ("trusted_instruction", "version-1"),
        ("message_revision", "revision-1"),
        ("media_object", f"sha256-{'00' * 32}"),
        ("memory_version", "version-1"),
        ("summary_version", "version-1"),
    ):
        repository = ContextRepository(
            cast(
                AsyncSession,
                FakeSession(
                    results=(FakeResult(),),
                    scalars=(None,) if source_type == "media_object" else (),
                ),
            )
        )
        with pytest.raises(ValueError, match="context_manifest_source_stale"):
            await repository._require_manifest_source_current(
                source_item(source_type, uuid4(), source_revision, "SYNTHETIC"),
                account_id=account_id,
                now=NOW,
            )


@pytest.mark.unit
async def test_manifest_replay_requires_exact_identity_items_and_omissions() -> None:
    manifest_id, account_id = uuid4(), uuid4()
    values = {"id": manifest_id, "account_id": account_id, "created_at": NOW}
    persisted = {"id": manifest_id, "account_id": account_id}
    manifest = cast(Any, SimpleNamespace(id=manifest_id, items=(), omissions=()))
    repository = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(rows=(persisted,)),
                    FakeResult(),
                    FakeResult(),
                )
            ),
        )
    )
    await repository._require_manifest_replay(manifest=manifest, manifest_values=values)

    missing = ContextRepository(cast(AsyncSession, FakeSession(results=(FakeResult(),))))
    with pytest.raises(ValueError, match="context_manifest_replay_mismatch"):
        await missing._require_manifest_replay(manifest=manifest, manifest_values=values)

    changed_items = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(rows=(persisted,)),
                    FakeResult(),
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="context_manifest_replay_mismatch"):
        await changed_items._require_manifest_replay(
            manifest=cast(
                Any,
                SimpleNamespace(id=manifest_id, items=(object(),), omissions=()),
            ),
            manifest_values=values,
        )

    changed_omissions = ContextRepository(
        cast(
            AsyncSession,
            FakeSession(
                results=(
                    FakeResult(rows=(persisted,)),
                    FakeResult(),
                    FakeResult(),
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="context_manifest_replay_mismatch"):
        await changed_omissions._require_manifest_replay(
            manifest=cast(
                Any,
                SimpleNamespace(
                    id=manifest_id,
                    items=(),
                    omissions=("current:budget",),
                ),
            ),
            manifest_values=values,
        )


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

    missing = ContextRepository(cast(AsyncSession, FakeSession(results=(FakeResult(),))))
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
    delivery_rows = (
        {
            "id": 7,
            "ordinal": 1,
            "state": "pending",
            "bot_message_id": None,
        },
        {
            "id": 8,
            "ordinal": 2,
            "state": "pending",
            "bot_message_id": None,
        },
    )
    claim_fake = FakeSession(
        scalars=(request_id,),
        results=(FakeResult(), FakeResult(), FakeResult(rows=delivery_rows)),
    )
    claim_repository = ContextRepository(cast(AsyncSession, claim_fake))
    prepared = await claim_repository.begin_preview_delivery(
        request=request,
        chunk_count=2,
        now=NOW,
        delete_after=NOW + timedelta(minutes=10),
    )
    assert prepared is not None
    assert tuple(item.ordinal for item in prepared) == (1, 2)
    assert len(claim_fake.statements) == 4
    claim_sql = str(claim_fake.statements[0])
    assert "context_preview_requests.bot_identity" in claim_sql
    assert "context_preview_requests.bot_chat_id" in claim_sql
    assert "context_preview_requests.state" in claim_sql
    await claim_repository.commit_preview_boundary()
    assert claim_fake.commit_count == 1

    delivery_fake = FakeSession(scalars=(7, 7))
    delivery_repository = ContextRepository(cast(AsyncSession, delivery_fake))
    assert await delivery_repository.claim_preview_delivery_chunk(request=request, ordinal=1)
    await delivery_repository.record_preview_delivery_chunk(
        request=request,
        ordinal=1,
        state="sent",
        message_id=100,
        now=NOW,
        delete_after=NOW + timedelta(minutes=10),
    )
    assert len(delivery_fake.statements) == 2

    finish_fake = FakeSession(
        scalars=(request_id,),
        results=(
            FakeResult(
                rows=(
                    {
                        "state": "sent",
                        "bot_message_id": 100,
                        "delete_after": NOW + timedelta(minutes=10),
                    },
                    {"state": "send_unknown", "bot_message_id": None, "delete_after": None},
                )
            ),
        ),
    )
    assert await ContextRepository(cast(AsyncSession, finish_fake)).finish_preview_delivery(
        request=request, now=NOW
    ) == ("send_unknown", 1, 2)

    retry_fake = FakeSession(scalars=(7,))
    await ContextRepository(cast(AsyncSession, retry_fake)).retry_preview_delivery_chunk(
        request=request, ordinal=1
    )

    deletion = PreviewDeletionRecord(7, request_id, "control-bot", 42, 100, 3)
    for states in (("deleted",), ("delete_failed",), ("sent",), ("deleted", "send_unknown")):
        deletion_fake = FakeSession(
            scalars=(7, request_id),
            scalar_sequences=(states,),
        )
        await ContextRepository(cast(AsyncSession, deletion_fake)).finish_preview_deletion(
            deletion=deletion,
            deleted=states in {("deleted",), ("deleted", "send_unknown")},
            now=NOW,
            error_code="synthetic" if states == ("delete_failed",) else None,
        )
        assert len(deletion_fake.statements) == 3
        deletion_sql = str(deletion_fake.statements[0])
        assert "context_preview_deliveries.request_id" in deletion_sql
        assert "context_preview_deliveries.bot_identity" in deletion_sql
        assert "context_preview_deliveries.bot_message_id" in deletion_sql
        assert "context_preview_deliveries.delete_fencing_token" in deletion_sql

    conflict = FakeSession(scalars=(None,))
    with pytest.raises(RuntimeError, match="deletion_conflict"):
        await ContextRepository(cast(AsyncSession, conflict)).finish_preview_deletion(
            deletion=deletion,
            deleted=True,
            now=NOW,
        )


@pytest.mark.unit
async def test_context_repository_rejects_invalid_owner_and_naive_times_before_sql() -> None:
    fake = FakeSession()
    repository = ContextRepository(cast(AsyncSession, fake))
    account_id, conversation_id, owner_id = UUID(int=1), UUID(int=2), UUID(int=3)
    naive = NOW.replace(tzinfo=None)

    manifest_kwargs = {
        "account_id": account_id,
        "conversation_id": conversation_id,
        "turn_id": owner_id,
        "background_job_id": None,
        "context_policy_version_id": UUID(int=4),
        "retrieval_policy_version_id": UUID(int=5),
        "prompt_bundle_sha256": b"p" * 32,
        "capability_snapshot_sha256": b"c" * 32,
        "manifest": cast(Any, None),
    }
    with pytest.raises(ValueError, match="created_at must be timezone-aware"):
        await repository.save_manifest(**manifest_kwargs, created_at=naive)
    for owner_fields in (
        {"turn_id": None, "background_job_id": None},
        {"turn_id": owner_id, "background_job_id": UUID(int=6)},
    ):
        with pytest.raises(ValueError, match="context_manifest_owner_required"):
            await repository.save_manifest(
                **(manifest_kwargs | owner_fields),
                created_at=NOW,
            )

    request = PreviewRequestRecord(
        UUID(int=7), UUID(int=8), b"m" * 32, b"s" * 32, "confirmed", 42, 42, "bot"
    )
    deletion = PreviewDeletionRecord(1, request.request_id, "bot", 42, 9, 1)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        await repository.issue_preview(
            account_id=account_id,
            conversation_id=conversation_id,
            manifest_id=request.manifest_id,
            admin_user_id=42,
            bot_chat_id=42,
            bot_identity="bot",
            now=naive,
        )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        await repository.consume_preview(
            token=SensitiveValue("token"),
            admin_user_id=42,
            bot_chat_id=42,
            bot_identity="bot",
            now=naive,
        )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        await repository.record_preview_delivery_chunk(
            request=request,
            ordinal=1,
            state="sent",
            message_id=9,
            now=naive,
            delete_after=NOW,
        )
    with pytest.raises(ValueError, match="delete_after must be timezone-aware"):
        await repository.record_preview_delivery_chunk(
            request=request,
            ordinal=1,
            state="sent",
            message_id=9,
            now=NOW,
            delete_after=naive,
        )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        await repository.due_preview_deletions(bot_identity="bot", now=naive)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        await repository.finish_preview_deletion(deletion=deletion, deleted=True, now=naive)

    assert fake.statements == []


@pytest.mark.unit
async def test_context_preview_rejects_unbounded_or_incoherent_delivery_before_sql() -> None:
    repository = ContextRepository(cast(AsyncSession, FakeSession()))
    request = PreviewRequestRecord(
        UUID(int=7), UUID(int=8), b"m" * 32, b"s" * 32, "confirmed", 42, 42, "bot"
    )
    for ttl in (timedelta(0), timedelta(seconds=-1), timedelta(minutes=5, seconds=1)):
        with pytest.raises(ValueError, match="context_preview_not_allowed"):
            await repository.issue_preview(
                account_id=UUID(int=1),
                conversation_id=UUID(int=2),
                manifest_id=request.manifest_id,
                admin_user_id=42,
                bot_chat_id=42,
                bot_identity="bot",
                now=NOW,
                ttl=ttl,
            )
    for chunk_count, delete_after in (
        (0, NOW + timedelta(minutes=10)),
        (9, NOW + timedelta(minutes=10)),
        (1, NOW),
        (1, NOW + timedelta(minutes=10, seconds=1)),
    ):
        with pytest.raises(ValueError, match="context_preview_delivery_invalid"):
            await repository.begin_preview_delivery(
                request=request,
                chunk_count=chunk_count,
                now=NOW,
                delete_after=delete_after,
            )
    for ordinal, state, message_id, delete_after in (
        (0, "sent", 9, NOW + timedelta(minutes=10)),
        (1, "invalid", 9, NOW + timedelta(minutes=10)),
        (1, "sent", None, NOW + timedelta(minutes=10)),
        (1, "sent", 0, NOW + timedelta(minutes=10)),
        (1, "send_unknown", 9, NOW + timedelta(minutes=10)),
        (1, "sent", 9, NOW),
        (1, "sent", 9, NOW + timedelta(minutes=10, seconds=1)),
    ):
        with pytest.raises(ValueError, match="context_preview_delivery_invalid"):
            await repository.record_preview_delivery_chunk(
                request=request,
                ordinal=ordinal,
                state=state,
                message_id=message_id,
                now=NOW,
                delete_after=delete_after,
            )


@pytest.mark.unit
async def test_context_preview_delivery_cas_and_completion_conflicts_fail_closed() -> None:
    request = PreviewRequestRecord(
        UUID(int=7), UUID(int=8), b"m" * 32, b"s" * 32, "confirmed", 42, 42, "bot"
    )
    with pytest.raises(RuntimeError, match="delivery_conflict"):
        await ContextRepository(
            cast(AsyncSession, FakeSession(scalars=(None,)))
        ).retry_preview_delivery_chunk(request=request, ordinal=1)
    with pytest.raises(RuntimeError, match="delivery_conflict"):
        await ContextRepository(
            cast(AsyncSession, FakeSession(scalars=(None,)))
        ).record_preview_delivery_chunk(
            request=request,
            ordinal=1,
            state="sent",
            message_id=9,
            now=NOW,
            delete_after=NOW + timedelta(minutes=10),
        )
    for rows in ((), ({"state": "pending", "bot_message_id": None, "delete_after": None},)):
        with pytest.raises(RuntimeError, match="delivery_conflict"):
            await ContextRepository(
                cast(AsyncSession, FakeSession(results=(FakeResult(rows=rows),)))
            ).finish_preview_delivery(request=request, now=NOW)
