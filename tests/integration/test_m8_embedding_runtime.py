"""Real PostgreSQL fences and crypto through the real embedding wire adapter."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from ipaddress import IPv4Address
from typing import Any
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.llm.protocols import ProviderWireRequest, ProviderWireResponse
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.embedding_runtime import (
    EmbeddingRuntimeRepository,
    embedding_job_id,
)
from telegram_userbot.adapters.persistence.model_runtime import model_config_digest
from telegram_userbot.adapters.persistence.worker_runtime import WorkerJobRepository
from telegram_userbot.domain.model_config import CanonicalModelConfig, LogicalRole, ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding, CredentialKeyring
from telegram_userbot.processes.embedding_runtime import EmbeddingExecutor
from telegram_userbot.processes.worker_executors import JobExecutionContext, JobExecutionError
from tests.integration.test_m5_context_media import NOW, seed_scope

pytestmark = pytest.mark.asyncio(loop_scope="session")
BODY = "SYNTHETIC_PRIVATE_CONTEXT_BODY"
KEYRING = CredentialKeyring(
    deployment_id="embedding-integration",
    active_key_version=1,
    keys={1: SensitiveValue(b"e" * 32)},
)


class Resolver:
    def resolve(self, hostname: str, port: int) -> frozenset[IPv4Address]:
        return frozenset({IPv4Address("8.8.8.8")})


class Transport:
    def __init__(self, callback: Callable[[], Awaitable[None]] | None = None) -> None:
        self.callback = callback
        self.requests: list[ProviderWireRequest] = []
        self.status = 200
        self.vector = [3.0, 4.0]
        self.expected_input = BODY

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        self.requests.append(request)
        assert request.path == "/embeddings"
        assert request.body.reveal_for_use()["input"] == [self.expected_input]
        if self.callback:
            await self.callback()
        return ProviderWireResponse(
            self.status,
            SensitiveValue(
                {
                    "data": [{"index": 0, "embedding": self.vector}],
                    "usage": {"prompt_tokens": 5, "total_tokens": 5},
                }
            ),
        )


async def seed(
    session: AsyncSession, *, body_hash: bytes | None = None
) -> tuple[UUID, UUID, UUID, UUID]:
    account, conversation, _, revision = await seed_scope(session, body_hash=body_hash)
    profile, credential, endpoint, capability, config_id, space, record = (
        uuid7() for _ in range(7)
    )
    config = CanonicalModelConfig(
        profile,
        LogicalRole.EMBEDDING,
        endpoint,
        credential,
        ModelProtocol.EMBEDDING,
        "synthetic-embedding",
        None,
        None,
        10,
        True,
        {"dimensions": 2},
    )
    await session.execute(
        insert(s.model_endpoints).values(
            id=endpoint,
            label="synthetic",
            base_url="https://embedding.example.invalid/v1",
            canonical_sha256=hashlib.sha256(b"https://embedding.example.invalid/v1").digest(),
            network_policy_id=uuid7(),
            network_policy_version=1,
            network_category="public",
            created_by_admin_id=42,
        )
    )
    await session.execute(
        insert(s.model_profiles).values(
            id=profile,
            logical_role="embedding",
            profile_kind="embedding",
            state="disabled",
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
    await credential_version(session, profile, credential, 1)
    await session.execute(
        insert(s.model_capability_snapshots).values(
            id=capability,
            endpoint_id=endpoint,
            protocol="embedding",
            model_name=config.model_name,
            supports_text=True,
            supports_temperature=False,
            supports_reasoning_effort=False,
            supports_image=False,
            supports_stream=False,
            supports_structured_output=False,
            max_context_tokens=8192,
            supported_input_roles=["user"],
            embedding_dimensions=[2],
            status="valid",
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    await session.execute(
        insert(s.model_config_versions).values(
            id=config_id,
            profile_id=profile,
            profile_kind="embedding",
            version_no=1,
            endpoint_id=endpoint,
            credential_id=credential,
            capability_snapshot_id=capability,
            protocol="embedding",
            model_name=config.model_name,
            timeout_seconds=10,
            enabled=True,
            protocol_options=dict(config.protocol_options),
            config_sha256=model_config_digest(config),
            created_by_admin_id=42,
            validated_at=NOW,
        )
    )
    await session.execute(
        update(s.model_profiles)
        .where(s.model_profiles.c.id == profile)
        .values(
            state="active",
            active_config_version_no=1,
        )
    )
    await session.execute(
        insert(s.embedding_spaces).values(
            id=space,
            account_id=account,
            model_profile_id=profile,
            config_version_id=config_id,
            model_name_snapshot=config.model_name,
            dimensions=2,
            distance_metric="cosine",
            normalization="l2",
            chunker_version="v1",
            state="building",
            generation=1,
        )
    )
    await session.execute(
        insert(s.embedding_records).values(
            id=record,
            account_id=account,
            embedding_space_id=space,
            message_revision_id=revision,
            chunk_index=0,
            chunker_version="v1",
            source_sha256=hashlib.sha256(BODY.encode()).digest(),
            vector_payload=[],
            dimensions=2,
            state="pending",
        )
    )
    return account, conversation, revision, record


async def credential_version(
    session: AsyncSession, profile: UUID, credential: UUID, version: int
) -> None:
    envelope = KEYRING.encrypt(
        SensitiveValue(f"synthetic-embedding-key-{version}"),
        binding=CredentialBinding(LogicalRole.EMBEDDING, profile, credential, version),
    )
    await session.execute(
        insert(s.model_credential_versions).values(
            id=uuid7(),
            credential_id=credential,
            profile_id=profile,
            version_no=version,
            **asdict(envelope),
        )
    )


async def context(
    sessions: async_sessionmaker[AsyncSession],
    record: UUID,
    *,
    offset: int = 0,
) -> JobExecutionContext:
    async with sessions() as session, session.begin():
        await EmbeddingRuntimeRepository(session).enqueue_pending(now=NOW)
        generation = await session.scalar(
            select(s.background_jobs.c.dispatch_generation).where(
                s.background_jobs.c.id == embedding_job_id(record),
            )
        )
        assert isinstance(generation, int)
        job = await WorkerJobRepository(session).claim_notification(
            job_id=embedding_job_id(record),
            dispatch_generation=generation,
            owner=uuid7(),
            now=NOW + timedelta(seconds=offset),
        )
        assert job is not None
    return JobExecutionContext(job, sessions, asyncio.Event(), asyncio.Semaphore(1))


def executor(transport: Transport, *, offset: int = 0) -> EmbeddingExecutor:
    return EmbeddingExecutor(
        keyring=KEYRING,
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: NOW + timedelta(seconds=offset),
    )


async def row(sessions: async_sessionmaker[AsyncSession], record: UUID) -> Any:
    async with sessions() as session:
        return (
            (
                await session.execute(
                    select(s.embedding_records).where(
                        s.embedding_records.c.id == record,
                    )
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.integration
async def test_embedding_ready_and_replay_without_provider(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
    work = await context(sessions, record)
    transport = Transport()
    await executor(transport)(work)
    await executor(transport)(work)
    result = await row(sessions, record)
    assert result["state"] == "ready"
    assert result["vector_payload"] == pytest.approx([0.6, 0.8])
    assert len(transport.requests) == 1
    async with sessions() as session:
        assert await session.scalar(select(s.embedding_spaces.c.state)) == "building"
        payload = await session.scalar(select(s.background_jobs.c.payload))
        assert payload is not None
        assert BODY not in str(payload)
        assert "synthetic-embedding-key" not in str(payload)
        assert payload["snapshot"]["credential_version_no"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "delete",
        "edit",
        "erasure",
        "contact_erasure",
        "lease",
        "expired_lease",
        "retire",
        "space_change",
        "destroy_key",
    ],
)
@pytest.mark.integration
async def test_late_result_rejected(
    isolated_scope_erasure_engine: AsyncEngine,
    mutation: str,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, _, revision, record = await seed(session)
    work = await context(sessions, record)

    changed = []

    async def mutate() -> None:
        # A separate connection can commit while provider HTTP is in flight.
        async with sessions() as session, session.begin():
            if mutation in {"delete", "edit"}:
                if mutation == "edit":
                    old = (
                        (
                            await session.execute(
                                select(s.message_revisions).where(
                                    s.message_revisions.c.id == revision
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    await session.execute(
                        insert(s.message_revisions).values(
                            **{**dict(old), "id": uuid7(), "revision_no": 2}
                        )
                    )
                values = (
                    {"deleted_at": NOW, "is_tombstone": True}
                    if mutation == "delete"
                    else {"current_revision_no": 2}
                )
                await session.execute(
                    update(s.messages).where(s.messages.c.account_id == account).values(**values)
                )
            elif mutation in {"erasure", "contact_erasure"}:
                await session.execute(
                    insert(s.data_erasure_requests).values(
                        id=uuid7(),
                        account_id=account,
                        scope_type="contact" if mutation == "contact_erasure" else "account",
                        contact_id=(await session.scalar(select(s.conversations.c.contact_id)))
                        if mutation == "contact_erasure"
                        else None,
                        state="requested",
                        requested_by="synthetic",
                        request_idempotency_key=b"e" * 32,
                        policy_version=1,
                    )
                )
            elif mutation == "lease":
                await session.execute(
                    update(s.background_jobs).values(
                        fencing_token=s.background_jobs.c.fencing_token + 1
                    )
                )
            elif mutation == "expired_lease":
                await session.execute(update(s.background_jobs).values(lease_expires_at=NOW))
            elif mutation == "retire":
                await session.execute(update(s.embedding_spaces).values(state="retired"))
            elif mutation == "space_change":
                await session.execute(update(s.embedding_spaces).values(distance_metric="l2"))
            else:
                await session.execute(
                    update(s.model_credential_versions).values(
                        destroyed_at=NOW,
                        ciphertext=None,
                        nonce=None,
                        secret_fingerprint=None,
                        destroy_reason="integration",
                    )
                )
        changed.append(mutation)

    transport = Transport(mutate)
    with pytest.raises(JobExecutionError):
        await asyncio.wait_for(executor(transport)(work), timeout=10)
    assert changed == [mutation]
    result = await row(sessions, record)
    assert result["state"] != "ready"
    assert result["vector_payload"] == []
    assert len(transport.requests) == 1


@pytest.mark.integration
async def test_retry_pins_credential_after_rotation(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
    work = await context(sessions, record)
    transport = Transport()
    transport.status = 503
    with pytest.raises(JobExecutionError) as failed:
        await executor(transport)(work)
    assert failed.value.retryable
    assert work.job.lease_owner is not None
    async with sessions() as session, session.begin():
        await WorkerJobRepository(session).fail_or_retry(
            job=work.job,
            owner=work.job.lease_owner,
            now=NOW,
            error_code=failed.value.code,
            retryable=True,
            retry_delay=timedelta(seconds=1),
        )
        identity = (await session.execute(select(s.model_credentials))).mappings().one()
        await credential_version(session, identity["profile_id"], identity["id"], 2)
        await session.execute(
            update(s.model_credentials).values(active_version_no=2, latest_version_no=2)
        )
    retried = await context(sessions, record, offset=2)
    transport.status = 200
    await executor(transport, offset=2)(retried)
    assert (await row(sessions, record))["state"] == "ready"
    expected_key = "synthetic-embedding-key-1"
    assert all(
        request.headers["authorization"].reveal_for_use() == f"Bearer {expected_key}"
        for request in transport.requests
    )


@pytest.mark.integration
async def test_wrong_dimensions_terminal_and_lost_notification_rebuilt(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
        repo = EmbeddingRuntimeRepository(session)
        assert await repo.enqueue_pending(now=NOW) == 1
        assert await repo.enqueue_pending(now=NOW) == 0
        assert await WorkerJobRepository(session).rebuild_due_notifications(now=NOW) == 1
        assert await session.scalar(select(s.transactional_outbox.c.id)) is not None
    work = await context(sessions, record)
    transport = Transport()
    transport.vector = [1.0]
    with pytest.raises(JobExecutionError) as failure:
        await executor(transport)(work)
    assert not failure.value.retryable
    assert (await row(sessions, record))["state"] == "failed"


@pytest.mark.integration
async def test_worker_role_can_execute_without_ciphertext_select(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
    work = await context(sessions, record)
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE telegram_userbot_worker_runtime"))
        prepared = await EmbeddingRuntimeRepository(session).prepare(work.job, now=NOW)
        assert prepared is not None
        await EmbeddingRuntimeRepository(session).complete(work.job, prepared, (3.0, 4.0), now=NOW)
        assert not await session.scalar(
            text("SELECT has_table_privilege(current_user, 'model_credential_versions', 'SELECT')")
        )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["memory_version", "summary_version"])
async def test_formal_sources_and_nonzero_chunk(
    isolated_scope_erasure_engine: AsyncEngine,
    kind: str,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, conversation, _, record = await seed(session)
        target, version = uuid7(), uuid7()
        content = "x" * 1600
        if kind == "memory_version":
            contact = await session.scalar(select(s.conversations.c.contact_id))
            await session.execute(
                insert(s.memories).values(
                    id=target,
                    account_id=account,
                    conversation_id=conversation,
                    contact_id=contact,
                    memory_type="fact",
                    semantic_key_hash=b"m" * 32,
                    status="active",
                    current_version_no=1,
                )
            )
            await session.execute(
                insert(s.memory_versions).values(
                    id=version,
                    account_id=account,
                    memory_id=target,
                    version_no=1,
                    operation="create",
                    payload_schema_version=1,
                    payload={"text": content},
                    rendered_text=content,
                    importance=0.5,
                    confidence=0.9,
                    time_precision="unknown",
                    validator_policy_version="test-v1",
                    acceptance_kind="migration",
                )
            )
        else:
            await session.execute(
                insert(s.summaries).values(
                    id=target,
                    account_id=account,
                    conversation_id=conversation,
                    summary_kind="rolling",
                    status="active",
                    current_version_no=1,
                )
            )
            await session.execute(
                insert(s.summary_versions).values(
                    id=version,
                    account_id=account,
                    summary_id=target,
                    version_no=1,
                    range_start_event_id=1,
                    range_end_event_id=1,
                    content_text=content,
                    content_sha256=hashlib.sha256(content.encode()).digest(),
                    pipeline_version="test-v1",
                    output_schema_version=2,
                    manifest_sha256=b"s" * 32,
                    invalidation_state="active",
                )
            )
        space = await session.scalar(
            select(s.embedding_records.c.embedding_space_id).where(
                s.embedding_records.c.id == record,
            )
        )
        record = uuid7()
        await session.execute(
            insert(s.embedding_records).values(
                id=record,
                account_id=account,
                embedding_space_id=space,
                **{f"{kind}_id": version},
                chunk_index=1,
                chunker_version="v1",
                source_sha256=hashlib.sha256(b"x" * 200).digest(),
                vector_payload=[],
                dimensions=2,
                state="pending",
            )
        )
    work = await context(sessions, record)
    transport = Transport()
    transport.expected_input = "x" * 200
    await executor(transport)(work)
    assert (await row(sessions, record))["state"] == "ready"


@pytest.mark.integration
async def test_cross_account_job_cannot_read_or_modify_target(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
        other, other_conversation, _, _ = await seed_scope(session)
        await session.execute(
            insert(s.background_jobs).values(
                id=embedding_job_id(record),
                account_id=other,
                queue_name="worker",
                job_type="embedding.compute",
                idempotency_key=hashlib.sha256(record.bytes).digest(),
                payload_schema_version=1,
                payload={"record_id": str(record), "conversation_id": str(other_conversation)},
                available_at=NOW,
            )
        )
    work = await context(sessions, record)
    transport = Transport()
    with pytest.raises(JobExecutionError, match="EMBEDDING_JOB_SCOPE_INVALID"):
        await executor(transport)(work)
    assert not transport.requests
    assert (await row(sessions, record))["state"] == "pending"


@pytest.mark.integration
async def test_final_crash_becomes_failed_without_rearming_budget(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        _, _, _, record = await seed(session)
    await context(sessions, record)
    async with sessions() as session, session.begin():
        await session.execute(update(s.background_jobs).values(attempt_count=5))
        assert (
            await WorkerJobRepository(session).recover_expired(now=NOW + timedelta(seconds=61)) == 1
        )
        assert (
            await EmbeddingRuntimeRepository(session).enqueue_pending(
                now=NOW + timedelta(seconds=61)
            )
            == 0
        )
    assert (await row(sessions, record))["state"] == "failed"


@pytest.mark.integration
async def test_pending_erased_source_does_not_block_compensation(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        account, _, _, record = await seed(session)
        await session.execute(
            insert(s.data_erasure_requests).values(
                id=uuid7(),
                account_id=account,
                scope_type="account",
                state="requested",
                requested_by="synthetic",
                request_idempotency_key=b"e" * 32,
                policy_version=1,
            )
        )
    async with sessions() as session, session.begin():
        assert await EmbeddingRuntimeRepository(session).enqueue_pending(now=NOW) == 0
    assert (await row(sessions, record))["state"] == "invalidated"
