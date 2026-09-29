"""Private image files, real SQL fences, provider wire and canonical candidates."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid7

import pytest
from PIL import Image
from sqlalchemy import delete, insert, null, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_pipeline import MemoryPipelineRepository
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.memory_results import MemoryResultRepository
from telegram_userbot.adapters.persistence.worker_runtime import WorkerJobRepository
from telegram_userbot.domain.memory import EventRange
from telegram_userbot.domain.messaging.events import BodyKind, MessageBody
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.processes.memory_pipeline import MemoryPipelineExecutor
from telegram_userbot.processes.memory_runtime import MemoryReviewRuntimeService
from telegram_userbot.processes.model_gateway import PrivateMediaRuntimeImageLoader
from telegram_userbot.processes.model_output_schema import (
    DEFAULT_MODEL_OUTPUT_SCHEMAS,
    ModelOutputParseContext,
)
from telegram_userbot.processes.worker_executors import JobExecutionError
from tests.integration.test_m5_context_media import NOW
from tests.integration.test_m8_embedding_runtime import KEYRING, Resolver
from tests.integration.test_m8_memory_runtime import (
    RUN_AT,
    SECRET,
    Transport,
    context,
    seed,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_image(
    session: AsyncSession, root: Path, *, image_only: bool = False, kind: str = "episode"
) -> tuple[UUID, UUID, UUID, UUID, UUID, Path]:
    account, conversation, revision, job_id = await seed(session, kind=kind, supports_images=True)
    previous = (
        (
            await session.execute(
                select(s.message_revisions).where(s.message_revisions.c.id == revision)
            )
        )
        .mappings()
        .one()
    )
    revision = uuid7()
    body = (
        MessageBody(BodyKind.NONE)
        if image_only
        else MessageBody(BodyKind.CAPTION, "Synthetic image caption")
    )
    await session.execute(
        insert(s.message_revisions).values(
            id=revision,
            account_id=account,
            message_id=previous["message_id"],
            revision_no=2,
            body_kind=body.kind.value,
            caption=body.text,
            entities_schema_version=1,
            entities=[],
            content_sha256=body.content_sha256,
            source_event_id=previous["source_event_id"],
        )
    )
    await session.execute(
        update(s.messages)
        .where(s.messages.c.id == previous["message_id"])
        .values(current_revision_no=2)
    )
    image_id = uuid7()
    storage_key = f"{account}/{image_id}.png"
    payload = io.BytesIO()
    Image.new("RGB", (3, 2), "green").save(payload, format="PNG")
    raw = payload.getvalue()
    path = root / storage_key
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    await session.execute(
        insert(s.media_objects).values(
            id=image_id,
            account_id=account,
            object_kind="provider_copy",
            status="ready",
            source_revision_id=revision,
            storage_key=storage_key,
            sha256=hashlib.sha256(raw).digest(),
            validated_mime="image/png",
            byte_size=len(raw),
            width=3,
            height=2,
            retention_class="media_provider_copy_24h",
            ready_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    )
    await session.execute(
        insert(s.message_media).values(
            id=uuid7(),
            account_id=account,
            message_revision_id=revision,
            media_object_id=image_id,
            media_kind="photo",
            position=0,
            metadata_schema_version=1,
        )
    )
    return account, conversation, revision, job_id, image_id, path


def executor(transport: Transport, root: Path, *, now: Any = RUN_AT) -> MemoryPipelineExecutor:
    return MemoryPipelineExecutor(
        keyring=KEYRING,
        fingerprint_secret=SensitiveValue(SECRET),
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: now,
        image_loader=PrivateMediaRuntimeImageLoader(root),
    )


@pytest.mark.integration
@pytest.mark.parametrize("image_only", [False, True])
async def test_image_result_is_candidate_even_when_provider_omits_visual_flag(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path, image_only: bool
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, revision, job_id, image_id, _ = await seed_image(
            session, tmp_path, image_only=image_only
        )
    work = await context(sessions, job_id)
    transport = Transport()
    await executor(transport, tmp_path)(work)
    content = transport.requests[0].body.reveal_for_use()["messages"][1]["content"]
    assert [item["type"] for item in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert str(tmp_path) not in json.dumps(content)
    async with sessions() as session:
        proposal = (await session.execute(select(s.memory_proposals))).mappings().one()
        assert proposal["state"] == "candidate"
        assert proposal["visual_only"] is True
        assert await session.scalar(select(s.memories.c.id)) is None
        assert (
            await session.scalar(select(s.memory_watermarks.c.last_contiguous_decided_event_id))
            == 1
        )
        assert await session.scalar(select(s.memory_input_manifests.c.image_count)) == 1
        assert (
            await session.scalar(
                select(s.memory_input_manifest_items.c.media_object_id).where(
                    s.memory_input_manifest_items.c.source_type == "media_object"
                )
            )
            == image_id
        )
        assert (
            await session.scalar(select(s.memory_proposal_evidence.c.message_revision_id))
            == revision
        )
        if image_only:
            # The seed has one old pending chunk; no new chunk was produced for
            # an empty body and no formal candidate embedding exists.
            assert len((await session.execute(select(s.embedding_records.c.id))).all()) == 1


@pytest.mark.integration
async def test_worker_role_can_publish_image_only_summary(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, revision, job_id, _, _ = await seed_image(
            session, tmp_path, image_only=True, kind="rolling_summary"
        )
    work = await context(sessions, job_id)
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        prepared = await MemoryPipelineRepository(session).prepare(
            work.job, secret=SECRET, now=RUN_AT
        )
        assert prepared is not None
        assert prepared.images
    generated = await executor(Transport(), tmp_path)._generate(prepared)
    raw = generated.text.reveal_for_use()
    output = DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 2, purpose="memory_rolling_summary"
    ).parse(
        raw,
        context=ModelOutputParseContext(
            prepared.manifest.account_id, prepared.manifest.conversation_id, RUN_AT
        ),
    )
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        await MemoryResultRepository(session).complete(
            work.job,
            prepared,
            output,
            raw=raw,
            input_tokens=1,
            output_tokens=1,
            secret=SECRET,
            now=RUN_AT,
        )
        assert (
            await session.scalar(select(s.summary_version_sources.c.message_revision_id))
            == revision
        )
        assert await session.scalar(select(s.summary_watermarks.c.last_included_event_id)) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "mode", ["bytes", "unlink", "erase", "expires", "position", "detach", "attach", "redact"]
)
async def test_late_image_change_cannot_commit_or_advance_watermark(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path, mode: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, _, revision, job_id, image_id, path = await seed_image(session, tmp_path)
    work = await context(sessions, job_id)

    mutation_committed = False

    async def change() -> None:
        nonlocal mutation_committed
        if mode == "bytes":
            path.write_bytes(b"changed-private-bytes")
            assert path.read_bytes() == b"changed-private-bytes"
        elif mode == "unlink":
            path.unlink()
            assert not path.exists()
        else:
            async with sessions() as session, session.begin():
                if mode in {"erase", "expires"}:
                    changes: dict[str, dict[str, Any]] = {
                        "erase": {"delete_requested_at": RUN_AT},
                        "expires": {"expires_at": RUN_AT},
                    }
                    values = changes[mode]
                    await session.execute(
                        update(s.media_objects)
                        .where(s.media_objects.c.id == image_id)
                        .values(**values)
                    )
                    row = (
                        (
                            await session.execute(
                                select(s.media_objects).where(s.media_objects.c.id == image_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert all(row[key] == value for key, value in values.items())
                elif mode == "redact":
                    await session.execute(
                        update(s.message_revisions)
                        .where(s.message_revisions.c.id == revision)
                        .values(
                            text_content=None,
                            caption=None,
                            entities=null(),
                            content_sha256=None,
                            redacted_at=RUN_AT,
                            redaction_reason="policy",
                        )
                    )
                    assert (
                        await session.scalar(
                            select(s.message_revisions.c.redacted_at).where(
                                s.message_revisions.c.id == revision
                            )
                        )
                        == RUN_AT
                    )
                elif mode == "position":
                    await session.execute(
                        update(s.message_media)
                        .where(s.message_media.c.media_object_id == image_id)
                        .values(position=2)
                    )
                    assert await session.scalar(select(s.message_media.c.position)) == 2
                elif mode == "detach":
                    await session.execute(
                        delete(s.message_media).where(s.message_media.c.media_object_id == image_id)
                    )
                    assert await session.scalar(select(s.message_media.c.id)) is None
                else:
                    await session.execute(
                        insert(s.message_media).values(
                            id=uuid7(),
                            account_id=account,
                            message_revision_id=revision,
                            media_kind="photo",
                            position=1,
                            metadata_schema_version=1,
                        )
                    )
                    assert len((await session.execute(select(s.message_media.c.id))).all()) == 2

        mutation_committed = True

    transport = Transport(callback=change)
    with pytest.raises(JobExecutionError) as failure:
        await executor(transport, tmp_path)(work)
    assert len(transport.requests) == 1
    assert mutation_committed
    assert (
        failure.value.code
        == {
            "bytes": "MODEL_IMAGE_SNAPSHOT_UNAVAILABLE",
            "unlink": "MODEL_IMAGE_SNAPSHOT_UNAVAILABLE",
            "erase": "MEMORY_IMAGE_UNAVAILABLE",
            "expires": "MEMORY_IMAGE_UNAVAILABLE",
            "position": "MEMORY_INPUT_CHANGED",
            "detach": "MEMORY_INPUT_CHANGED",
            "attach": "MEMORY_IMAGE_PENDING",
            "redact": "MEMORY_SOURCE_CHANGED",
        }[mode]
    )
    async with sessions() as session:
        assert await session.scalar(select(s.memory_proposals.c.id)) is None
        assert await session.scalar(select(s.memory_watermarks.c.conversation_id)) is None
        assert await session.scalar(
            select(s.memory_jobs.c.state).where(s.memory_jobs.c.id == job_id)
        ) in {"retry_wait", "dead_letter"}


@pytest.mark.integration
async def test_retry_preserves_exact_image_request(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id, _, _ = await seed_image(session, tmp_path)
    work = await context(sessions, job_id)
    transport = Transport()
    transport.status = 503
    with pytest.raises(JobExecutionError) as failure:
        await executor(transport, tmp_path)(work)
    async with sessions() as session, session.begin():
        assert work.job.lease_owner is not None
        await WorkerJobRepository(session).fail_or_retry(
            job=work.job,
            owner=work.job.lease_owner,
            now=RUN_AT,
            error_code=failure.value.code,
            retryable=True,
            retry_delay=timedelta(seconds=5),
        )
    later = RUN_AT + timedelta(seconds=6)
    retry = await context(sessions, job_id, now=later)
    transport.status = 200
    await executor(transport, tmp_path, now=later)(retry)
    assert len(transport.requests) == 2
    assert (
        transport.requests[0].body.reveal_for_use() == transport.requests[1].body.reveal_for_use()
    )


@pytest.mark.integration
@pytest.mark.parametrize("deleted", [False, True])
async def test_image_only_candidate_review_still_requires_current_canonical_root(
    isolated_scope_erasure_engine: AsyncEngine, tmp_path: Path, deleted: bool
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, conversation, _, job_id, _, _ = await seed_image(
            session, tmp_path, image_only=True
        )
    await executor(Transport(), tmp_path)(await context(sessions, job_id))
    token = b"synthetic-review-image-only-token"
    async with sessions() as session, session.begin():
        proposal = (await session.execute(select(s.memory_proposals))).mappings().one()
        repository = MemoryRepository(session)
        await repository.issue_review_action(
            account_id=account,
            conversation_id=conversation,
            admin_actor_id=42,
            bot_chat_id=42,
            action="accept",
            proposal_id=proposal["id"],
            memory_id=None,
            token=token,
            expires_at=RUN_AT + timedelta(minutes=5),
            expected_proposal_version=proposal["review_version"],
        )
    if deleted:
        async with sessions() as session, session.begin():
            await session.execute(update(s.messages).values(deleted_at=RUN_AT, is_tombstone=True))
    async with sessions() as session, session.begin():
        confirmed = await MemoryRepository(session).consume_review_action(
            token=token,
            admin_actor_id=42,
            bot_chat_id=42,
            now=RUN_AT,
        )
        assert (confirmed is None) == deleted
    runtime = MemoryReviewRuntimeService(
        sessions, erasure_scope_secret=SensitiveValue(SECRET), now=lambda: RUN_AT
    )
    execution = await runtime.run_once(account_id=account)
    if deleted:
        assert execution is None
    else:
        assert execution is not None
        assert execution.applied
    async with sessions() as session:
        assert (await session.scalar(select(s.memories.c.id)) is None) == deleted
        if not deleted:
            assert await session.scalar(select(s.memory_versions.c.acceptance_kind)) == "manual"
    if not deleted:
        # The next episode has new text but keeps the manually confirmed image
        # memory and canonical root as context. Old pixels need not survive.
        async with sessions() as session, session.begin():
            await session.execute(update(s.media_objects).values(expires_at=RUN_AT))
            template = (await session.execute(select(s.messages))).mappings().one()
            event = await session.scalar(
                insert(s.message_events)
                .values(
                    event_uuid=uuid7(),
                    account_id=account,
                    conversation_id=conversation,
                    event_kind="incoming.create",
                    telegram_message_id=99,
                    fingerprint_version=1,
                    update_fingerprint=hashlib.sha256(b"next-text").digest(),
                    ordering_key="v1:00000099",
                    metadata_schema_version=1,
                    projected_at=RUN_AT,
                )
                .returning(s.message_events.c.id)
            )
            assert isinstance(event, int)
            message = uuid7()
            await session.execute(
                insert(s.messages).values(
                    **{
                        **dict(template),
                        "id": message,
                        "telegram_message_id": 99,
                        "current_revision_no": 1,
                    }
                )
            )
            body = MessageBody(BodyKind.TEXT, "New text after image expiry")
            await session.execute(
                insert(s.message_revisions).values(
                    id=uuid7(),
                    account_id=account,
                    message_id=message,
                    revision_no=1,
                    body_kind="text",
                    text_content=body.text,
                    entities_schema_version=1,
                    entities=[],
                    content_sha256=body.content_sha256,
                    source_event_id=event,
                )
            )
            next_job = await MemoryRepository(session).refresh_pending_job(
                account_id=account,
                conversation_id=conversation,
                job_kind="episode",
                event_range=EventRange(event, event),
                estimated_input_tokens=50,
                now=RUN_AT,
            )
        later = RUN_AT + timedelta(minutes=1)
        transport = Transport(mode="empty")
        await executor(transport, tmp_path, now=later)(await context(sessions, next_job, now=later))
        content = transport.requests[0].body.reveal_for_use()["messages"][1]["content"]
        assert isinstance(content, list)
        assert len(content) == 1
        assert content[0]["type"] == "text"
        document = json.loads(content[0]["text"])
        assert any(source["visual_only"] for source in document["sources"])
        assert not any(source["source_type"] == "media_object" for source in document["sources"])
        async with sessions() as session:
            assert (
                await session.scalar(select(s.memory_watermarks.c.last_contiguous_decided_event_id))
                == event
            )
