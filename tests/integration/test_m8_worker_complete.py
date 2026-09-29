"""Complete Worker components against isolated PostgreSQL and synthetic providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_userbot.adapters.llm.protocols import ProviderWireRequest, ProviderWireResponse
from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.embedding_rebuild import EmbeddingRebuildRepository
from telegram_userbot.adapters.persistence.model_runtime import model_config_digest
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
)
from telegram_userbot.adapters.persistence.proactive_delivery import expire_proactive_targets
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.proactive_runtime import ProactiveRuntimeRepository
from telegram_userbot.domain.model_config import CanonicalModelConfig, LogicalRole, ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.processes.proactive_pipeline import ProactivePublisher
from tests.integration.test_m5_context_media import NOW
from tests.integration.test_m8_embedding_runtime import KEYRING, Resolver
from tests.integration.test_m8_memory_periods import WorkerSession
from tests.integration.test_m8_memory_runtime import seed
from tests.integration.test_m8_worker_backlog import append_messages

pytestmark = pytest.mark.asyncio(loop_scope="session")
RUN_AT = NOW + timedelta(minutes=40)
SECRET = b"worker-complete-synthetic-secret!"


async def model(session: AsyncSession, role: LogicalRole) -> None:
    endpoint = await session.scalar(select(s.model_endpoints.c.id))
    assert endpoint is not None
    profile, credential, cap_id, config_id = (uuid7() for _ in range(4))
    config = CanonicalModelConfig(
        profile,
        role,
        endpoint,
        credential,
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        f"synthetic-{role.value}",
        None,
        1024,
        10,
        True,
        {},
    )
    await session.execute(
        insert(s.model_profiles).values(
            id=profile, logical_role=role.value, profile_kind="generation", state="disabled"
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
        SensitiveValue("synthetic-worker-credential"),
        binding=CredentialBinding(role, profile, credential, 1),
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
            id=cap_id,
            endpoint_id=endpoint,
            protocol=config.protocol.value,
            model_name=config.model_name,
            supports_text=True,
            supports_temperature=False,
            supports_reasoning_effort=False,
            supports_image=False,
            supports_stream=False,
            supports_structured_output=True,
            max_context_tokens=32768,
            max_output_tokens_limit=4096,
            chat_token_limit_field="max_tokens",  # noqa: S106
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
            capability_snapshot_id=cap_id,
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
    prompt = "Use only the supplied synthetic evidence."
    await session.execute(
        insert(s.prompt_versions).values(
            id=uuid7(),
            logical_role=role.value,
            version_no=1,
            template_body=prompt,
            template_sha256=hashlib.sha256(prompt.encode()).digest(),
            created_by_admin_id=42,
        )
    )


async def setup(
    sessions: async_sessionmaker[AsyncSession], *, mode: str = "AUTO"
) -> tuple[UUID, UUID, UUID]:
    async with sessions() as session, session.begin():
        account, conversation, revision, job = await seed(session)
        await session.execute(
            update(s.memory_jobs).where(s.memory_jobs.c.id == job).values(state="cancelled")
        )
        await session.execute(
            update(s.contacts)
            .where(s.contacts.c.account_id == account)
            .values(proactive_enabled=True, timezone="Asia/Tokyo")
        )
        await session.execute(
            update(s.message_events)
            .where(s.message_events.c.conversation_id == conversation)
            .values(observed_at=NOW, projected_at=NOW)
        )
        await session.execute(
            insert(s.account_orchestrator_states).values(
                account_id=account, default_base_mode=mode, updated_by="synthetic"
            )
        )
        await session.execute(
            insert(s.proactive_policies).values(
                id=uuid7(),
                account_id=account,
                version_no=1,
                enabled=True,
                timezone_name="Asia/Tokyo",
            )
        )
        contact = await session.scalar(
            select(s.conversations.c.contact_id).where(s.conversations.c.id == conversation)
        )
        fact, version = uuid7(), uuid7()
        await session.execute(
            insert(s.memories).values(
                id=fact,
                account_id=account,
                contact_id=contact,
                conversation_id=conversation,
                memory_type="intention",
                semantic_key_hash=hashlib.sha256(fact.bytes).digest(),
                status="active",
                current_version_no=1,
            )
        )
        await session.execute(
            insert(s.memory_versions).values(
                id=version,
                account_id=account,
                memory_id=fact,
                version_no=1,
                operation="create",
                payload_schema_version=1,
                payload={"expected_at": RUN_AT.isoformat(), "owner": "self"},
                rendered_text="A synthetic promised check-in is due.",
                importance=0.8,
                confidence=0.95,
                time_precision="exact",
                validator_policy_version="policy-v1",
                acceptance_kind="manual",
            )
        )
        digest = await session.scalar(
            select(s.message_revisions.c.content_sha256).where(s.message_revisions.c.id == revision)
        )
        await session.execute(
            insert(s.memory_evidence).values(
                memory_version_id=version,
                account_id=account,
                message_revision_id=revision,
                evidence_role="primary",
                trust_class="user_statement",
                source_content_sha256=digest,
            )
        )
        await model(session, LogicalRole.PROACTIVE_AGENT)
        await model(session, LogicalRole.MAIN_AI)
        return account, conversation, revision


class Transport:
    def __init__(
        self,
        *,
        action: str = "send_now",
        callback: Any = None,
        final: str = "A short synthetic check-in.",
    ) -> None:
        self.action, self.callback, self.final = action, callback, final
        self.inputs: list[Any] = []

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        value = request.body.reveal_for_use()["messages"][1]["content"]
        data = json.loads(value[0]["text"] if isinstance(value, list) else value)
        self.inputs.append(data)
        if self.callback:
            await self.callback(data)
        content = self.final
        if data["purpose"] == "proactive_decision":
            content = json.dumps(
                {
                    "schema_version": 1,
                    "action": self.action,
                    "decision_code": "not_natural_now"
                    if self.action == "none"
                    else "timely_support",
                    "selected_occurrence_ids": []
                    if self.action == "none"
                    else [data["occurrences"][0]["id"]],
                    "topic": None if self.action == "none" else "A synthetic check-in",
                    "priority": 0 if self.action == "none" else 0.5,
                    "defer_until": (RUN_AT + timedelta(minutes=2)).isoformat()
                    if self.action == "defer_once"
                    else None,
                }
            )
        return ProviderWireResponse(
            200,
            SensitiveValue(
                {
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
            ),
        )


class TracedPublisher(ProactivePublisher):
    failure: Exception | None = None

    async def execute(self, job: Any, lost: asyncio.Event) -> bool:
        try:
            return await super().execute(job, lost)
        except Exception as error:
            self.failure = error
            raise


def publisher(
    sessions: async_sessionmaker[AsyncSession], transport: Transport, *, now: Any = RUN_AT
) -> TracedPublisher:
    return TracedPublisher(
        sessions=sessions,
        keyring=KEYRING,
        secret=SensitiveValue(SECRET),
        admission=lambda: disk_admission(total_bytes=1000 * 1024**3, available_bytes=900 * 1024**3),
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: now,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "action", "expected"),
    [
        ("AUTO", "send_now", "group"),
        ("COPILOT", "send_now", "draft"),
        ("AUTO", "none", "none"),
        ("AUTO", "defer_once", "defer"),
    ],
)
async def test_proactive_worker_publishes_fenced_targets(
    isolated_scope_erasure_engine: AsyncEngine, mode: str, action: str, expected: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    _, conversation, _ = await setup(sessions, mode=mode)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    transport = Transport(action=action)
    runtime = publisher(worker, transport)
    async with worker() as session, session.begin():
        scope = await ProactiveRuntimeRepository(session).scope(conversation, now=RUN_AT)
        assert (
            await ProactiveRuntimeRepository(session).materialize(scope, secret=SECRET, now=RUN_AT)
            == 1
        )
    result = await runtime.publish(now=RUN_AT)
    if runtime.failure:
        raise runtime.failure
    assert result == 1
    async with sessions() as session:
        decisions = (await session.execute(select(s.proactive_decisions))).mappings().all()
        assert len(decisions) == 1
        assert decisions[0]["action"] == action
        groups = (await session.execute(select(s.outbound_delivery_groups))).mappings().all()
        drafts = (await session.execute(select(s.copilot_drafts))).mappings().all()
        assert len(groups) == int(expected == "group")
        assert len(drafts) == int(expected == "draft")
        runs = (await session.execute(select(s.model_runs))).mappings().all()
        assert all(row["state"] == "succeeded" for row in runs)
        if expected in {"group", "draft"}:
            assert len(runs) == 2
            assert all(
                row["turn_id"] is None and row["proactive_job_id"] is not None for row in runs
            )
            target = groups[0] if groups else drafts[0]
            assert target["proactive_decision_id"] == decisions[0]["id"]
    if expected == "defer":
        later = publisher(worker, transport, now=RUN_AT + timedelta(minutes=3))
        assert await later.publish(now=RUN_AT + timedelta(minutes=3)) == 1
        assert len(transport.inputs) == 2
    else:
        assert await runtime.publish(now=RUN_AT) == 0
    assert len(transport.inputs) == (1 if expected == "none" else 2)
    await assert_delivery_migration(isolated_scope_erasure_engine, published=expected != "none")


async def assert_delivery_migration(engine: AsyncEngine, *, published: bool) -> None:
    def migrate(connection: Any, *, down: bool) -> None:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        if down:
            command.downgrade(config, "0035_memory_period_summaries")
        else:
            command.upgrade(config, "head")

    if not published:
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: migrate(c, down=True))
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: migrate(c, down=False))
    else:
        with pytest.raises(RuntimeError, match="published proactive output prevents downgrade"):
            async with engine.begin() as connection:
                await connection.run_sync(lambda c: migrate(c, down=True))
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0036_worker_complete"
        )


@pytest.mark.integration
async def test_shadow_activation_requires_current_complete_vectors(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    async with sessions() as session, session.begin():
        account, _, _, _ = await seed(session)
        assert await EmbeddingRebuildRepository(session).ensure_spaces(now=RUN_AT) == 0
        assert await EmbeddingRebuildRepository(session).advance(now=RUN_AT) == 0
        await session.execute(
            update(s.embedding_records).values(state="ready", vector_payload=[0.6, 0.8])
        )
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    async with worker() as session, session.begin():
        assert await EmbeddingRebuildRepository(session).advance(now=RUN_AT) == 1
        assert (
            await session.scalar(
                select(s.embedding_spaces.c.state).where(s.embedding_spaces.c.account_id == account)
            )
            == "active"
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation", ["activity", "mode", "control", "contact", "evidence", "fact", "lease"]
)
async def test_proactive_late_results_cannot_publish(
    isolated_scope_erasure_engine: AsyncEngine, mutation: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    account, conversation, _ = await setup(sessions)
    changed = []

    async def modify(data: Any) -> None:
        if data["purpose"] != "proactive_final":
            return
        async with sessions() as session, session.begin():
            if mutation == "activity":
                await session.execute(
                    update(s.message_events)
                    .where(s.message_events.c.conversation_id == conversation)
                    .values(observed_at=RUN_AT)
                )
            elif mutation == "mode":
                await session.execute(
                    update(s.conversations)
                    .where(s.conversations.c.id == conversation)
                    .values(mode_version=2)
                )
            elif mutation == "control":
                await session.execute(
                    update(s.account_orchestrator_states)
                    .where(s.account_orchestrator_states.c.account_id == account)
                    .values(control_version=2)
                )
            elif mutation == "contact":
                await session.execute(
                    update(s.contacts)
                    .where(s.contacts.c.account_id == account)
                    .values(proactive_enabled=False)
                )
            elif mutation == "evidence":
                await session.execute(
                    update(s.messages)
                    .where(s.messages.c.conversation_id == conversation)
                    .values(deleted_at=RUN_AT, is_tombstone=True)
                )
            elif mutation == "fact":
                await session.execute(
                    update(s.memories)
                    .where(s.memories.c.conversation_id == conversation)
                    .values(status="invalidated")
                )
            else:
                await session.execute(update(s.proactive_jobs).values(lease_owner=uuid7()))
        changed.append(mutation)

    transport = Transport(callback=modify)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    runtime = publisher(worker, transport)
    await runtime.publish(now=RUN_AT)
    if not changed and runtime.failure:
        raise runtime.failure
    assert changed == [mutation]
    assert runtime.failure is not None
    assert getattr(runtime.failure, "code", "").startswith("PROACTIVE_")
    async with sessions() as session:
        assert await session.scalar(select(s.outbound_delivery_groups.c.id)) is None
        assert await session.scalar(select(s.copilot_drafts.c.id)) is None


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["AUTO", "COPILOT"])
async def test_expired_proactive_targets_release_budget(
    isolated_scope_erasure_engine: AsyncEngine, mode: str
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    await setup(sessions, mode=mode)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    runtime = publisher(worker, Transport())
    await runtime.publish(now=RUN_AT)
    if runtime.failure:
        raise runtime.failure
    async with worker() as session, session.begin():
        assert await expire_proactive_targets(session, now=RUN_AT + timedelta(minutes=11)) == 1
        assert (
            await ProactiveRepository(session).reap_budget(now=RUN_AT + timedelta(minutes=11)) == 1
        )
        assert await session.scalar(select(s.proactive_budget_reservations.c.state)) == "expired"
        assert all(
            value == 0
            for value in (
                await session.execute(select(s.proactive_budget_buckets.c.held_count))
            ).scalars()
        )


@pytest.mark.integration
@pytest.mark.parametrize("changed", [False, True])
async def test_app_preflight_rechecks_proactive_and_claims_lease(
    isolated_scope_erasure_engine: AsyncEngine, changed: bool
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    _, conversation, _ = await setup(sessions)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    runtime = publisher(worker, Transport())
    await runtime.publish(now=RUN_AT)
    if runtime.failure:
        raise runtime.failure
    async with sessions() as session, session.begin():
        if changed:
            await session.execute(
                update(s.conversations)
                .where(s.conversations.c.id == conversation)
                .values(mode_version=2)
            )
        intent = await session.scalar(select(s.outbound_intents.c.id))
        assert intent is not None
        await session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
        prepared = await ConversationOrchestratorRepository(session).preflight_intent(
            intent_id=intent, owner=uuid7(), now=RUN_AT
        )
        assert (prepared is None) == changed
        assert await session.scalar(select(s.proactive_budget_reservations.c.state)) == (
            "held" if changed else "send_unknown"
        )


@pytest.mark.integration
async def test_proactive_final_retry_reuses_decision_and_budget(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    await setup(sessions)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    attempts = []

    async def interrupt(data: Any) -> None:
        if data["purpose"] == "proactive_final":
            attempts.append(data)
            if len(attempts) == 1:
                raise TimeoutError

    transport = Transport(callback=interrupt)
    first = publisher(worker, transport)
    await first.publish(now=RUN_AT)
    assert isinstance(first.failure, TimeoutError)
    async with sessions() as session:
        hold = (await session.execute(select(s.proactive_budget_reservations))).mappings().one()
    restarted = publisher(worker, transport, now=RUN_AT + timedelta(minutes=2))
    assert await restarted.publish(now=RUN_AT + timedelta(minutes=2)) == 1
    if restarted.failure:
        raise restarted.failure
    async with sessions() as session:
        assert len((await session.execute(select(s.proactive_decisions))).all()) == 1
        current = (await session.execute(select(s.proactive_budget_reservations))).mappings().one()
        assert current["id"] == hold["id"]
        assert current["expires_at"] == hold["expires_at"]
        assert len((await session.execute(select(s.outbound_delivery_groups))).all()) == 1
    assert attempts[0] == attempts[1]


@pytest.mark.integration
async def test_shadow_rollback_backfills_new_sources_before_reactivation(
    isolated_scope_erasure_engine: AsyncEngine,
) -> None:

    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    async with sessions() as session, session.begin():
        account, conversation, _, _ = await seed(session)
        await EmbeddingRebuildRepository(session).advance(now=RUN_AT)
        await session.execute(
            update(s.embedding_records).values(state="ready", vector_payload=[0.6, 0.8])
        )
        await EmbeddingRebuildRepository(session).advance(now=RUN_AT)
        space = await session.scalar(select(s.embedding_spaces.c.id))
        assert space is not None
        await session.execute(update(s.embedding_spaces).values(state="retired", retired_at=RUN_AT))
        await append_messages(session, conversation, 1)
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    async with worker() as session, session.begin():
        repository = EmbeddingRebuildRepository(session)
        assert await repository.request_rollback(account_id=account, space_id=space)
        assert await repository.advance(now=RUN_AT) == 1
        assert await session.scalar(select(s.embedding_spaces.c.state)) == "building"
    async with sessions() as session, session.begin():
        await session.execute(
            update(s.embedding_records).values(state="ready", vector_payload=[0.6, 0.8])
        )
    async with worker() as session, session.begin():
        assert await EmbeddingRebuildRepository(session).advance(now=RUN_AT) == 1
        assert await session.scalar(select(s.embedding_spaces.c.state)) == "active"


@pytest.mark.integration
@pytest.mark.parametrize("expire", [False, True])
async def test_proactive_copilot_approval_and_expiration(
    isolated_scope_erasure_engine: AsyncEngine, expire: bool
) -> None:
    sessions = async_sessionmaker(isolated_scope_erasure_engine)
    await setup(sessions, mode="COPILOT")
    worker = async_sessionmaker(isolated_scope_erasure_engine, sync_session_class=WorkerSession)
    runtime = publisher(worker, Transport())
    await runtime.publish(now=RUN_AT)
    if runtime.failure:
        raise runtime.failure
    owner = uuid7()
    async with sessions() as session, session.begin():
        draft = await session.scalar(select(s.copilot_drafts.c.id))
        assert draft is not None
        await session.execute(text("SET LOCAL ROLE telegram_userbot_app_runtime"))
        repo = ConversationOrchestratorRepository(session)
        await repo.issue_draft_token(
            draft_id=draft,
            raw_token="synthetic-approval",  # noqa: S106 - isolated callback token
            admin_telegram_user_id=42,
            bot_chat_id=42,
            purpose="send",
            now=RUN_AT,
        )
        group = await repo.approve_copilot_draft(
            raw_token="synthetic-approval",  # noqa: S106 - isolated callback token
            admin_telegram_user_id=42,
            bot_chat_id=42,
            owner=owner,
            entropy=SECRET,
            now=RUN_AT,
        )
        assert group is not None
        if not expire:
            intent = await session.scalar(select(s.outbound_intents.c.id))
            assert intent is not None
            assert (
                await repo.preflight_intent(intent_id=intent, owner=owner, now=RUN_AT) is not None
            )
            assert (
                await session.scalar(select(s.proactive_budget_reservations.c.state))
                == "send_unknown"
            )
    if expire:
        async with worker() as session, session.begin():
            assert await expire_proactive_targets(session, now=RUN_AT + timedelta(minutes=11)) == 1
            assert (
                await ProactiveRepository(session).reap_budget(now=RUN_AT + timedelta(minutes=11))
                == 1
            )
            assert (
                await session.scalar(select(s.proactive_budget_reservations.c.state)) == "expired"
            )
