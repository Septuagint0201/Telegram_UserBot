"""Real SQL, immutable provenance, provider adapter and late-result fences."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.llm.protocols import ProviderWireRequest, ProviderWireResponse
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_pipeline import (
    MemoryPipelineRepository,
    memory_background_id,
)
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.memory_results import MemoryResultRepository
from telegram_userbot.adapters.persistence.model_runtime import model_config_digest
from telegram_userbot.adapters.persistence.worker_runtime import WorkerJobRepository
from telegram_userbot.domain.memory import EventRange
from telegram_userbot.domain.messaging.events import BodyKind, MessageBody
from telegram_userbot.domain.model_config import CanonicalModelConfig, LogicalRole, ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding
from telegram_userbot.processes.memory_pipeline import MemoryPipelineExecutor
from telegram_userbot.processes.model_output_schema import (
    DEFAULT_MODEL_OUTPUT_SCHEMAS,
    ModelOutputParseContext,
)
from telegram_userbot.processes.worker_executors import JobExecutionContext, JobExecutionError
from tests.integration.test_m5_context_media import NOW
from tests.integration.test_m8_embedding_runtime import (
    BODY,
    KEYRING,
    Resolver,
)
from tests.integration.test_m8_embedding_runtime import (
    seed as seed_embedding,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
SECRET = b"m" * 32
RUN_AT = NOW + timedelta(minutes=1)


async def seed(
    session: AsyncSession, *, kind: str = "episode", supports_images: bool = False
) -> tuple[UUID, UUID, UUID, UUID]:
    account, conversation, revision, _ = await seed_embedding(
        session,
        body_hash=MessageBody(BodyKind.TEXT, BODY).content_sha256,
    )
    await session.execute(
        update(s.message_events)
        .where(s.message_events.c.conversation_id == conversation)
        .values(projected_at=NOW)
    )
    endpoint = await session.scalar(select(s.model_endpoints.c.id))
    assert endpoint is not None
    profile, credential, capability, config_id = (uuid7() for _ in range(4))
    config = CanonicalModelConfig(
        profile,
        LogicalRole.MEMORY_AGENT,
        endpoint,
        credential,
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        "synthetic-memory",
        None,
        1024,
        10,
        True,
        {},
    )
    await session.execute(
        insert(s.model_profiles).values(
            id=profile, logical_role="memory_agent", profile_kind="generation", state="disabled"
        )
    )
    await session.execute(
        insert(s.model_credentials).values(
            id=credential,
            profile_id=profile,
            status="active",
            active_version_no=1,
            latest_version_no=1,
        )
    )
    envelope = KEYRING.encrypt(
        SensitiveValue("synthetic-memory-key-1"),
        binding=CredentialBinding(LogicalRole.MEMORY_AGENT, profile, credential, 1),
    )
    await session.execute(
        insert(s.model_credential_versions).values(
            id=uuid7(),
            credential_id=credential,
            profile_id=profile,
            version_no=1,
            **asdict(envelope),
        )
    )
    await session.execute(
        insert(s.model_capability_snapshots).values(
            id=capability,
            endpoint_id=endpoint,
            protocol=config.protocol.value,
            model_name=config.model_name,
            supports_text=True,
            supports_temperature=False,
            supports_reasoning_effort=False,
            supports_image=supports_images,
            supports_stream=False,
            supports_structured_output=True,
            max_context_tokens=32768,
            max_output_tokens_limit=4096,
            chat_token_limit_field="max_tokens",  # noqa: S106 - provider field name
            supported_input_roles=["system", "user"],
            status="valid",
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    await session.execute(
        insert(s.model_config_versions).values(
            id=config_id,
            profile_id=profile,
            profile_kind="generation",
            version_no=1,
            endpoint_id=endpoint,
            credential_id=credential,
            capability_snapshot_id=capability,
            protocol=config.protocol.value,
            model_name=config.model_name,
            max_output_tokens=1024,
            timeout_seconds=10,
            enabled=True,
            protocol_options={},
            config_sha256=model_config_digest(config),
            created_by_admin_id=42,
            validated_at=NOW,
        )
    )
    await session.execute(
        update(s.model_profiles)
        .where(s.model_profiles.c.id == profile)
        .values(state="active", active_config_version_no=1)
    )
    prompt = "Extract justified memories with canonical evidence."
    await session.execute(
        insert(s.prompt_versions).values(
            id=uuid7(),
            logical_role="memory_agent",
            version_no=1,
            template_body=prompt,
            template_sha256=hashlib.sha256(prompt.encode()).digest(),
            created_by_admin_id=42,
        )
    )
    event = await session.scalar(
        select(s.message_revisions.c.source_event_id).where(s.message_revisions.c.id == revision)
    )
    assert event is not None
    job_id = await MemoryRepository(session).refresh_pending_job(
        account_id=account,
        conversation_id=conversation,
        job_kind=kind,
        event_range=EventRange(event, event),
        estimated_input_tokens=50,
        now=NOW,
    )
    return account, conversation, revision, job_id


async def context(
    sessions: async_sessionmaker[AsyncSession], identity: UUID, *, now: Any = RUN_AT
) -> JobExecutionContext:
    async with sessions() as session, session.begin():
        await MemoryPipelineRepository(session).enqueue_pending(now=now)
        generation = await session.scalar(
            select(s.background_jobs.c.dispatch_generation).where(
                s.background_jobs.c.id == memory_background_id(identity)
            )
        )
        assert isinstance(generation, int)
        job = await WorkerJobRepository(session).claim_notification(
            job_id=memory_background_id(identity),
            dispatch_generation=generation,
            owner=uuid7(),
            now=now,
        )
        assert job is not None
    return JobExecutionContext(job, sessions, asyncio.Event(), asyncio.Semaphore(1))


class Transport:
    def __init__(
        self, *, callback: Callable[[], Awaitable[None]] | None = None, mode: str = "accepted"
    ) -> None:
        self.callback = callback
        self.mode = mode
        self.requests: list[ProviderWireRequest] = []
        self.status = 200

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        self.requests.append(request)
        body = request.body.reveal_for_use()
        content = body["messages"][1]["content"]
        if isinstance(content, list):
            content = content[0]["text"]
        data = json.loads(content)
        source = next(item for item in data["sources"] if item["source_type"] == "message_revision")
        output: dict[str, Any] = {"schema_version": data["output_schema_version"], "proposals": []}
        if data["output_schema_version"] == 2:
            output = {
                "schema_version": 2,
                "summary_text": "A synthetic summary.",
                "no_change_reason": None,
            }
            if self.mode == "empty":
                output.update(summary_text=None, no_change_reason="No new facts")
        elif self.mode != "empty":
            evidence = {
                name: source[name]
                for name in ("source_id", "source_revision", "source_content_sha256", "trust")
            }
            if self.mode == "outside":
                evidence["source_id"] = str(uuid7())
            output["proposals"] = [
                {
                    "operation": "create",
                    "memory_type": "preference",
                    "semantic_key": "synthetic:preference",
                    "payload": {"value": "synthetic"},
                    "rendered_text": "A synthetic memory.",
                    "confidence": 0.7 if self.mode == "candidate" else 0.95,
                    "importance": 0.5,
                    "evidence": [evidence],
                    "targets": [],
                }
            ]
        if self.mode == "update":
            output["proposals"][0].update(
                operation="update",
                targets=[data["memory_targets"][0]["memory_id"]],
                rendered_text="Updated synthetic memory.",
            )
        if self.callback:
            await self.callback()
        return ProviderWireResponse(
            self.status,
            SensitiveValue(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "not-json"
                                if self.mode == "invalid"
                                else json.dumps(output)
                            },
                            "finish_reason": "length" if self.mode == "incomplete" else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 25, "total_tokens": 65},
                }
            ),
        )


def executor(transport: Transport, *, now: Any = RUN_AT) -> MemoryPipelineExecutor:
    return MemoryPipelineExecutor(
        keyring=KEYRING,
        fingerprint_secret=SensitiveValue(SECRET),
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: now,
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["episode", "rolling_summary", "consolidation", "reconciliation"])
async def test_pipeline_success_and_replay(
    isolated_scope_erasure_engine: AsyncEngine, kind: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id = await seed(session, kind=kind)
    work = await context(sessions, job_id)
    transport = Transport()
    # Exercise the repository directly first so unexpected SQL errors retain diagnostics.
    async with sessions() as session, session.begin():
        prepared = await MemoryPipelineRepository(session).prepare(
            work.job, secret=SECRET, now=RUN_AT
        )
        assert prepared is not None
    result = await executor(transport)._generate(prepared)
    raw = result.text.reveal_for_use()
    output = DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, prepared.manifest.output_schema_version, purpose=f"memory_{kind}"
    ).parse(
        raw,
        context=ModelOutputParseContext(
            prepared.manifest.account_id, prepared.manifest.conversation_id, RUN_AT
        ),
    )
    async with sessions() as session, session.begin():
        await MemoryResultRepository(session).complete(
            work.job,
            prepared,
            output,
            raw=raw,
            input_tokens=40,
            output_tokens=25,
            secret=SECRET,
            now=RUN_AT,
        )
    await executor(transport)(work)
    async with sessions() as session:
        assert await session.scalar(select(s.memory_jobs.c.state)) == "succeeded"
        assert await session.scalar(select(s.model_runs.c.state)) == "succeeded"
        assert await session.scalar(select(s.model_run_attempts.c.state)) == "succeeded"
        if kind == "rolling_summary":
            assert (
                await session.scalar(select(s.summary_versions.c.content_text))
                == "A synthetic summary."
            )
            assert (
                await session.scalar(select(s.summary_watermarks.c.last_included_event_id))
                == prepared.manifest.range_end_event_id
            )
        else:
            assert (
                await session.scalar(select(s.memory_versions.c.rendered_text))
                == "A synthetic memory."
            )
            assert await session.scalar(select(s.memory_proposals.c.state)) == "accepted"
            assert await session.scalar(select(s.memory_evidence.c.message_revision_id)) is not None
        records = (await session.execute(select(s.embedding_records))).mappings().all()
        assert len(records) == 2
        assert all(row["state"] == "pending" and row["vector_payload"] == [] for row in records)
        assert BODY not in str(
            (await session.execute(select(s.memory_input_manifests))).mappings().all()
        )
        assert BODY not in str((await session.execute(select(s.background_jobs.c.payload))).all())
    assert len(transport.requests) == 1


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["candidate", "empty", "invalid", "incomplete", "outside"])
async def test_validation_never_promotes_invalid_output(
    isolated_scope_erasure_engine: AsyncEngine, mode: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id = await seed(session)
    work = await context(sessions, job_id)
    transport = Transport(mode=mode)
    if mode in {"candidate", "empty"}:
        await executor(transport)(work)
    else:
        with pytest.raises(JobExecutionError):
            await executor(transport)(work)
    async with sessions() as session:
        assert await session.scalar(select(s.memory_versions.c.id)) is None
        state = await session.scalar(select(s.memory_jobs.c.state))
        assert state == ("succeeded" if mode in {"candidate", "empty"} else "dead_letter")
        assert (
            await session.scalar(select(s.memory_watermarks.c.last_contiguous_decided_event_id))
            is not None
        ) == (mode in {"candidate", "empty"})


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    ["delete", "revision", "source_status", "lease", "memory_lease", "erasure", "credential"],
)
async def test_late_results_cannot_commit(
    isolated_scope_erasure_engine: AsyncEngine, mutation: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, _, revision, job_id = await seed(session)
    work = await context(sessions, job_id)

    changed = []

    async def change() -> None:
        async with sessions() as session, session.begin():
            if mutation == "delete":
                await session.execute(
                    update(s.messages).values(deleted_at=RUN_AT, is_tombstone=True)
                )
            elif mutation == "revision":
                old = (
                    (
                        await session.execute(
                            select(s.message_revisions).where(s.message_revisions.c.id == revision)
                        )
                    )
                    .mappings()
                    .one()
                )
                await session.execute(
                    insert(s.message_revisions).values(
                        **{
                            **dict(old),
                            "id": uuid7(),
                            "revision_no": 2,
                            "text_content": "edited",
                            "content_sha256": MessageBody(BodyKind.TEXT, "edited").content_sha256,
                        },
                    )
                )
                await session.execute(update(s.messages).values(current_revision_no=2))
            elif mutation == "source_status":
                await session.execute(update(s.messages).values(source_status="pending"))
            elif mutation == "lease":
                await session.execute(
                    update(s.background_jobs).values(
                        fencing_token=s.background_jobs.c.fencing_token + 1
                    )
                )
            elif mutation == "memory_lease":
                await session.execute(
                    update(s.memory_jobs).values(job_version=s.memory_jobs.c.job_version + 1)
                )
            elif mutation == "credential":
                await session.execute(
                    update(s.model_credential_versions)
                    .where(
                        s.model_credential_versions.c.profile_id.in_(
                            select(s.model_profiles.c.id).where(
                                s.model_profiles.c.logical_role == "memory_agent"
                            )
                        )
                    )
                    .values(
                        destroyed_at=RUN_AT,
                        destroy_reason="integration",
                        secret_fingerprint=None,
                        ciphertext=None,
                        nonce=None,
                    )
                )
            else:
                await session.execute(
                    insert(s.data_erasure_requests).values(
                        id=uuid7(),
                        account_id=account,
                        scope_type="account",
                        state="requested",
                        requested_by="synthetic",
                        policy_version=1,
                        request_idempotency_key=hashlib.sha256(revision.bytes).digest(),
                    )
                )
        changed.append(mutation)

    with pytest.raises(JobExecutionError) as failure:
        await executor(Transport(callback=change))(work)
    assert changed == [mutation]
    assert (
        failure.value.code
        == {
            "delete": "MEMORY_SOURCE_CHANGED",
            "revision": "MEMORY_SOURCE_CHANGED",
            "source_status": "MEMORY_SOURCE_CHANGED",
            "lease": "WORKER_JOB_FENCE_LOST",
            "memory_lease": "MEMORY_JOB_FENCE_LOST",
            "erasure": "MEMORY_SCOPE_ERASED",
            "credential": "MEMORY_CREDENTIAL_UNAVAILABLE",
        }[mutation]
    )
    async with sessions() as session:
        assert await session.scalar(select(s.memory_versions.c.id)) is None
        assert await session.scalar(select(s.memory_watermarks.c.conversation_id)) is None
        assert await session.scalar(select(s.model_runs.c.state)) != "succeeded"


@pytest.mark.integration
async def test_worker_role_prepare_and_complete(isolated_scope_erasure_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id = await seed(session)
    work = await context(sessions, job_id)
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        prepared = await MemoryPipelineRepository(session).prepare(
            work.job, secret=SECRET, now=RUN_AT
        )
        assert prepared is not None
        assert not await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, "
                "'model_credential_versions', 'ciphertext', 'SELECT')"
            )
        )
    generated = await executor(Transport())._generate(prepared)
    raw = generated.text.reveal_for_use()
    output = DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 1, purpose="memory_episode"
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


@pytest.mark.integration
async def test_retry_uses_sealed_credential_and_expired_capability(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id = await seed(session)
    work = await context(sessions, job_id)
    transport = Transport(mode="empty")
    transport.status = 503
    with pytest.raises(JobExecutionError) as failure:
        await executor(transport)(work)
    assert failure.value.retryable
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
        credential = (
            (
                await session.execute(
                    select(s.model_credentials)
                    .join(
                        s.model_profiles, s.model_profiles.c.id == s.model_credentials.c.profile_id
                    )
                    .where(s.model_profiles.c.logical_role == "memory_agent")
                )
            )
            .mappings()
            .one()
        )
        envelope = KEYRING.encrypt(
            SensitiveValue("synthetic-memory-key-2"),
            binding=CredentialBinding(
                LogicalRole.MEMORY_AGENT, credential["profile_id"], credential["id"], 2
            ),
        )
        await session.execute(
            insert(s.model_credential_versions).values(
                id=uuid7(),
                credential_id=credential["id"],
                profile_id=credential["profile_id"],
                version_no=2,
                **asdict(envelope),
            )
        )
        await session.execute(
            update(s.model_credentials)
            .where(s.model_credentials.c.id == credential["id"])
            .values(active_version_no=2, latest_version_no=2)
        )
        sealed = (await session.execute(select(s.memory_input_manifests))).mappings().one()
    later = RUN_AT + timedelta(hours=2)
    retry = await context(sessions, job_id, now=later)
    transport.status = 200
    await executor(transport, now=later)(retry)
    expected_key = "synthetic-memory-key-1"
    assert [
        request.headers["authorization"].reveal_for_use() for request in transport.requests
    ] == [f"Bearer {expected_key}"] * 2
    assert (
        transport.requests[0].body.reveal_for_use() == transport.requests[1].body.reveal_for_use()
    )
    async with sessions() as session:
        assert (await session.execute(select(s.memory_input_manifests))).mappings().one() == sealed
        assert (
            await session.execute(
                select(s.model_run_attempts.c.state).order_by(s.model_run_attempts.c.attempt_no)
            )
        ).scalars().all() == ["retryable_failed", "succeeded"]


@pytest.mark.integration
async def test_quiet_window_republication_and_serial_conversation_jobs(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, conversation, _, job_id = await seed(session)
        second = await MemoryRepository(session).refresh_pending_job(
            account_id=account,
            conversation_id=conversation,
            job_kind="consolidation",
            event_range=EventRange(1, 1),
            estimated_input_tokens=1,
            # Equal timestamps use UUID ordering; insertion order is not the
            # queue contract. Give this explicitly later job a later timestamp.
            now=NOW + timedelta(seconds=1),
        )
    work = await context(sessions, job_id)
    async with sessions() as session, session.begin():
        assert (
            await session.scalar(
                select(s.background_jobs.c.id).where(
                    s.background_jobs.c.id == memory_background_id(second)
                )
            )
            is None
        )
        await session.execute(
            update(s.memory_jobs)
            .where(s.memory_jobs.c.id == job_id)
            .values(quiet_until=RUN_AT + timedelta(seconds=40))
        )
    transport = Transport(mode="empty")
    await executor(transport)(work)
    assert not transport.requests
    async with sessions() as session, session.begin():
        assert work.job.lease_owner is not None
        assert await WorkerJobRepository(session).succeed(
            job=work.job, owner=work.job.lease_owner, now=RUN_AT
        )
    later = RUN_AT + timedelta(seconds=45)
    retry = await context(sessions, job_id, now=later)
    assert retry.job.attempt_count == 1
    await executor(transport, now=later)(retry)
    async with sessions() as session, session.begin():
        assert await MemoryPipelineRepository(session).enqueue_pending(now=later) == 1
        assert (
            await session.scalar(
                select(s.background_jobs.c.id).where(
                    s.background_jobs.c.id == memory_background_id(second)
                )
            )
            is not None
        )


@pytest.mark.integration
async def test_rolling_no_change_advances_without_inventing_summary(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, job_id = await seed(session, kind="rolling_summary")
    work = await context(sessions, job_id)
    await executor(Transport(mode="empty"))(work)
    async with sessions() as session:
        assert await session.scalar(select(s.summary_versions.c.id)) is None
        assert await session.scalar(select(s.summary_watermarks.c.last_included_event_id)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["projection", "pending", "image"])
async def test_incomplete_input_never_advances_watermark(
    isolated_scope_erasure_engine: AsyncEngine, mode: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, _, revision, job_id = await seed(session)
        if mode == "projection":
            await session.execute(update(s.message_events).values(projected_at=None))
        elif mode == "pending":
            await session.execute(update(s.messages).values(source_status="pending"))
        else:
            await session.execute(
                insert(s.message_media).values(
                    id=uuid7(),
                    account_id=account,
                    message_revision_id=revision,
                    media_kind="photo",
                    position=0,
                    metadata_schema_version=1,
                )
            )
    work = await context(sessions, job_id)
    transport = Transport()
    with pytest.raises(JobExecutionError) as failure:
        await executor(transport)(work)
    assert (
        failure.value.code
        == {
            "projection": "MEMORY_PROJECTION_PENDING",
            "pending": "MEMORY_SOURCE_PENDING",
            "image": "MEMORY_VISION_UNAVAILABLE",
        }[mode]
    )
    assert not transport.requests
    async with sessions() as session:
        assert await session.scalar(select(s.memory_watermarks.c.conversation_id)) is None


@pytest.mark.integration
async def test_fifty_revisions_schedule_summary_with_complete_coverage(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, conversation, _, _job_id = await seed(session)
        first_message = (await session.execute(select(s.messages))).mappings().one()
        first_revision = (await session.execute(select(s.message_revisions))).mappings().one()
        end: int | None = 1
        for index in range(11, 60):
            end = await session.scalar(
                insert(s.message_events)
                .values(
                    event_uuid=uuid7(),
                    account_id=account,
                    conversation_id=conversation,
                    event_kind="incoming.create",
                    telegram_message_id=index,
                    fingerprint_version=1,
                    update_fingerprint=hashlib.sha256(str(index).encode()).digest(),
                    ordering_key=f"v1:{index:08d}",
                    metadata_schema_version=1,
                    projected_at=NOW,
                )
                .returning(s.message_events.c.id)
            )
            message = uuid7()
            await session.execute(
                insert(s.messages).values(
                    **{**dict(first_message), "id": message, "telegram_message_id": index}
                )
            )
            await session.execute(
                insert(s.message_revisions).values(
                    **{
                        **dict(first_revision),
                        "id": uuid7(),
                        "message_id": message,
                        "source_event_id": end,
                    }
                )
            )
        assert isinstance(end, int)
        await MemoryRepository(session).refresh_pending_job(
            account_id=account,
            conversation_id=conversation,
            job_kind="episode",
            event_range=EventRange(1, end),
            estimated_input_tokens=1000,
            now=NOW,
        )
    # Bounded generations must finish every episode and summary segment.
    for index in range(12):
        async with sessions() as session:
            pending = (
                (
                    await session.execute(
                        select(s.memory_jobs)
                        .where(s.memory_jobs.c.state == "pending")
                        .order_by(s.memory_jobs.c.created_at, s.memory_jobs.c.id)
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if pending is None:
            break
        clock = RUN_AT + timedelta(minutes=index)
        work = await context(sessions, pending["id"], now=clock)
        await executor(
            Transport(mode="empty" if pending["job_kind"] == "episode" else "accepted"), now=clock
        )(work)
    async with sessions() as session:
        assert await session.scalar(select(s.summary_watermarks.c.last_included_event_id)) == end
        assert (
            len(
                (
                    await session.execute(
                        select(s.summary_version_sources.c.message_revision_id).where(
                            s.summary_version_sources.c.message_revision_id.is_not(None)
                        )
                    )
                ).all()
            )
            == 50
        )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["consolidation", "rolling_summary"])
async def test_next_generation_uses_pinned_formal_context(
    isolated_scope_erasure_engine: AsyncEngine, kind: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, conversation, _, first = await seed(
            session, kind="episode" if kind == "consolidation" else kind
        )
    await executor(Transport())(await context(sessions, first))
    async with sessions() as session, session.begin():
        second = await MemoryRepository(session).refresh_pending_job(
            account_id=account,
            conversation_id=conversation,
            job_kind=kind,
            event_range=EventRange(1, 1),
            estimated_input_tokens=20,
            now=RUN_AT,
        )
    later = RUN_AT + timedelta(minutes=1)
    work = await context(sessions, second, now=later)
    transport = Transport(mode="update" if kind == "consolidation" else "accepted")
    await executor(transport, now=later)(work)
    async with sessions() as session:
        if kind == "consolidation":
            assert await session.scalar(select(s.memories.c.current_version_no)) == 2
            assert (
                await session.execute(
                    select(s.memory_versions.c.rendered_text).order_by(
                        s.memory_versions.c.version_no
                    )
                )
            ).scalars().all() == ["A synthetic memory.", "Updated synthetic memory."]
        else:
            assert await session.scalar(select(s.summaries.c.current_version_no)) == 2
            parents = (
                (
                    await session.execute(
                        select(s.summary_version_sources.c.prior_summary_version_id).where(
                            s.summary_version_sources.c.prior_summary_version_id.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(parents) == 1
