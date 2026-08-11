from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.model_repository import ModelConfigurationRepository
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
    OrchestratorConflictError,
)
from telegram_userbot.adapters.persistence.records import AttemptCompletionRecord
from telegram_userbot.adapters.persistence.schema import (
    account_peers,
    accounts,
    background_jobs,
    contacts,
    conversation_turns,
    conversations,
    copilot_action_tokens,
    copilot_draft_revisions,
    copilot_drafts,
    model_runs,
    outbound_delivery_groups,
    outbound_intents,
    telegram_peers,
    turn_grace_authorizations,
)
from telegram_userbot.adapters.persistence.telegram_repository import (
    TelegramLifecycleRepository,
)
from telegram_userbot.adapters.telegram_user import (
    PeerAdmission,
    RawTelegramUpdate,
    normalize_update,
)
from telegram_userbot.domain.conversation import BaseMode
from telegram_userbot.domain.messaging import (
    Direction,
    EventKind,
    NormalizedTelegramEvent,
    PeerKind,
)
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialKeyring

NOW = datetime(2030, 4, 5, 6, 7, 8, tzinfo=UTC)
OWNER = UUID(int=900)
ADMIN_ID = 42
BOT_CHAT_ID = 84
EDIT_TOKEN = "SYNTHETIC_EDIT_ACTION_TOKEN"  # noqa: S105 - fake callback token
SEND_TOKEN = "SYNTHETIC_SEND_ACTION_TOKEN"  # noqa: S105 - fake callback token
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_conversation(session: AsyncSession) -> tuple[UUID, UUID, int]:
    account_id = uuid7()
    peer_id = uuid7()
    account_peer_id = uuid7()
    contact_id = uuid7()
    conversation_id = uuid7()
    telegram_chat_id = peer_id.int % 2**63
    await session.execute(
        insert(accounts).values(
            id=account_id,
            telegram_user_id=account_id.int % 2**63,
            display_label="m4-synthetic-owner",
            status="active",
        )
    )
    await session.execute(
        insert(telegram_peers).values(
            id=peer_id,
            peer_type="user",
            telegram_peer_id=telegram_chat_id,
            is_bot=False,
        )
    )
    await session.execute(
        insert(account_peers).values(
            id=account_peer_id,
            account_id=account_id,
            peer_id=peer_id,
            observed_is_contact=True,
            metadata_schema_version=1,
        )
    )
    await session.execute(
        insert(contacts).values(
            id=contact_id,
            account_id=account_id,
            account_peer_id=account_peer_id,
            automation_status="allowed",
        )
    )
    await session.execute(
        insert(conversations).values(
            id=conversation_id,
            account_id=account_id,
            contact_id=contact_id,
            account_peer_id=account_peer_id,
            telegram_chat_id=telegram_chat_id,
        )
    )
    return account_id, conversation_id, telegram_chat_id


def capabilities() -> ModelCapabilities:
    return ModelCapabilities(
        ProfileKind.GENERATION,
        frozenset({ModelProtocol.OPENAI_RESPONSES}),
        True,
        True,
        True,
        True,
        True,
        32_000,
        4096,
        frozenset({"system", "user", "assistant"}),
    )


async def activate_main_ai(session: AsyncSession) -> None:
    repository = ModelConfigurationRepository(session)
    profile_ids = {role: uuid7() for role in LogicalRole}
    credential_ids = {role: uuid7() for role in LogicalRole}
    await repository.bootstrap_profiles(
        profile_ids=profile_ids,
        credential_ids=credential_ids,
    )
    profile_id = profile_ids[LogicalRole.MAIN_AI]
    endpoint_id = uuid7()
    await repository.add_endpoint(
        endpoint_id=endpoint_id,
        label="m4-synthetic-provider",
        base_url="https://api.example.invalid/v1",
        network_policy_id=uuid7(),
        network_policy_version=1,
        network_category="public",
        admin_id=ADMIN_ID,
    )
    keyring = CredentialKeyring(
        deployment_id="m4-synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"k" * 32)},
    )
    await repository.set_credential(
        profile_id=profile_id,
        logical_role=LogicalRole.MAIN_AI,
        expected_credential_version=1,
        secret=SensitiveValue("SYNTHETIC_M4_PROVIDER_KEY"),
        keyring=keyring,
        now=NOW,
    )
    draft_id = uuid7()
    capability_id = uuid7()
    await repository.create_draft(
        draft_id=draft_id,
        profile_id=profile_id,
        expected_profile_version=1,
        admin_id=ADMIN_ID,
        config=CanonicalModelConfig(
            profile_id,
            LogicalRole.MAIN_AI,
            endpoint_id,
            credential_ids[LogicalRole.MAIN_AI],
            ModelProtocol.OPENAI_RESPONSES,
            "m4-synthetic-model",
            0.2,
            512,
            30,
            True,
            {},
        ),
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    await repository.record_capabilities(
        snapshot_id=capability_id,
        endpoint_id=endpoint_id,
        protocol=ModelProtocol.OPENAI_RESPONSES,
        model_name="m4-synthetic-model",
        capabilities=capabilities(),
        metadata={},
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert await repository.validate_draft(
        draft_id=draft_id,
        expected_draft_version=1,
        capability_snapshot_id=capability_id,
        now=NOW,
    )
    await repository.activate_draft(
        draft_id=draft_id,
        expected_draft_version=2,
        admin_id=ADMIN_ID,
        now=NOW,
    )


def event(  # noqa: PLR0913 - all race dimensions are explicit
    *,
    account_id: UUID,
    conversation_id: UUID,
    chat_id: int,
    identity: str,
    message_id: int,
    observed_at: datetime,
    kind: EventKind = EventKind.MESSAGE_CREATED,
    direction: Direction = Direction.INCOMING,
    text_content: str | None = "synthetic incoming",
    random_id: int | None = None,
) -> NormalizedTelegramEvent:
    return normalize_update(
        event_uuid=uuid7(),
        admission=PeerAdmission(account_id, conversation_id, PeerKind.PRIVATE_USER, chat_id),
        raw=RawTelegramUpdate(
            identity,
            kind,
            observed_at,
            telegram_event_at=observed_at,
            telegram_message_id=message_id,
            direction=direction,
            outbound_random_id=random_id,
            text=text_content,
        ),
    )


async def enable_auto(
    repository: ConversationOrchestratorRepository,
    *,
    account_id: UUID,
    conversation_id: UUID,
) -> None:
    await repository.resolve(conversation_id, NOW)
    changed, version = await repository.set_account_control(
        account_id=account_id,
        actor_ref="admin:42",
        now=NOW,
        expected_version=1,
        default_base_mode=BaseMode.AUTO,
    )
    assert changed
    assert version == 2


async def ingest_incoming(  # noqa: PLR0913 - explicit event fixture
    session: AsyncSession,
    repository: ConversationOrchestratorRepository,
    *,
    account_id: UUID,
    conversation_id: UUID,
    chat_id: int,
    identity: str,
    message_id: int,
    observed_at: datetime,
) -> UUID:
    projected = await TelegramLifecycleRepository(session).ingest(
        event(
            account_id=account_id,
            conversation_id=conversation_id,
            chat_id=chat_id,
            identity=identity,
            message_id=message_id,
            observed_at=observed_at,
        )
    )
    assert projected.message_id is not None
    await repository.handle_new_incoming(
        conversation_id=conversation_id,
        message_id=projected.message_id,
        observed_at=observed_at,
    )
    return projected.message_id


@pytest.mark.integration
async def test_auto_turn_run_send_gate_and_reconciliation(db_session: AsyncSession) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    await activate_main_ai(db_session)
    repository = ConversationOrchestratorRepository(db_session)
    await enable_auto(repository, account_id=account_id, conversation_id=conversation_id)
    await ingest_incoming(
        db_session,
        repository,
        account_id=account_id,
        conversation_id=conversation_id,
        chat_id=chat_id,
        identity="m4:auto:1",
        message_id=10,
        observed_at=NOW + timedelta(seconds=1),
    )
    turn_id = await db_session.scalar(
        select(conversation_turns.c.id).where(
            conversation_turns.c.conversation_id == conversation_id,
            conversation_turns.c.state == "collecting",
        )
    )
    assert turn_id is not None
    await repository.seal_turn(turn_id=turn_id, now=NOW + timedelta(seconds=4))
    claim = await repository.start_generation(
        turn_id=turn_id,
        owner=OWNER,
        now=NOW + timedelta(seconds=4),
    )
    assert claim.max_telegram_message_id == 10
    assert claim.typing_lease_token is not None
    assert await repository.renew_generation_lease(
        run_id=claim.run.id,
        owner=OWNER,
        now=NOW + timedelta(seconds=5),
    )
    completed = await repository.complete_generation(
        run_id=claim.run.id,
        owner=OWNER,
        text_output="synthetic model reply",
        completed_at=NOW + timedelta(seconds=6),
        entropy=b"e" * 32,
        input_tokens=10,
        output_tokens=4,
    )
    assert completed.delivery_group_id is not None
    intent_id = await db_session.scalar(
        select(outbound_intents.c.id).where(
            outbound_intents.c.delivery_group_id == completed.delivery_group_id
        )
    )
    assert intent_id is not None
    intent = await repository.preflight_intent(
        intent_id=intent_id,
        owner=OWNER,
        now=NOW + timedelta(seconds=7),
    )
    assert intent is not None
    await TelegramLifecycleRepository(db_session).finish_attempt(
        intent=intent,
        completion=AttemptCompletionRecord(
            "succeeded",
            NOW + timedelta(seconds=8),
            telegram_message_id=100,
        ),
    )
    outgoing = await TelegramLifecycleRepository(db_session).ingest(
        event(
            account_id=account_id,
            conversation_id=conversation_id,
            chat_id=chat_id,
            identity="m4:auto:outgoing",
            message_id=100,
            observed_at=NOW + timedelta(seconds=9),
            direction=Direction.OUTGOING,
            text_content="synthetic model reply",
            random_id=intent.telegram_random_id,
        )
    )
    assert outgoing.source == "ai"
    assert await repository.reconcile_completed_delivery(
        conversation_id=conversation_id,
        telegram_message_id=100,
        now=NOW + timedelta(seconds=9),
    )
    assert (
        await db_session.scalar(
            select(conversation_turns.c.state).where(conversation_turns.c.id == turn_id)
        )
        == "completed"
    )
    assert await db_session.scalar(
        select(background_jobs.c.id).where(
            background_jobs.c.job_type == "memory.refresh_completed_turn"
        )
    )


@pytest.mark.integration
async def test_generation_grace_and_late_result_discard(db_session: AsyncSession) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    await activate_main_ai(db_session)
    repository = ConversationOrchestratorRepository(db_session)
    await enable_auto(repository, account_id=account_id, conversation_id=conversation_id)
    await ingest_incoming(
        db_session,
        repository,
        account_id=account_id,
        conversation_id=conversation_id,
        chat_id=chat_id,
        identity="m4:grace:1",
        message_id=20,
        observed_at=NOW + timedelta(seconds=1),
    )
    turn_id = await db_session.scalar(
        select(conversation_turns.c.id).where(
            conversation_turns.c.conversation_id == conversation_id,
            conversation_turns.c.state == "collecting",
        )
    )
    assert turn_id is not None
    await repository.seal_turn(turn_id=turn_id, now=NOW + timedelta(seconds=4))
    claim = await repository.start_generation(
        turn_id=turn_id,
        owner=OWNER,
        now=NOW + timedelta(seconds=4),
    )
    await ingest_incoming(
        db_session,
        repository,
        account_id=account_id,
        conversation_id=conversation_id,
        chat_id=chat_id,
        identity="m4:grace:2",
        message_id=21,
        observed_at=NOW + timedelta(seconds=5),
    )
    within = await repository.complete_generation(
        run_id=claim.run.id,
        owner=OWNER,
        text_output="within grace",
        completed_at=NOW + timedelta(seconds=6),
        entropy=b"g" * 32,
    )
    assert within.delivery_group_id is not None
    assert await db_session.scalar(
        select(turn_grace_authorizations.c.id).where(
            turn_grace_authorizations.c.model_run_id == claim.run.id
        )
    )

    second_account, second_conversation, second_chat = await seed_conversation(db_session)
    second = ConversationOrchestratorRepository(db_session)
    await enable_auto(
        second,
        account_id=second_account,
        conversation_id=second_conversation,
    )
    await ingest_incoming(
        db_session,
        second,
        account_id=second_account,
        conversation_id=second_conversation,
        chat_id=second_chat,
        identity="m4:late:1",
        message_id=30,
        observed_at=NOW + timedelta(seconds=1),
    )
    second_turn = await db_session.scalar(
        select(conversation_turns.c.id).where(
            conversation_turns.c.conversation_id == second_conversation,
            conversation_turns.c.state == "collecting",
        )
    )
    assert second_turn is not None
    await second.seal_turn(turn_id=second_turn, now=NOW + timedelta(seconds=4))
    second_claim = await second.start_generation(
        turn_id=second_turn,
        owner=OWNER,
        now=NOW + timedelta(seconds=4),
    )
    await ingest_incoming(
        db_session,
        second,
        account_id=second_account,
        conversation_id=second_conversation,
        chat_id=second_chat,
        identity="m4:late:2",
        message_id=31,
        observed_at=NOW + timedelta(seconds=8),
    )
    discarded = await second.complete_generation(
        run_id=second_claim.run.id,
        owner=OWNER,
        text_output="must be discarded",
        completed_at=NOW + timedelta(seconds=9),
        entropy=b"l" * 32,
    )
    assert discarded.state == "superseded"
    assert (
        await db_session.scalar(
            select(model_runs.c.state).where(model_runs.c.id == second_claim.run.id)
        )
        == "superseded"
    )
    assert await db_session.scalar(
        select(conversation_turns.c.id).where(
            conversation_turns.c.conversation_id == second_conversation,
            conversation_turns.c.state == "collecting",
        )
    )


@pytest.mark.integration
async def test_mode_flip_blocks_auto_and_copilot_tokens_are_one_time(
    db_session: AsyncSession,
) -> None:
    account_id, conversation_id, chat_id = await seed_conversation(db_session)
    await activate_main_ai(db_session)
    repository = ConversationOrchestratorRepository(db_session)
    await repository.resolve(conversation_id, NOW)
    switched = await repository.set_conversation_control(
        conversation_id=conversation_id,
        actor_ref="admin:42",
        now=NOW,
        expected_version=1,
        base_mode_override=BaseMode.COPILOT,
    )
    assert switched.changed
    await ingest_incoming(
        db_session,
        repository,
        account_id=account_id,
        conversation_id=conversation_id,
        chat_id=chat_id,
        identity="m4:copilot:1",
        message_id=40,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert not await db_session.scalar(
        select(conversation_turns.c.id).where(
            conversation_turns.c.conversation_id == conversation_id
        )
    )
    draft = await repository.request_copilot_draft(
        conversation_id=conversation_id,
        requested_by="admin:42",
        now=NOW + timedelta(seconds=2),
    )
    await repository.seal_turn(turn_id=draft.turn_id, now=NOW + timedelta(seconds=5))
    claim = await repository.start_generation(
        turn_id=draft.turn_id,
        owner=OWNER,
        now=NOW + timedelta(seconds=5),
    )
    generated = await repository.complete_generation(
        run_id=claim.run.id,
        owner=OWNER,
        text_output="original copilot draft",
        completed_at=NOW + timedelta(seconds=6),
        entropy=b"c" * 32,
    )
    assert generated.draft_id == draft.id
    await repository.issue_draft_token(
        draft_id=draft.id,
        raw_token=EDIT_TOKEN,
        admin_telegram_user_id=ADMIN_ID,
        bot_chat_id=BOT_CHAT_ID,
        purpose="edit",
        now=NOW + timedelta(seconds=7),
    )
    assert (
        await repository.edit_copilot_draft(
            raw_token=EDIT_TOKEN,
            admin_telegram_user_id=ADMIN_ID,
            bot_chat_id=BOT_CHAT_ID,
            text_output="edited copilot draft",
            now=NOW + timedelta(seconds=8),
        )
        == 2
    )
    with pytest.raises(OrchestratorConflictError, match="ACTION_TOKEN_STALE"):
        await repository.edit_copilot_draft(
            raw_token=EDIT_TOKEN,
            admin_telegram_user_id=ADMIN_ID,
            bot_chat_id=BOT_CHAT_ID,
            text_output="replay",
            now=NOW + timedelta(seconds=9),
        )
    await repository.issue_draft_token(
        draft_id=draft.id,
        raw_token=SEND_TOKEN,
        admin_telegram_user_id=ADMIN_ID,
        bot_chat_id=BOT_CHAT_ID,
        purpose="send",
        now=NOW + timedelta(seconds=9),
    )
    group_id = await repository.approve_copilot_draft(
        raw_token=SEND_TOKEN,
        admin_telegram_user_id=ADMIN_ID,
        bot_chat_id=BOT_CHAT_ID,
        owner=OWNER,
        entropy=b"a" * 32,
        now=NOW + timedelta(seconds=10),
    )
    assert await db_session.scalar(
        select(outbound_delivery_groups.c.id).where(
            outbound_delivery_groups.c.id == group_id,
            outbound_delivery_groups.c.source == "copilot_approved",
        )
    )
    assert (
        await db_session.scalar(
            select(copilot_drafts.c.state).where(copilot_drafts.c.id == draft.id)
        )
        == "send_queued"
    )
    assert (
        await db_session.scalar(
            select(copilot_draft_revisions.c.content_text).where(
                copilot_draft_revisions.c.draft_id == draft.id,
                copilot_draft_revisions.c.revision_no == 2,
            )
        )
        == "edited copilot draft"
    )
    assert await db_session.scalar(
        select(copilot_action_tokens.c.used_at).where(
            copilot_action_tokens.c.token_sha256.is_not(None),
            copilot_action_tokens.c.purpose == "send",
        )
    )


@pytest.mark.integration
async def test_m4_roles_and_wide_constraints_are_enforced(db_session: AsyncSession) -> None:
    privileges = {
        "app_runs": await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_app_runtime', "
                "'model_runs', 'SELECT,INSERT,UPDATE')"
            )
        ),
        "app_control_state": await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_app_runtime', "
                "'account_orchestrator_states', 'SELECT,INSERT,UPDATE')"
            )
        ),
        "control_commands": await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_control_runtime', "
                "'control_commands', 'SELECT,INSERT,UPDATE')"
            )
        ),
        "worker_read": await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_worker_runtime', "
                "'model_runs', 'SELECT')"
            )
        ),
        "app_delete": await db_session.scalar(
            text(
                "SELECT has_table_privilege('telegram_userbot_app_runtime', 'model_runs', 'DELETE')"
            )
        ),
    }
    assert privileges == {
        "app_runs": True,
        "app_control_state": True,
        "control_commands": True,
        "worker_read": True,
        "app_delete": False,
    }
    foreign_keys = set(
        (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname IN ("
                    "'fk_outbound_groups_turn_scope',"
                    "'fk_outbound_groups_model_run_scope',"
                    "'fk_outbound_intents_turn_scope',"
                    "'fk_outbound_intents_model_run_scope',"
                    "'fk_outbound_intents_group_m4_scope')"
                )
            )
        ).scalars()
    )
    assert foreign_keys == {
        "fk_outbound_groups_turn_scope",
        "fk_outbound_groups_model_run_scope",
        "fk_outbound_intents_turn_scope",
        "fk_outbound_intents_model_run_scope",
        "fk_outbound_intents_group_m4_scope",
    }
