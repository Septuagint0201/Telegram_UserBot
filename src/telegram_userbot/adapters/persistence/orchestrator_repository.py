"""PostgreSQL conversation coordinator and final-send authorization boundary."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import RowMapping, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.orchestrator_records import (
    ControlCommandRecord,
    ControlResult,
    ConversationActivityRecord,
    DraftRecord,
    GenerationClaim,
    ModelRunRecord,
    RunResult,
    TurnRecord,
)
from telegram_userbot.adapters.persistence.records import NewDeliveryGroupRecord, NewJobRecord
from telegram_userbot.adapters.persistence.repositories import DurableJobRepository
from telegram_userbot.adapters.persistence.schema import (
    account_control_history,
    account_orchestrator_states,
    accounts,
    contacts,
    control_commands,
    conversation_mode_history,
    conversation_turns,
    conversations,
    copilot_action_tokens,
    copilot_draft_revisions,
    copilot_drafts,
    message_events,
    message_revisions,
    messages,
    model_capability_snapshots,
    model_config_versions,
    model_credential_versions,
    model_credentials,
    model_profiles,
    model_run_attempts,
    model_runs,
    operational_blocks,
    outbound_delivery_groups,
    outbound_intents,
    transactional_outbox,
    turn_grace_authorizations,
    turn_grace_events,
    turn_messages,
)
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.domain.conversation import (
    AccountControl,
    BaseMode,
    ConversationControl,
    DebouncePolicy,
    DraftActionToken,
    DraftState,
    EffectiveMode,
    FinalGateInput,
    MaintenanceState,
    ModeResolution,
    TurnState,
    WorkSnapshot,
    evaluate_final_gate,
    generation_race_decision,
    resolve_mode,
    split_telegram_text,
)
from telegram_userbot.domain.messaging import (
    EventKind,
    OutboundChunk,
    payload_sha256,
    stable_telegram_random_id,
)


class OrchestratorConflictError(RuntimeError):
    """A version, mode, lease, or state gate rejected work."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _LockedScope:
    account: RowMapping
    conversation: RowMapping
    contact: RowMapping
    resolution: ModeResolution


@dataclass(frozen=True, slots=True)
class _MainProfile:
    profile_id: UUID
    config_version_id: UUID
    credential_version_id: UUID
    config_sha256: bytes
    capability_snapshot_sha256: bytes


def _capability_snapshot_digest(row: RowMapping) -> bytes:
    """Hash only admission-relevant capability data, never the config digest."""

    fields = {
        name: row[name]
        for name in (
            "endpoint_id",
            "protocol",
            "model_name",
            "supports_text",
            "supports_temperature",
            "supports_reasoning_effort",
            "supports_image",
            "supports_stream",
            "supports_structured_output",
            "chat_token_limit_field",
            "max_context_tokens",
            "max_output_tokens_limit",
            "max_images_per_request",
            "max_image_bytes_per_request",
            "auto_image_tokens",
            "messages_auto_detail_equivalent",
            "supported_input_roles",
            "embedding_dimensions",
            "metadata_schema_version",
            "metadata",
            "observed_at",
            "expires_at",
        )
        if name in row
    }
    return sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).digest()


def _turn(row: RowMapping) -> TurnRecord:
    return TurnRecord(
        row["id"],
        row["account_id"],
        row["conversation_id"],
        row["state"],
        row["trigger_kind"],
        row["active_generation_no"],
        WorkSnapshot(
            row["account_control_version_snapshot"],
            row["mode_version_snapshot"],
            row["content_revision_snapshot"],
            max(1, row["active_generation_no"]),
        ),
        row["quiet_deadline_at"],
        row["hard_deadline_at"],
        row["lease_owner"],
        row["lease_expires_at"],
        row["fencing_token"],
    )


def _run(row: RowMapping, trigger_kind: str) -> ModelRunRecord:
    started_at = row["started_at"]
    if started_at is None:
        raise RuntimeError("claimed model run has no start time")
    return ModelRunRecord(
        row["id"],
        row["account_id"],
        row["conversation_id"],
        row["turn_id"],
        row["state"],
        row["generation_no"],
        row["model_profile_id"],
        row["config_version_id"],
        row["credential_version_id"],
        row["input_fingerprint"],
        started_at,
        WorkSnapshot(
            row["account_control_version_snapshot"],
            row["mode_version_snapshot"],
            row["content_revision_snapshot"],
            row["generation_no"],
        ),
        trigger_kind,
    )


def _control_command(row: RowMapping) -> ControlCommandRecord:
    return ControlCommandRecord(
        row["id"],
        row["account_id"],
        row["conversation_id"],
        row["bot_identity"],
        row["telegram_update_id"],
        row["admin_telegram_user_id"],
        row["bot_chat_id"],
        row["command_kind"],
        row["expected_control_version"],
        row["expected_mode_version"],
        row.get("result_control_version"),
        row.get("result_mode_version"),
        row["state"],
        row["result_code"],
        row["result_changed"],
        row.get("result_payload"),
    )


class ConversationOrchestratorRepository:
    """All methods are transaction-scoped; this class never commits or calls external services."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        new_uuid: Callable[[], UUID] = uuid7,
        debounce: DebouncePolicy | None = None,
    ) -> None:
        self._session = session
        self._new_uuid = new_uuid
        self._debounce = debounce or DebouncePolicy()

    async def _ensure_account_state(self, account_id: UUID, now: datetime) -> None:
        await self._session.execute(
            postgresql_insert(account_orchestrator_states)
            .values(account_id=account_id, updated_by="system:bootstrap", updated_at=now)
            .on_conflict_do_nothing(index_elements=[account_orchestrator_states.c.account_id])
        )

    async def _locked_scope(self, conversation_id: UUID, now: datetime) -> _LockedScope:
        account_id = await self._session.scalar(
            select(conversations.c.account_id).where(conversations.c.id == conversation_id)
        )
        if account_id is None:
            raise OrchestratorConflictError("CONVERSATION_NOT_FOUND")
        await self._ensure_account_state(cast(UUID, account_id), now)
        account_row = (
            (
                await self._session.execute(
                    select(account_orchestrator_states)
                    .where(account_orchestrator_states.c.account_id == account_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        scope_row = (
            (
                await self._session.execute(
                    select(
                        conversations,
                        accounts.c.status.label("account_status"),
                        contacts.c.automation_status.label("automation_status"),
                    )
                    .join(accounts, accounts.c.id == conversations.c.account_id)
                    .join(
                        contacts,
                        and_(
                            contacts.c.id == conversations.c.contact_id,
                            contacts.c.account_id == conversations.c.account_id,
                        ),
                    )
                    .where(conversations.c.id == conversation_id)
                    .with_for_update(of=conversations)
                )
            )
            .mappings()
            .one()
        )
        block_reason = await self._session.scalar(
            select(operational_blocks.c.reason_code)
            .where(
                operational_blocks.c.account_id == account_id,
                operational_blocks.c.active.is_(True),
                or_(
                    operational_blocks.c.conversation_id.is_(None),
                    operational_blocks.c.conversation_id == conversation_id,
                ),
            )
            .order_by(operational_blocks.c.created_at, operational_blocks.c.id)
            .limit(1)
        )
        resolution = resolve_mode(
            account=AccountControl(
                BaseMode(account_row["default_base_mode"]),
                account_row["global_paused"],
                MaintenanceState(account_row["maintenance_state"]),
                account_row["control_version"],
                account_row["resume_floor_event_id"],
            ),
            conversation=ConversationControl(
                BaseMode(scope_row["base_mode_override"])
                if scope_row["base_mode_override"] is not None
                else None,
                scope_row["contact_paused"],
                scope_row["temporary_human_until"],
                scope_row["mode_version"],
                scope_row["content_revision"],
                scope_row["automation_resume_floor_event_id"],
                scope_row["last_response_covered_event_id"],
            ),
            now=now,
            account_active=scope_row["account_status"] == "active",
            contact_automation_status=scope_row["automation_status"],
            dependency_block_reason=cast(str | None, block_reason),
        )
        return _LockedScope(account_row, scope_row, scope_row, resolution)

    async def resolve(self, conversation_id: UUID, now: datetime) -> ModeResolution:
        return (await self._locked_scope(conversation_id, now)).resolution

    async def account_control(self, account_id: UUID, now: datetime) -> AccountControl:
        await self._ensure_account_state(account_id, now)
        row = (
            (
                await self._session.execute(
                    select(account_orchestrator_states)
                    .where(account_orchestrator_states.c.account_id == account_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        return AccountControl(
            BaseMode(row["default_base_mode"]),
            row["global_paused"],
            MaintenanceState(row["maintenance_state"]),
            row["control_version"],
            row["resume_floor_event_id"],
        )

    async def conversation_activity(
        self, conversation_id: UUID, now: datetime
    ) -> ConversationActivityRecord:
        scope = await self._locked_scope(conversation_id, now)
        watermark = scope.resolution.last_response_covered_event_id or 0
        unanswered_count = int(
            await self._session.scalar(
                select(func.count(messages.c.id))
                .join(
                    message_revisions,
                    and_(
                        message_revisions.c.message_id == messages.c.id,
                        message_revisions.c.account_id == messages.c.account_id,
                        message_revisions.c.revision_no == messages.c.current_revision_no,
                    ),
                )
                .where(
                    messages.c.conversation_id == conversation_id,
                    messages.c.direction == "incoming",
                    messages.c.source == "telegram_user",
                    messages.c.source_status == "resolved",
                    messages.c.is_tombstone.is_(False),
                    message_revisions.c.source_event_id > watermark,
                    message_revisions.c.redacted_at.is_(None),
                )
            )
            or 0
        )
        active_turn_state = cast(
            str | None,
            await self._session.scalar(
                select(conversation_turns.c.state)
                .where(
                    conversation_turns.c.conversation_id == conversation_id,
                    conversation_turns.c.state.in_(
                        ("collecting", "ready", "generating", "output_ready")
                    ),
                )
                .order_by(conversation_turns.c.collection_sequence.desc())
                .limit(1)
            ),
        )
        active_draft_state = cast(
            str | None,
            await self._session.scalar(
                select(copilot_drafts.c.state)
                .where(
                    copilot_drafts.c.conversation_id == conversation_id,
                    copilot_drafts.c.state.in_(
                        ("requested", "collecting", "generating", "ready", "editing", "approved")
                    ),
                )
                .order_by(copilot_drafts.c.requested_at.desc())
                .limit(1)
            ),
        )
        return ConversationActivityRecord(
            scope.resolution,
            unanswered_count,
            active_turn_state,
            active_draft_state,
        )

    async def _latest_event_id(self, account_id: UUID) -> int | None:
        return cast(
            int | None,
            await self._session.scalar(
                select(func.max(message_events.c.id)).where(
                    message_events.c.account_id == account_id
                )
            ),
        )

    async def _invalidate_pre_send(
        self, *, account_id: UUID, conversation_id: UUID | None, now: datetime, reason: str
    ) -> bool:
        condition = [model_runs.c.account_id == account_id]
        turn_condition = [conversation_turns.c.account_id == account_id]
        group_condition = [outbound_delivery_groups.c.account_id == account_id]
        draft_condition = [copilot_drafts.c.account_id == account_id]
        if conversation_id is not None:
            condition.append(model_runs.c.conversation_id == conversation_id)
            turn_condition.append(conversation_turns.c.conversation_id == conversation_id)
            group_condition.append(outbound_delivery_groups.c.conversation_id == conversation_id)
            draft_condition.append(copilot_drafts.c.conversation_id == conversation_id)
        run_ids = tuple(
            (
                await self._session.execute(
                    update(model_runs)
                    .where(
                        *condition, model_runs.c.state.in_(("created", "running", "output_ready"))
                    )
                    .values(state="cancelled", cancel_requested_at=now, error_code=reason)
                    .returning(model_runs.c.id)
                )
            ).scalars()
        )
        await self._session.execute(
            update(conversation_turns)
            .where(
                *turn_condition,
                conversation_turns.c.state.in_(
                    ("collecting", "ready", "generating", "output_ready")
                ),
            )
            .values(
                state="cancelled",
                terminal_reason=reason,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
            )
        )
        group_ids = tuple(
            (
                await self._session.execute(
                    update(outbound_delivery_groups)
                    .where(
                        *group_condition,
                        outbound_delivery_groups.c.state == "planned",
                    )
                    .values(state="cancelled", updated_at=now, completed_at=now)
                    .returning(outbound_delivery_groups.c.id)
                )
            ).scalars()
        )
        if group_ids:
            await self._session.execute(
                update(outbound_intents)
                .where(
                    outbound_intents.c.delivery_group_id.in_(group_ids),
                    outbound_intents.c.state.in_(("pending", "retry_wait")),
                )
                .values(state="cancelled", last_error_code=reason, updated_at=now)
            )
        await self._session.execute(
            update(copilot_drafts)
            .where(
                *draft_condition,
                copilot_drafts.c.state.in_(
                    (
                        "requested",
                        "collecting",
                        "generating",
                        "ready",
                        "editing",
                        "approved",
                    )
                ),
            )
            .values(state="invalidated", terminal_at=now, terminal_reason=reason)
        )
        return bool(run_ids or group_ids)

    async def set_account_control(  # noqa: PLR0913 - explicit command CAS contract
        self,
        *,
        account_id: UUID,
        actor_ref: str,
        now: datetime,
        expected_version: int,
        default_base_mode: BaseMode | None = None,
        global_paused: bool | None = None,
        maintenance_state: MaintenanceState | None = None,
    ) -> tuple[bool, int]:
        await self._ensure_account_state(account_id, now)
        row = (
            (
                await self._session.execute(
                    select(account_orchestrator_states)
                    .where(account_orchestrator_states.c.account_id == account_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if row["control_version"] != expected_version:
            raise OrchestratorConflictError("CONTROL_VERSION_CONFLICT")
        values = {
            "default_base_mode": default_base_mode or BaseMode(row["default_base_mode"]),
            "global_paused": row["global_paused"] if global_paused is None else global_paused,
            "maintenance_state": maintenance_state or MaintenanceState(row["maintenance_state"]),
        }
        changed = any(str(values[key]) != str(row[key]) for key in values)
        if not changed:
            return False, expected_version
        latest_event = await self._latest_event_id(account_id)
        resume = (
            (global_paused is False and row["global_paused"])
            or (
                maintenance_state is MaintenanceState.INACTIVE
                and row["maintenance_state"] != MaintenanceState.INACTIVE
            )
            or (default_base_mode is BaseMode.AUTO and row["default_base_mode"] != BaseMode.AUTO)
        )
        next_version = expected_version + 1
        await self._session.execute(
            update(account_orchestrator_states)
            .where(account_orchestrator_states.c.account_id == account_id)
            .values(
                **values,
                resume_floor_event_id=latest_event if resume else row["resume_floor_event_id"],
                control_version=next_version,
                updated_by=actor_ref,
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(account_control_history).values(
                account_id=account_id,
                control_version=next_version,
                change_kind="account_control",
                previous_state=f"{row['default_base_mode']}:{row['global_paused']}:{row['maintenance_state']}",
                new_state=f"{values['default_base_mode']}:{values['global_paused']}:{values['maintenance_state']}",
                reason="control_command",
                actor_type="admin",
                actor_ref=actor_ref,
                created_at=now,
            )
        )
        await self._invalidate_pre_send(
            account_id=account_id, conversation_id=None, now=now, reason="ACCOUNT_CONTROL_CHANGED"
        )
        return True, next_version

    async def set_conversation_control(  # noqa: PLR0913 - explicit command CAS contract
        self,
        *,
        conversation_id: UUID,
        actor_ref: str,
        now: datetime,
        expected_version: int,
        base_mode_override: BaseMode | object | None = ...,  # Ellipsis means unchanged.
        contact_paused: bool | None = None,
        cancel_only: bool = False,
    ) -> ControlResult:
        scope = await self._locked_scope(conversation_id, now)
        row = scope.conversation
        if row["mode_version"] != expected_version:
            raise OrchestratorConflictError("MODE_VERSION_CONFLICT")
        override = (
            row["base_mode_override"]
            if base_mode_override is ...
            else (str(base_mode_override) if base_mode_override is not None else None)
        )
        paused = row["contact_paused"] if contact_paused is None else contact_paused
        changed = (
            cancel_only or override != row["base_mode_override"] or paused != row["contact_paused"]
        )
        if not changed:
            return ControlResult(False, "NO_CHANGE", scope.resolution, False)
        will_resume_auto = (
            not paused
            and scope.resolution.effective_mode is not EffectiveMode.AUTO
            and (override or scope.account["default_base_mode"]) == BaseMode.AUTO
        )
        latest_event = await self._latest_event_id(row["account_id"])
        next_version = expected_version + 1
        await self._session.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(
                base_mode_override=override,
                contact_paused=paused,
                mode_version=next_version,
                automation_resume_floor_event_id=(
                    latest_event if will_resume_auto else row["automation_resume_floor_event_id"]
                ),
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(conversation_mode_history).values(
                account_id=row["account_id"],
                conversation_id=conversation_id,
                mode_version=next_version,
                change_kind="cancel" if cancel_only else "conversation_control",
                previous_state=f"{row['base_mode_override']}:{row['contact_paused']}",
                new_state=f"{override}:{paused}",
                reason="control_command",
                actor_type="admin",
                actor_ref=actor_ref,
                created_at=now,
            )
        )
        cancelled = await self._invalidate_pre_send(
            account_id=row["account_id"],
            conversation_id=conversation_id,
            now=now,
            reason="CONVERSATION_CONTROL_CHANGED",
        )
        resolution = (await self._locked_scope(conversation_id, now)).resolution
        return ControlResult(True, "CHANGED", resolution, cancelled)

    async def _eligible_message(
        self, *, conversation_id: UUID, message_id: UUID
    ) -> RowMapping | None:
        return (
            (
                await self._session.execute(
                    select(
                        messages.c.id,
                        messages.c.account_id,
                        messages.c.conversation_id,
                        messages.c.current_revision_no,
                        messages.c.telegram_message_id,
                        message_revisions.c.source_event_id,
                    )
                    .join(
                        message_revisions,
                        and_(
                            message_revisions.c.message_id == messages.c.id,
                            message_revisions.c.account_id == messages.c.account_id,
                            message_revisions.c.revision_no == messages.c.current_revision_no,
                        ),
                    )
                    .where(
                        messages.c.id == message_id,
                        messages.c.conversation_id == conversation_id,
                        messages.c.direction == "incoming",
                        messages.c.source == "telegram_user",
                        messages.c.source_status == "resolved",
                        messages.c.is_tombstone.is_(False),
                        message_revisions.c.redacted_at.is_(None),
                        or_(
                            message_revisions.c.text_content.is_not(None),
                            message_revisions.c.caption.is_not(None),
                        ),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def collect_message(
        self, *, conversation_id: UUID, message_id: UUID, observed_at: datetime
    ) -> TurnRecord | None:
        scope = await self._locked_scope(conversation_id, observed_at)
        if not scope.resolution.permits_auto:
            return None
        message = await self._eligible_message(
            conversation_id=conversation_id, message_id=message_id
        )
        if message is None:
            return None
        event_id = int(message["source_event_id"])
        floor = scope.resolution.automation_resume_floor_event_id
        coverage = scope.resolution.last_response_covered_event_id
        if (floor is not None and event_id <= floor) or (
            coverage is not None and event_id <= coverage
        ):
            return None
        active = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(
                        conversation_turns.c.conversation_id == conversation_id,
                        conversation_turns.c.state == TurnState.COLLECTING,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if active is None:
            sequence = (
                int(
                    await self._session.scalar(
                        select(
                            func.coalesce(func.max(conversation_turns.c.collection_sequence), 0)
                        ).where(conversation_turns.c.conversation_id == conversation_id)
                    )
                    or 0
                )
                + 1
            )
            turn_id = self._new_uuid()
            quiet, hard = self._debounce.collection_deadlines(
                started_at=observed_at, observed_at=observed_at
            )
            await self._session.execute(
                insert(conversation_turns).values(
                    id=turn_id,
                    account_id=message["account_id"],
                    conversation_id=conversation_id,
                    state=TurnState.COLLECTING,
                    trigger_kind="incoming",
                    collection_sequence=sequence,
                    base_mode_snapshot=scope.resolution.base_mode,
                    base_mode_source_snapshot=scope.resolution.base_source,
                    effective_mode_snapshot=scope.resolution.effective_mode,
                    account_control_version_snapshot=scope.resolution.account_control_version,
                    mode_version_snapshot=scope.resolution.mode_version,
                    content_revision_snapshot=scope.resolution.content_revision,
                    resume_floor_event_id_snapshot=floor,
                    coverage_event_id_snapshot=coverage,
                    debounce_seconds=self._debounce.quiet_seconds,
                    hard_cap_seconds=self._debounce.hard_cap_seconds,
                    collect_started_at=observed_at,
                    quiet_deadline_at=quiet,
                    hard_deadline_at=hard,
                    created_at=observed_at,
                )
            )
            active = (
                (
                    await self._session.execute(
                        select(conversation_turns).where(conversation_turns.c.id == turn_id)
                    )
                )
                .mappings()
                .one()
            )
        existing = await self._session.scalar(
            select(turn_messages.c.message_id).where(
                turn_messages.c.turn_id == active["id"],
                turn_messages.c.message_id == message_id,
            )
        )
        if existing is None:
            ordinal = (
                int(
                    await self._session.scalar(
                        select(func.coalesce(func.max(turn_messages.c.ordinal), 0)).where(
                            turn_messages.c.turn_id == active["id"]
                        )
                    )
                    or 0
                )
                + 1
            )
            await self._session.execute(
                insert(turn_messages).values(
                    turn_id=active["id"],
                    account_id=message["account_id"],
                    conversation_id=conversation_id,
                    message_id=message_id,
                    message_revision_no=message["current_revision_no"],
                    ordinal=ordinal,
                    source_event_id=event_id,
                )
            )
        quiet, _ = self._debounce.collection_deadlines(
            started_at=active["collect_started_at"] or active["created_at"],
            observed_at=observed_at,
        )
        updated = (
            (
                await self._session.execute(
                    update(conversation_turns)
                    .where(conversation_turns.c.id == active["id"])
                    .values(
                        quiet_deadline_at=quiet,
                        content_revision_snapshot=scope.resolution.content_revision,
                    )
                    .returning(*conversation_turns.c)
                )
            )
            .mappings()
            .one()
        )
        return _turn(updated)

    async def handle_new_incoming(
        self, *, conversation_id: UUID, message_id: UUID, observed_at: datetime
    ) -> TurnRecord | None:
        await self._locked_scope(conversation_id, observed_at)
        active = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(
                        conversation_turns.c.conversation_id == conversation_id,
                        conversation_turns.c.state.in_(("collecting", "ready", "generating")),
                    )
                    .order_by(conversation_turns.c.collection_sequence.desc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if active is None or active["state"] == "collecting":
            return await self.collect_message(
                conversation_id=conversation_id,
                message_id=message_id,
                observed_at=observed_at,
            )
        if active["state"] == "ready":
            await self._session.execute(
                update(conversation_turns)
                .where(conversation_turns.c.id == active["id"])
                .values(
                    state="superseded",
                    terminal_reason="NEW_INPUT_BEFORE_GENERATION",
                    completed_at=observed_at,
                )
            )
            return await self.create_pending_turn(
                conversation_id=conversation_id,
                trigger_kind="replacement",
                now=observed_at,
            )
        run = (
            (
                await self._session.execute(
                    select(model_runs)
                    .where(
                        model_runs.c.turn_id == active["id"],
                        model_runs.c.generation_no == active["active_generation_no"],
                        model_runs.c.state == "running",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            return None
        grace_deadline = run["started_at"] + timedelta(
            seconds=self._debounce.generation_grace_seconds
        )
        if observed_at < grace_deadline:
            return None
        await self._supersede(run, active, now=observed_at, reason="GENERATION_GRACE_EXPIRED")
        return await self.create_pending_turn(
            conversation_id=conversation_id,
            trigger_kind="replacement",
            now=observed_at,
        )

    async def expire_generation_grace(self, *, now: datetime) -> int:
        rows = tuple(
            (
                await self._session.execute(
                    select(model_runs)
                    .where(
                        model_runs.c.state == "running",
                        model_runs.c.started_at
                        <= now - timedelta(seconds=self._debounce.generation_grace_seconds),
                    )
                    .order_by(model_runs.c.started_at, model_runs.c.id)
                    .limit(100)
                )
            ).mappings()
        )
        expired = 0
        for candidate in rows:
            scope = await self._locked_scope(candidate["conversation_id"], now)
            turn = (
                (
                    await self._session.execute(
                        select(conversation_turns)
                        .where(conversation_turns.c.id == candidate["turn_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            run = (
                (
                    await self._session.execute(
                        select(model_runs)
                        .where(model_runs.c.id == candidate["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                run["state"] != "running"
                or run["started_at"]
                > now - timedelta(seconds=self._debounce.generation_grace_seconds)
                or scope.resolution.content_revision == run["content_revision_snapshot"]
            ):
                continue
            new_events = await self._new_revision_events(run)
            only_new_incoming = bool(new_events) and all(
                row["event_kind"] == EventKind.MESSAGE_CREATED.value
                and row["direction"] == "incoming"
                and row["source"] == "telegram_user"
                for row in new_events
            )
            if not only_new_incoming:
                continue
            await self._supersede(run, turn, now=now, reason="GENERATION_GRACE_EXPIRED")
            await self.create_pending_turn(
                conversation_id=run["conversation_id"],
                trigger_kind="replacement",
                now=now,
            )
            expired += 1
        return expired

    async def _pending_messages(
        self, scope: _LockedScope, *, ignore_resume_floor: bool
    ) -> tuple[RowMapping, ...]:
        watermark = scope.resolution.last_response_covered_event_id or 0
        if not ignore_resume_floor:
            watermark = max(watermark, scope.resolution.automation_resume_floor_event_id or 0)
        unresolved = await self._session.scalar(
            select(messages.c.id)
            .join(
                message_revisions,
                and_(
                    message_revisions.c.message_id == messages.c.id,
                    message_revisions.c.revision_no == messages.c.current_revision_no,
                ),
            )
            .where(
                messages.c.conversation_id == scope.conversation["id"],
                messages.c.direction == "outgoing",
                messages.c.source_status != "resolved",
                message_revisions.c.source_event_id > watermark,
            )
            .limit(1)
        )
        if unresolved is not None:
            raise OrchestratorConflictError("UNRESOLVED_OUTGOING")
        return tuple(
            (
                await self._session.execute(
                    select(
                        messages.c.id,
                        messages.c.account_id,
                        messages.c.conversation_id,
                        messages.c.current_revision_no,
                        messages.c.telegram_message_id,
                        message_revisions.c.source_event_id,
                    )
                    .join(
                        message_revisions,
                        and_(
                            message_revisions.c.message_id == messages.c.id,
                            message_revisions.c.account_id == messages.c.account_id,
                            message_revisions.c.revision_no == messages.c.current_revision_no,
                        ),
                    )
                    .where(
                        messages.c.conversation_id == scope.conversation["id"],
                        messages.c.direction == "incoming",
                        messages.c.source == "telegram_user",
                        messages.c.source_status == "resolved",
                        messages.c.is_tombstone.is_(False),
                        message_revisions.c.source_event_id > watermark,
                        message_revisions.c.redacted_at.is_(None),
                        or_(
                            message_revisions.c.text_content.is_not(None),
                            message_revisions.c.caption.is_not(None),
                        ),
                    )
                    .order_by(
                        message_revisions.c.source_event_id,
                        messages.c.telegram_message_id,
                    )
                    .limit(100)
                )
            ).mappings()
        )

    async def create_pending_turn(
        self,
        *,
        conversation_id: UUID,
        trigger_kind: str,
        now: datetime,
        ignore_resume_floor: bool = False,
    ) -> TurnRecord:
        scope = await self._locked_scope(conversation_id, now)
        expected = EffectiveMode.COPILOT if trigger_kind == "copilot" else EffectiveMode.AUTO
        if (
            scope.resolution.effective_mode is not expected
            or scope.resolution.operational_state.value != "READY"
        ):
            raise OrchestratorConflictError("MODE_OR_OPERATIONAL_GATE")
        rows = await self._pending_messages(scope, ignore_resume_floor=ignore_resume_floor)
        if not rows:
            raise OrchestratorConflictError("NO_PENDING_SEGMENT")
        if await self._session.scalar(
            select(conversation_turns.c.id)
            .where(
                conversation_turns.c.conversation_id == conversation_id,
                conversation_turns.c.state.in_(("collecting", "ready", "generating")),
            )
            .limit(1)
        ):
            raise OrchestratorConflictError("ACTIVE_TURN_EXISTS")
        sequence = (
            int(
                await self._session.scalar(
                    select(
                        func.coalesce(func.max(conversation_turns.c.collection_sequence), 0)
                    ).where(conversation_turns.c.conversation_id == conversation_id)
                )
                or 0
            )
            + 1
        )
        turn_id = self._new_uuid()
        quiet, hard = self._debounce.collection_deadlines(started_at=now, observed_at=now)
        await self._session.execute(
            insert(conversation_turns).values(
                id=turn_id,
                account_id=scope.conversation["account_id"],
                conversation_id=conversation_id,
                state="collecting",
                trigger_kind=trigger_kind,
                collection_sequence=sequence,
                base_mode_snapshot=scope.resolution.base_mode,
                base_mode_source_snapshot=scope.resolution.base_source,
                effective_mode_snapshot=scope.resolution.effective_mode,
                account_control_version_snapshot=scope.resolution.account_control_version,
                mode_version_snapshot=scope.resolution.mode_version,
                content_revision_snapshot=scope.resolution.content_revision,
                resume_floor_event_id_snapshot=scope.resolution.automation_resume_floor_event_id,
                coverage_event_id_snapshot=scope.resolution.last_response_covered_event_id,
                debounce_seconds=self._debounce.quiet_seconds,
                hard_cap_seconds=self._debounce.hard_cap_seconds,
                collect_started_at=now,
                quiet_deadline_at=quiet,
                hard_deadline_at=hard,
                created_at=now,
            )
        )
        for ordinal, row in enumerate(rows, start=1):
            await self._session.execute(
                insert(turn_messages).values(
                    turn_id=turn_id,
                    account_id=row["account_id"],
                    conversation_id=conversation_id,
                    message_id=row["id"],
                    message_revision_no=row["current_revision_no"],
                    ordinal=ordinal,
                    source_event_id=row["source_event_id"],
                )
            )
        row = (
            (
                await self._session.execute(
                    select(conversation_turns).where(conversation_turns.c.id == turn_id)
                )
            )
            .mappings()
            .one()
        )
        return _turn(row)

    async def seal_turn(self, *, turn_id: UUID, now: datetime) -> TurnRecord:
        conversation_id = await self._session.scalar(
            select(conversation_turns.c.conversation_id).where(conversation_turns.c.id == turn_id)
        )
        if conversation_id is None:
            raise OrchestratorConflictError("TURN_NOT_COLLECTING")
        scope = await self._locked_scope(cast(UUID, conversation_id), now)
        row = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == turn_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["state"] != "collecting":
            raise OrchestratorConflictError("TURN_NOT_COLLECTING")
        if now < min(row["quiet_deadline_at"], row["hard_deadline_at"]):
            raise OrchestratorConflictError("DEBOUNCE_NOT_DUE")
        if row["conversation_id"] != conversation_id:
            raise OrchestratorConflictError("TURN_SCOPE_CHANGED")
        expected = EffectiveMode.COPILOT if row["trigger_kind"] == "copilot" else EffectiveMode.AUTO
        if (
            scope.resolution.effective_mode is not expected
            or scope.resolution.operational_state.value != "READY"
            or scope.resolution.account_control_version != row["account_control_version_snapshot"]
            or scope.resolution.mode_version != row["mode_version_snapshot"]
            or scope.resolution.content_revision != row["content_revision_snapshot"]
        ):
            await self._session.execute(
                update(conversation_turns)
                .where(conversation_turns.c.id == turn_id)
                .values(state="cancelled", terminal_reason="SEAL_GATE_FAILED", completed_at=now)
            )
            raise OrchestratorConflictError("SEAL_GATE_FAILED")
        sealed = (
            (
                await self._session.execute(
                    update(conversation_turns)
                    .where(conversation_turns.c.id == turn_id)
                    .values(state="ready", sealed_at=now)
                    .returning(*conversation_turns.c)
                )
            )
            .mappings()
            .one()
        )
        return _turn(sealed)

    async def _main_profile(self) -> _MainProfile | None:
        row = (
            (
                await self._session.execute(
                    select(
                        model_profiles.c.id.label("profile_id"),
                        model_config_versions.c.id.label("config_version_id"),
                        model_credential_versions.c.id.label("credential_version_id"),
                        model_config_versions.c.config_sha256,
                        model_capability_snapshots,
                    )
                    .join(
                        model_config_versions,
                        and_(
                            model_config_versions.c.profile_id == model_profiles.c.id,
                            model_config_versions.c.version_no
                            == model_profiles.c.active_config_version_no,
                            model_config_versions.c.enabled.is_(True),
                        ),
                    )
                    .join(
                        model_credentials,
                        and_(
                            model_credentials.c.profile_id == model_profiles.c.id,
                            model_credentials.c.status == "active",
                        ),
                    )
                    .join(
                        model_capability_snapshots,
                        model_capability_snapshots.c.id
                        == model_config_versions.c.capability_snapshot_id,
                    )
                    .join(
                        model_credential_versions,
                        and_(
                            model_credential_versions.c.credential_id == model_credentials.c.id,
                            model_credential_versions.c.version_no
                            == model_credentials.c.active_version_no,
                            model_credential_versions.c.destroyed_at.is_(None),
                        ),
                    )
                    .where(
                        model_profiles.c.logical_role == "main_ai",
                        model_profiles.c.state == "active",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return (
            None
            if row is None
            else _MainProfile(
                row["profile_id"],
                row["config_version_id"],
                row["credential_version_id"],
                row["config_sha256"],
                _capability_snapshot_digest(row),
            )
        )

    async def _set_main_ai_block(self, account_id: UUID, now: datetime, active: bool) -> None:
        existing = await self._session.scalar(
            select(operational_blocks.c.id).where(
                operational_blocks.c.account_id == account_id,
                operational_blocks.c.conversation_id.is_(None),
                operational_blocks.c.reason_code == "MAIN_AI_UNAVAILABLE",
                operational_blocks.c.active.is_(True),
            )
        )
        if active and existing is None:
            await self._session.execute(
                insert(operational_blocks).values(
                    id=self._new_uuid(),
                    account_id=account_id,
                    reason_code="MAIN_AI_UNAVAILABLE",
                    created_at=now,
                )
            )
        elif not active and existing is not None:
            await self._session.execute(
                update(operational_blocks)
                .where(operational_blocks.c.id == existing)
                .values(active=False, cleared_at=now, version=operational_blocks.c.version + 1)
            )

    async def start_generation(
        self, *, turn_id: UUID, owner: UUID, now: datetime
    ) -> GenerationClaim:
        conversation_id = await self._session.scalar(
            select(conversation_turns.c.conversation_id).where(conversation_turns.c.id == turn_id)
        )
        if conversation_id is None:
            raise OrchestratorConflictError("TURN_NOT_READY")
        scope = await self._locked_scope(cast(UUID, conversation_id), now)
        turn_row = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == turn_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if turn_row is None or turn_row["state"] != "ready":
            raise OrchestratorConflictError("TURN_NOT_READY")
        profile = await self._main_profile()
        await self._set_main_ai_block(turn_row["account_id"], now, profile is None)
        if profile is None:
            raise OrchestratorConflictError("MAIN_AI_UNAVAILABLE")
        if turn_row["conversation_id"] != conversation_id:
            raise OrchestratorConflictError("TURN_SCOPE_CHANGED")
        expected = (
            EffectiveMode.COPILOT if turn_row["trigger_kind"] == "copilot" else EffectiveMode.AUTO
        )
        if (
            scope.resolution.effective_mode is not expected
            or scope.resolution.operational_state.value != "READY"
            or scope.resolution.account_control_version
            != turn_row["account_control_version_snapshot"]
            or scope.resolution.mode_version != turn_row["mode_version_snapshot"]
            or scope.resolution.content_revision != turn_row["content_revision_snapshot"]
        ):
            raise OrchestratorConflictError("GENERATION_GATE_FAILED")
        membership = tuple(
            (
                await self._session.execute(
                    select(
                        turn_messages.c.message_id,
                        turn_messages.c.message_revision_no,
                        messages.c.telegram_message_id,
                    )
                    .join(messages, messages.c.id == turn_messages.c.message_id)
                    .where(turn_messages.c.turn_id == turn_id)
                    .order_by(turn_messages.c.ordinal)
                )
            ).all()
        )
        if not membership:
            raise OrchestratorConflictError("TURN_HAS_NO_MESSAGES")
        input_fingerprint = sha256(
            b"m4-turn-input-v1\0"
            + b"\0".join(
                item.message_id.bytes + int(item.message_revision_no).to_bytes(4, "big")
                for item in membership
            )
        ).digest()
        generation_no = int(turn_row["active_generation_no"]) + 1
        run_id = self._new_uuid()
        lease_expires = now + timedelta(seconds=60)
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == turn_id)
            .values(
                state="generating",
                active_generation_no=generation_no,
                lease_owner=owner,
                lease_expires_at=lease_expires,
                fencing_token=conversation_turns.c.fencing_token + 1,
            )
        )
        prompt_hash = sha256(b"m4-main-ai-prompt-v1").digest()
        await self._session.execute(
            insert(model_runs).values(
                id=run_id,
                account_id=turn_row["account_id"],
                conversation_id=turn_row["conversation_id"],
                turn_id=turn_id,
                logical_role="main_ai",
                model_profile_id=profile.profile_id,
                purpose=(
                    "copilot_reactive_draft"
                    if turn_row["trigger_kind"] == "copilot"
                    else "conversation_reply"
                ),
                generation_no=generation_no,
                state="running",
                account_control_version_snapshot=scope.resolution.account_control_version,
                mode_version_snapshot=scope.resolution.mode_version,
                content_revision_snapshot=scope.resolution.content_revision,
                config_version_id=profile.config_version_id,
                credential_version_id=profile.credential_version_id,
                prompt_version="m4-main-ai-v1",
                prompt_bundle_sha256=prompt_hash,
                capability_snapshot_sha256=getattr(
                    profile,
                    "capability_snapshot_sha256",
                    sha256(b"capability-snapshot-unavailable").digest(),
                ),
                input_fingerprint=input_fingerprint,
                adapter_version="canonical-model-port-v1",
                request_schema_version=1,
                output_schema_version=1,
                normalizer_version="normalized-text-v1",
                started_at=now,
                created_at=now,
            )
        )
        await self._session.execute(
            insert(model_run_attempts).values(
                model_run_id=run_id,
                attempt_no=1,
                state="started",
                started_at=now,
            )
        )
        if turn_row["trigger_kind"] == "copilot":
            await self._session.execute(
                update(copilot_drafts)
                .where(copilot_drafts.c.turn_id == turn_id)
                .values(state="generating", model_run_id=run_id, model_role="main_ai")
            )
        run_row = (
            (await self._session.execute(select(model_runs).where(model_runs.c.id == run_id)))
            .mappings()
            .one()
        )
        return GenerationClaim(
            _run(run_row, turn_row["trigger_kind"]),
            scope.resolution,
            max(item.telegram_message_id for item in membership),
            self._new_uuid() if expected is EffectiveMode.AUTO else None,
        )

    async def renew_generation_lease(
        self,
        *,
        run_id: UUID,
        owner: UUID,
        now: datetime,
        lease_seconds: int = 60,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        identity = (
            (
                await self._session.execute(
                    select(model_runs.c.conversation_id, model_runs.c.turn_id).where(
                        model_runs.c.id == run_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            return False
        scope = await self._locked_scope(identity["conversation_id"], now)
        turn = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == identity["turn_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        run = (
            (
                await self._session.execute(
                    select(model_runs).where(model_runs.c.id == run_id).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        expected_mode = (
            EffectiveMode.COPILOT
            if run["purpose"] == "copilot_reactive_draft"
            else EffectiveMode.AUTO
        )
        if (
            run["state"] != "running"
            or turn["state"] != "generating"
            or turn["lease_owner"] != owner
            or turn["lease_expires_at"] <= now
            or turn["active_generation_no"] < 1
            or scope.resolution.effective_mode is not expected_mode
            or scope.resolution.operational_state.value != "READY"
            or scope.resolution.account_control_version != run["account_control_version_snapshot"]
            or scope.resolution.mode_version != run["mode_version_snapshot"]
        ):
            return False
        await self._session.execute(
            update(conversation_turns)
            .where(
                conversation_turns.c.id == identity["turn_id"],
                conversation_turns.c.lease_owner == owner,
                conversation_turns.c.state == "generating",
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return True

    async def _new_revision_events(self, run: RowMapping) -> tuple[RowMapping, ...]:
        max_included = int(
            await self._session.scalar(
                select(func.max(turn_messages.c.source_event_id)).where(
                    turn_messages.c.turn_id == run["turn_id"]
                )
            )
            or 0
        )
        return tuple(
            (
                await self._session.execute(
                    select(
                        message_events.c.id,
                        message_events.c.event_kind,
                        messages.c.direction,
                        messages.c.source,
                    )
                    .outerjoin(
                        messages,
                        and_(
                            messages.c.account_id == message_events.c.account_id,
                            messages.c.conversation_id == message_events.c.conversation_id,
                            messages.c.telegram_message_id == message_events.c.telegram_message_id,
                        ),
                    )
                    .where(
                        message_events.c.account_id == run["account_id"],
                        message_events.c.conversation_id == run["conversation_id"],
                        message_events.c.id > max_included,
                    )
                    .order_by(message_events.c.id)
                )
            ).mappings()
        )

    async def _supersede(
        self, run: RowMapping, turn: RowMapping, *, now: datetime, reason: str
    ) -> RunResult:
        await self._session.execute(
            update(model_runs)
            .where(model_runs.c.id == run["id"])
            .values(
                state="superseded",
                cancel_requested_at=now,
                completed_at=now,
                error_code=reason,
            )
        )
        await self._session.execute(
            update(model_run_attempts)
            .where(
                model_run_attempts.c.model_run_id == run["id"],
                model_run_attempts.c.state == "started",
            )
            .values(state="cancelled", completed_at=now, error_code=reason)
        )
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == turn["id"])
            .values(
                state="superseded",
                terminal_reason=reason,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
            )
        )
        await self._session.execute(
            update(copilot_drafts)
            .where(
                copilot_drafts.c.turn_id == turn["id"],
                copilot_drafts.c.state.in_(("requested", "collecting", "generating")),
            )
            .values(state="invalidated", terminal_at=now, terminal_reason=reason)
        )
        return RunResult(run["id"], "superseded", reason)

    async def complete_generation(  # noqa: PLR0913 - materialization gate is explicit
        self,
        *,
        run_id: UUID,
        owner: UUID,
        text_output: str,
        completed_at: datetime,
        entropy: bytes,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> RunResult:
        identity = (
            (
                await self._session.execute(
                    select(model_runs.c.conversation_id, model_runs.c.turn_id).where(
                        model_runs.c.id == run_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            raise OrchestratorConflictError("RUN_NOT_RUNNING")
        scope = await self._locked_scope(identity["conversation_id"], completed_at)
        turn = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == identity["turn_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        run = (
            (
                await self._session.execute(
                    select(model_runs).where(model_runs.c.id == run_id).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise OrchestratorConflictError("RUN_NOT_RUNNING")
        if run["state"] != "running":
            return RunResult(
                run_id,
                run["state"],
                run["error_code"] or f"RUN_{str(run['state']).upper()}",
            )
        if (
            run["conversation_id"] != identity["conversation_id"]
            or run["turn_id"] != identity["turn_id"]
        ):
            raise OrchestratorConflictError("RUN_SCOPE_CHANGED")
        new_events = await self._new_revision_events(run)
        only_new_incoming = bool(new_events) and all(
            row["event_kind"] == EventKind.MESSAGE_CREATED.value
            and row["direction"] == "incoming"
            and row["source"] == "telegram_user"
            for row in new_events
        )
        non_grace_change = (
            scope.resolution.account_control_version != run["account_control_version_snapshot"]
            or scope.resolution.mode_version != run["mode_version_snapshot"]
            or (
                scope.resolution.content_revision != run["content_revision_snapshot"]
                and not only_new_incoming
            )
        )
        race = generation_race_decision(
            has_new_incoming=only_new_incoming,
            has_non_grace_change=non_grace_change,
            run_started_at=run["started_at"],
            checked_at=completed_at,
            model_completed_at=completed_at,
            grace_seconds=self._debounce.generation_grace_seconds,
        )
        is_copilot = turn["trigger_kind"] == "copilot"
        if is_copilot and scope.resolution.content_revision != run["content_revision_snapshot"]:
            race = type(race).SUPERSEDE
        if race.value == "supersede":
            return await self._supersede(
                run, turn, now=completed_at, reason="STALE_DURING_GENERATION"
            )
        grace_authorized = race.value == "authorize_grace"
        if grace_authorized:
            authorization_id = self._new_uuid()
            await self._session.execute(
                insert(turn_grace_authorizations).values(
                    id=authorization_id,
                    account_id=run["account_id"],
                    conversation_id=run["conversation_id"],
                    turn_id=run["turn_id"],
                    model_run_id=run_id,
                    model_role="main_ai",
                    run_started_at=run["started_at"],
                    grace_deadline_at=run["started_at"]
                    + timedelta(seconds=self._debounce.generation_grace_seconds),
                    model_completed_at=completed_at,
                    authorized_at=completed_at,
                )
            )
            for event in new_events:
                await self._session.execute(
                    insert(turn_grace_events).values(
                        authorization_id=authorization_id,
                        message_event_id=event["id"],
                    )
                )
        required_mode = EffectiveMode.COPILOT if is_copilot else EffectiveMode.AUTO
        gate = evaluate_final_gate(
            FinalGateInput(
                WorkSnapshot(
                    run["account_control_version_snapshot"],
                    run["mode_version_snapshot"],
                    run["content_revision_snapshot"],
                    run["generation_no"],
                ),
                scope.resolution,
                required_mode,
                turn["state"] == "generating"
                and turn["active_generation_no"] == run["generation_no"],
                turn["lease_owner"] == owner and turn["lease_expires_at"] > completed_at,
                grace_authorized=grace_authorized,
            )
        )
        if not gate.allowed:
            return await self._supersede(run, turn, now=completed_at, reason=gate.reason)
        normalized = text_output.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            await self.fail_generation(
                run_id=run_id,
                owner=owner,
                now=completed_at,
                error_code="EMPTY_OUTPUT",
            )
            return RunResult(run_id, "failed", "EMPTY_OUTPUT")
        output_digest = sha256(normalized.encode()).digest()
        await self._session.execute(
            update(model_run_attempts)
            .where(
                model_run_attempts.c.model_run_id == run_id,
                model_run_attempts.c.state == "started",
            )
            .values(
                state="succeeded",
                completed_at=completed_at,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        if is_copilot:
            draft = (
                (
                    await self._session.execute(
                        select(copilot_drafts)
                        .where(copilot_drafts.c.turn_id == turn["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            revision_id = self._new_uuid()
            await self._session.execute(
                insert(copilot_draft_revisions).values(
                    id=revision_id,
                    account_id=run["account_id"],
                    conversation_id=run["conversation_id"],
                    draft_id=draft["id"],
                    revision_no=1,
                    author_type="model",
                    content_text=normalized,
                    content_sha256=output_digest,
                    created_at=completed_at,
                )
            )
            await self._session.execute(
                update(copilot_drafts)
                .where(copilot_drafts.c.id == draft["id"])
                .values(
                    state="ready",
                    current_revision_no=1,
                    ready_at=completed_at,
                    expires_at=completed_at + timedelta(minutes=30),
                )
            )
            await self._finish_run_row(
                run_id, completed_at, output_digest, input_tokens, output_tokens
            )
            await self._session.execute(
                update(conversation_turns)
                .where(conversation_turns.c.id == turn["id"])
                .values(state="output_ready", lease_owner=None, lease_expires_at=None)
            )
            return RunResult(run_id, "succeeded", "COPILOT_DRAFT_READY", draft_id=draft["id"])
        chunks = split_telegram_text(normalized)
        group_id = await self._create_group(
            run=run,
            turn=turn,
            source="ai",
            chunks=chunks,
            entropy=entropy,
            now=completed_at,
            output_digest=output_digest,
        )
        await self._finish_run_row(run_id, completed_at, output_digest, input_tokens, output_tokens)
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == turn["id"])
            .values(state="output_ready")
        )
        return RunResult(run_id, "succeeded", "DELIVERY_PLANNED", group_id)

    async def _finish_run_row(
        self,
        run_id: UUID,
        now: datetime,
        output_digest: bytes,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        await self._session.execute(
            update(model_runs)
            .where(model_runs.c.id == run_id)
            .values(
                state="succeeded",
                output_fingerprint=output_digest,
                finish_reason="stop",
                result_kind="text",
                is_complete=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                completed_at=now,
            )
        )

    async def _create_group(  # noqa: PLR0913 - all authorization bindings are explicit
        self,
        *,
        run: RowMapping,
        turn: RowMapping,
        source: str,
        chunks: Sequence[str],
        entropy: bytes,
        now: datetime,
        output_digest: bytes,
        copilot_draft_id: UUID | None = None,
        approved_revision_id: UUID | None = None,
    ) -> UUID:
        group_id = self._new_uuid()
        outbound_chunks = []
        for sequence_no, text_content in enumerate(chunks):
            intent_id = self._new_uuid()
            outbound_chunks.append(
                OutboundChunk(
                    intent_id,
                    sequence_no,
                    stable_telegram_random_id(intent_id, entropy),
                    text_content,
                    payload_sha256(text_content),
                )
            )
        idempotency = sha256(
            b"m4-delivery-v1\0"
            + run["id"].bytes
            + source.encode()
            + int(run["generation_no"]).to_bytes(4, "big")
            + output_digest
        ).digest()
        return await TelegramLifecycleRepository(
            self._session, new_uuid=self._new_uuid
        ).create_delivery_group(
            group=NewDeliveryGroupRecord(
                id=group_id,
                account_id=run["account_id"],
                conversation_id=run["conversation_id"],
                model_run_id=run["id"],
                source=source,
                idempotency_key=idempotency,
                created_at=now,
                mode_version=run["mode_version_snapshot"],
                content_revision=run["content_revision_snapshot"],
                turn_id=turn["id"],
                model_role="main_ai",
                generation_no=run["generation_no"],
                account_control_version=run["account_control_version_snapshot"],
                copilot_draft_id=copilot_draft_id,
                approved_draft_revision_id=approved_revision_id,
                logical_content_sha256=output_digest,
                max_delivery_chunks=16,
                send_authorized_at=now,
            ),
            chunks=tuple(outbound_chunks),
        )

    async def retry_generation_attempt(
        self,
        *,
        run_id: UUID,
        owner: UUID,
        now: datetime,
        error_code: str,
    ) -> bool:
        """Record a bounded retry without granting a new run or lease."""

        identity = (
            (
                await self._session.execute(
                    select(model_runs.c.conversation_id, model_runs.c.turn_id).where(
                        model_runs.c.id == run_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            return False
        await self._locked_scope(identity["conversation_id"], now)
        turn = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == identity["turn_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        run = (
            (
                await self._session.execute(
                    select(model_runs).where(model_runs.c.id == run_id).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            turn is None
            or run is None
            or run["state"] != "running"
            or turn["state"] != "generating"
            or turn["lease_owner"] != owner
            or turn["lease_expires_at"] <= now
        ):
            return False
        current_attempt = await self._session.scalar(
            select(func.max(model_run_attempts.c.attempt_no)).where(
                model_run_attempts.c.model_run_id == run_id
            )
        )
        if current_attempt is None:
            return False
        result = await self._session.execute(
            update(model_run_attempts)
            .where(
                model_run_attempts.c.model_run_id == run_id,
                model_run_attempts.c.attempt_no == current_attempt,
                model_run_attempts.c.state == "started",
            )
            .values(state="retryable_failed", completed_at=now, error_code=error_code)
        )
        if getattr(result, "rowcount", 1) != 1:
            return False
        await self._session.execute(
            insert(model_run_attempts).values(
                model_run_id=run_id,
                attempt_no=int(current_attempt) + 1,
                state="started",
                started_at=now,
            )
        )
        return True

    async def fail_generation(
        self,
        *,
        run_id: UUID,
        now: datetime,
        error_code: str,
        owner: UUID | None = None,
    ) -> None:
        identity = (
            (
                await self._session.execute(
                    select(model_runs.c.conversation_id, model_runs.c.turn_id).where(
                        model_runs.c.id == run_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            return
        await self._locked_scope(identity["conversation_id"], now)
        turn = (
            (
                await self._session.execute(
                    select(conversation_turns)
                    .where(conversation_turns.c.id == identity["turn_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        run = (
            (
                await self._session.execute(
                    select(model_runs).where(model_runs.c.id == run_id).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None or turn is None or run["state"] != "running":
            return
        if (
            run["conversation_id"] != identity["conversation_id"]
            or run["turn_id"] != identity["turn_id"]
        ):
            raise OrchestratorConflictError("RUN_SCOPE_CHANGED")
        if owner is not None and (turn["lease_owner"] != owner or turn["lease_expires_at"] <= now):
            return
        await self._session.execute(
            update(model_run_attempts)
            .where(
                model_run_attempts.c.model_run_id == run_id,
                model_run_attempts.c.state == "started",
            )
            .values(state="terminal_failed", completed_at=now, error_code=error_code)
        )
        await self._session.execute(
            update(model_runs)
            .where(model_runs.c.id == run_id)
            .values(state="failed", completed_at=now, error_code=error_code, is_complete=False)
        )
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == run["turn_id"])
            .values(
                state="failed",
                terminal_reason=error_code,
                completed_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        await self._session.execute(
            update(copilot_drafts)
            .where(copilot_drafts.c.model_run_id == run_id)
            .values(state="failed", terminal_at=now, terminal_reason=error_code)
        )

    async def recover_expired_generations(self, *, now: datetime, limit: int = 100) -> int:
        """Fence orphaned provider calls before a runtime resumes dispatching."""

        if not 1 <= limit <= 1000:
            raise ValueError("generation recovery limit must be between 1 and 1000")
        candidates = tuple(
            (
                await self._session.execute(
                    select(model_runs.c.id, model_runs.c.conversation_id, model_runs.c.turn_id)
                    .join(conversation_turns, conversation_turns.c.id == model_runs.c.turn_id)
                    .where(
                        model_runs.c.state == "running",
                        conversation_turns.c.state == "generating",
                        conversation_turns.c.lease_expires_at <= now,
                    )
                    .order_by(conversation_turns.c.lease_expires_at, model_runs.c.id)
                    .limit(limit)
                )
            ).mappings()
        )
        recovered = 0
        for candidate in candidates:
            await self._locked_scope(candidate["conversation_id"], now)
            turn = (
                (
                    await self._session.execute(
                        select(conversation_turns)
                        .where(conversation_turns.c.id == candidate["turn_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            run = (
                (
                    await self._session.execute(
                        select(model_runs)
                        .where(model_runs.c.id == candidate["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                turn is None
                or run is None
                or run["state"] != "running"
                or turn["state"] != "generating"
                or turn["lease_expires_at"] > now
            ):
                continue
            await self._session.execute(
                update(model_run_attempts)
                .where(
                    model_run_attempts.c.model_run_id == run["id"],
                    model_run_attempts.c.state == "started",
                )
                .values(
                    state="unknown",
                    completed_at=now,
                    error_code="GENERATION_LEASE_EXPIRED",
                )
            )
            await self._session.execute(
                update(model_runs)
                .where(model_runs.c.id == run["id"], model_runs.c.state == "running")
                .values(
                    state="failed",
                    completed_at=now,
                    error_code="GENERATION_LEASE_EXPIRED",
                    is_complete=False,
                )
            )
            await self._session.execute(
                update(conversation_turns)
                .where(
                    conversation_turns.c.id == turn["id"],
                    conversation_turns.c.state == "generating",
                    conversation_turns.c.lease_expires_at <= now,
                )
                .values(
                    state="failed",
                    terminal_reason="GENERATION_LEASE_EXPIRED",
                    completed_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            recovered += 1
        return recovered

    async def _exact_grace_authorized(self, row: RowMapping) -> bool:
        authorization_id = await self._session.scalar(
            select(turn_grace_authorizations.c.id).where(
                turn_grace_authorizations.c.model_run_id == row["model_run_id"]
            )
        )
        if authorization_id is None:
            return False
        max_included = int(
            await self._session.scalar(
                select(func.max(turn_messages.c.source_event_id)).where(
                    turn_messages.c.turn_id == row["turn_id"]
                )
            )
            or 0
        )
        authorized_ids = frozenset(
            (
                await self._session.execute(
                    select(turn_grace_events.c.message_event_id).where(
                        turn_grace_events.c.authorization_id == authorization_id
                    )
                )
            ).scalars()
        )
        actual = tuple(
            (
                await self._session.execute(
                    select(
                        message_events.c.id,
                        message_events.c.event_kind,
                        messages.c.direction,
                        messages.c.source,
                    )
                    .outerjoin(
                        messages,
                        and_(
                            messages.c.account_id == message_events.c.account_id,
                            messages.c.conversation_id == message_events.c.conversation_id,
                            messages.c.telegram_message_id == message_events.c.telegram_message_id,
                        ),
                    )
                    .where(
                        message_events.c.account_id == row["account_id"],
                        message_events.c.conversation_id == row["conversation_id"],
                        message_events.c.id > max_included,
                    )
                    .order_by(message_events.c.id)
                )
            ).mappings()
        )
        return (
            bool(actual)
            and authorized_ids == frozenset(item["id"] for item in actual)
            and all(
                item["event_kind"] == EventKind.MESSAGE_CREATED.value
                and item["direction"] == "incoming"
                and item["source"] == "telegram_user"
                for item in actual
            )
        )

    async def _continuation_source_valid(self, row: RowMapping) -> bool:
        """Validate immutable turn sources and reject human takeover between chunks."""

        invalid_source = await self._session.scalar(
            select(turn_messages.c.message_id)
            .join(messages, messages.c.id == turn_messages.c.message_id)
            .join(
                message_revisions,
                and_(
                    message_revisions.c.message_id == turn_messages.c.message_id,
                    message_revisions.c.account_id == turn_messages.c.account_id,
                    message_revisions.c.revision_no == turn_messages.c.message_revision_no,
                ),
            )
            .where(
                turn_messages.c.turn_id == row["turn_id"],
                or_(
                    messages.c.current_revision_no != turn_messages.c.message_revision_no,
                    messages.c.is_tombstone.is_(True),
                    message_revisions.c.redacted_at.is_not(None),
                ),
            )
            .limit(1)
        )
        if invalid_source is not None:
            return False
        max_included = int(
            await self._session.scalar(
                select(func.max(turn_messages.c.source_event_id)).where(
                    turn_messages.c.turn_id == row["turn_id"]
                )
            )
            or 0
        )
        human_outgoing = await self._session.scalar(
            select(message_events.c.id)
            .join(
                messages,
                and_(
                    messages.c.account_id == message_events.c.account_id,
                    messages.c.conversation_id == message_events.c.conversation_id,
                    messages.c.telegram_message_id == message_events.c.telegram_message_id,
                ),
            )
            .where(
                message_events.c.account_id == row["account_id"],
                message_events.c.conversation_id == row["conversation_id"],
                message_events.c.id > max_included,
                message_events.c.event_kind == EventKind.MESSAGE_CREATED.value,
                messages.c.direction == "outgoing",
                messages.c.source == "human",
            )
            .limit(1)
        )
        return human_outgoing is None

    async def _continuation_ordinal_ready(self, row: RowMapping) -> bool:
        previous_unsent = await self._session.scalar(
            select(outbound_intents.c.id)
            .where(
                outbound_intents.c.delivery_group_id == row["delivery_group_id"],
                outbound_intents.c.sequence_no < row["sequence_no"],
                outbound_intents.c.state != "sent",
            )
            .limit(1)
        )
        return previous_unsent is None

    async def _cancel_delivery_remainder(
        self, row: RowMapping, *, now: datetime, reason: str
    ) -> None:
        """Terminally cancel this and later unclaimed chunks after a failed gate."""

        await self._session.execute(
            update(outbound_intents)
            .where(
                outbound_intents.c.delivery_group_id == row["delivery_group_id"],
                outbound_intents.c.sequence_no >= row["sequence_no"],
                outbound_intents.c.state.in_(("pending", "retry_wait")),
            )
            .values(state="cancelled", last_error_code=reason, updated_at=now)
        )
        await self._session.execute(
            update(outbound_delivery_groups)
            .where(outbound_delivery_groups.c.id == row["delivery_group_id"])
            .values(
                state="partial" if row["first_side_effect_at"] is not None else "cancelled",
                updated_at=now,
                completed_at=now,
            )
        )

    async def preflight_intent(self, *, intent_id: UUID, owner: UUID, now: datetime) -> Any | None:
        identity = (
            (
                await self._session.execute(
                    select(
                        outbound_intents.c.conversation_id,
                        outbound_intents.c.turn_id,
                        outbound_intents.c.delivery_group_id,
                    ).where(outbound_intents.c.id == intent_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            return None
        scope = await self._locked_scope(identity["conversation_id"], now)
        await self._session.execute(
            select(conversation_turns.c.id)
            .where(conversation_turns.c.id == identity["turn_id"])
            .with_for_update()
        )
        await self._session.execute(
            select(outbound_delivery_groups.c.id)
            .where(outbound_delivery_groups.c.id == identity["delivery_group_id"])
            .with_for_update()
        )
        row = (
            (
                await self._session.execute(
                    select(
                        outbound_intents,
                        outbound_delivery_groups.c.state.label("group_state"),
                        outbound_delivery_groups.c.first_side_effect_at,
                        outbound_delivery_groups.c.sent_count,
                        conversation_turns.c.state.label("turn_state"),
                        conversation_turns.c.lease_owner,
                        conversation_turns.c.lease_expires_at,
                        conversation_turns.c.active_generation_no,
                    )
                    .join(
                        outbound_delivery_groups,
                        outbound_delivery_groups.c.id == outbound_intents.c.delivery_group_id,
                    )
                    .join(conversation_turns, conversation_turns.c.id == outbound_intents.c.turn_id)
                    .where(outbound_intents.c.id == intent_id)
                    .with_for_update(of=outbound_intents)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if (
            row["conversation_id"] != identity["conversation_id"]
            or row["turn_id"] != identity["turn_id"]
            or row["delivery_group_id"] != identity["delivery_group_id"]
        ):
            raise OrchestratorConflictError("INTENT_SCOPE_CHANGED")
        # Claiming an intent records the conservative point at which an RPC may
        # have produced a side effect. It does not prove a chunk was delivered.
        # Only a durable successful prior chunk enables continuation semantics.
        continuation = row["sent_count"] > 0
        grace_authorized = (
            not continuation
            and scope.resolution.content_revision != row["content_revision"]
            and await self._exact_grace_authorized(row)
        )
        source_valid = await self._continuation_source_valid(row)
        # Ordering is a dispatch prerequisite, not a terminal authorization
        # failure. A scheduler that observes a later chunk first must leave it
        # pending so the preceding chunk can still make progress.
        if not await self._continuation_ordinal_ready(row):
            return None
        gate = evaluate_final_gate(
            FinalGateInput(
                WorkSnapshot(
                    row["account_control_version"],
                    row["mode_version"],
                    row["content_revision"],
                    row["generation_no"],
                ),
                scope.resolution,
                EffectiveMode.COPILOT
                if row["source"] == "copilot_approved"
                else EffectiveMode.AUTO,
                row["turn_state"] == "output_ready"
                and row["active_generation_no"] == row["generation_no"],
                row["lease_owner"] == owner and row["lease_expires_at"] > now,
                duplicate_delivery=row["group_state"] not in ("planned", "sending", "partial"),
                grace_authorized=grace_authorized,
                source_valid=source_valid,
                content_revision_required=not continuation,
            )
        )
        if not gate.allowed:
            await self._cancel_delivery_remainder(row, now=now, reason=gate.reason)
            return None
        intent = await TelegramLifecycleRepository(
            self._session, new_uuid=self._new_uuid
        ).claim_intent(account_id=row["account_id"], intent_id=intent_id, now=now)
        if intent is None:
            return None
        await self._session.execute(
            update(outbound_delivery_groups)
            .where(
                outbound_delivery_groups.c.id == row["delivery_group_id"],
                outbound_delivery_groups.c.first_side_effect_at.is_(None),
            )
            .values(first_side_effect_at=now, updated_at=now)
        )
        return intent

    async def request_copilot_draft(
        self, *, conversation_id: UUID, requested_by: str, now: datetime
    ) -> DraftRecord:
        turn = await self.create_pending_turn(
            conversation_id=conversation_id,
            trigger_kind="copilot",
            now=now,
            ignore_resume_floor=True,
        )
        contact_id = await self._session.scalar(
            select(conversations.c.contact_id).where(conversations.c.id == conversation_id)
        )
        draft_id = self._new_uuid()
        await self._session.execute(
            insert(copilot_drafts).values(
                id=draft_id,
                account_id=turn.account_id,
                contact_id=contact_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                draft_kind="reactive",
                state="collecting",
                account_control_version_snapshot=turn.snapshot.account_control_version,
                mode_version_snapshot=turn.snapshot.mode_version,
                content_revision_snapshot=turn.snapshot.content_revision,
                requested_by=requested_by,
                requested_at=now,
            )
        )
        return DraftRecord(
            draft_id,
            turn.account_id,
            conversation_id,
            turn.id,
            None,
            "collecting",
            None,
            None,
            turn.snapshot,
        )

    async def issue_draft_token(  # noqa: PLR0913 - token bindings are security-relevant
        self,
        *,
        draft_id: UUID,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        purpose: str,
        now: datetime,
        ttl: timedelta = timedelta(minutes=5),
    ) -> UUID:
        draft = (
            (
                await self._session.execute(
                    select(copilot_drafts).where(copilot_drafts.c.id == draft_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if draft is None or draft["state"] != "ready" or draft["expires_at"] <= now:
            raise OrchestratorConflictError("DRAFT_NOT_ACTIONABLE")
        token_id = self._new_uuid()
        await self._session.execute(
            insert(copilot_action_tokens).values(
                id=token_id,
                token_sha256=sha256(raw_token.encode()).digest(),
                account_id=draft["account_id"],
                conversation_id=draft["conversation_id"],
                draft_id=draft_id,
                draft_revision_no=draft["current_revision_no"],
                admin_telegram_user_id=admin_telegram_user_id,
                bot_chat_id=bot_chat_id,
                purpose=purpose,
                expires_at=min(now + ttl, draft["expires_at"]),
                created_at=now,
            )
        )
        return token_id

    async def _consume_draft_token(
        self,
        *,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        purpose: str,
        now: datetime,
    ) -> tuple[RowMapping, RowMapping, _LockedScope]:
        digest = sha256(raw_token.encode()).digest()
        identity = (
            (
                await self._session.execute(
                    select(
                        copilot_action_tokens.c.draft_id,
                        copilot_drafts.c.conversation_id,
                        copilot_drafts.c.turn_id,
                    )
                    .join(
                        copilot_drafts,
                        copilot_drafts.c.id == copilot_action_tokens.c.draft_id,
                    )
                    .where(copilot_action_tokens.c.token_sha256 == digest)
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None:
            raise OrchestratorConflictError("ACTION_TOKEN_INVALID")
        scope = await self._locked_scope(identity["conversation_id"], now)
        await self._session.execute(
            select(conversation_turns.c.id)
            .where(conversation_turns.c.id == identity["turn_id"])
            .with_for_update()
        )
        draft = (
            (
                await self._session.execute(
                    select(copilot_drafts)
                    .where(copilot_drafts.c.id == identity["draft_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        token = (
            (
                await self._session.execute(
                    select(copilot_action_tokens)
                    .where(copilot_action_tokens.c.token_sha256 == digest)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if token is None or token["draft_id"] != draft["id"]:
            raise OrchestratorConflictError("ACTION_TOKEN_INVALID")
        binding = DraftActionToken(
            token["token_sha256"],
            token["admin_telegram_user_id"],
            token["bot_chat_id"],
            token["purpose"],
            token["draft_revision_no"],
            token["expires_at"],
            token["used_at"],
        )
        if not binding.accepts(
            raw_token=raw_token,
            admin_telegram_user_id=admin_telegram_user_id,
            bot_chat_id=bot_chat_id,
            purpose=purpose,
            revision_no=draft["current_revision_no"],
            now=now,
        ):
            raise OrchestratorConflictError("ACTION_TOKEN_STALE")
        await self._session.execute(
            update(copilot_action_tokens)
            .where(copilot_action_tokens.c.id == token["id"])
            .values(used_at=now)
        )
        return token, draft, scope

    async def edit_copilot_draft(
        self,
        *,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        text_output: str,
        now: datetime,
    ) -> int:
        _, draft, _ = await self._consume_draft_token(
            raw_token=raw_token,
            admin_telegram_user_id=admin_telegram_user_id,
            bot_chat_id=bot_chat_id,
            purpose="edit",
            now=now,
        )
        normalized = text_output.replace("\r\n", "\n").replace("\r", "\n").strip()
        split_telegram_text(normalized)
        revision_no = int(draft["current_revision_no"]) + 1
        await self._session.execute(
            insert(copilot_draft_revisions).values(
                id=self._new_uuid(),
                account_id=draft["account_id"],
                conversation_id=draft["conversation_id"],
                draft_id=draft["id"],
                revision_no=revision_no,
                author_type="admin_edit",
                content_text=normalized,
                content_sha256=sha256(normalized.encode()).digest(),
                created_at=now,
            )
        )
        await self._session.execute(
            update(copilot_drafts)
            .where(copilot_drafts.c.id == draft["id"])
            .values(state="ready", current_revision_no=revision_no)
        )
        return revision_no

    async def ignore_copilot_draft(
        self,
        *,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> None:
        _, draft, _ = await self._consume_draft_token(
            raw_token=raw_token,
            admin_telegram_user_id=admin_telegram_user_id,
            bot_chat_id=bot_chat_id,
            purpose="ignore",
            now=now,
        )
        await self._session.execute(
            update(copilot_drafts)
            .where(copilot_drafts.c.id == draft["id"])
            .values(state="ignored", terminal_at=now, terminal_reason="ADMIN_IGNORED")
        )

    async def approve_copilot_draft(  # noqa: PLR0913 - callback binding is explicit
        self,
        *,
        raw_token: str,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        owner: UUID,
        entropy: bytes,
        now: datetime,
    ) -> UUID:
        _, draft, scope = await self._consume_draft_token(
            raw_token=raw_token,
            admin_telegram_user_id=admin_telegram_user_id,
            bot_chat_id=bot_chat_id,
            purpose="send",
            now=now,
        )
        if draft["state"] != DraftState.READY or draft["expires_at"] <= now:
            raise OrchestratorConflictError("DRAFT_NOT_ACTIONABLE")
        run = (
            (
                await self._session.execute(
                    select(model_runs).where(model_runs.c.id == draft["model_run_id"])
                )
            )
            .mappings()
            .one()
        )
        turn = (
            (
                await self._session.execute(
                    select(conversation_turns).where(conversation_turns.c.id == draft["turn_id"])
                )
            )
            .mappings()
            .one()
        )
        gate = evaluate_final_gate(
            FinalGateInput(
                WorkSnapshot(
                    draft["account_control_version_snapshot"],
                    draft["mode_version_snapshot"],
                    draft["content_revision_snapshot"],
                    run["generation_no"],
                ),
                scope.resolution,
                EffectiveMode.COPILOT,
                turn["state"] == "output_ready",
                True,  # Control approval establishes a fresh app-side lease below.
            )
        )
        if not gate.allowed:
            raise OrchestratorConflictError(gate.reason)
        revision = (
            (
                await self._session.execute(
                    select(copilot_draft_revisions).where(
                        copilot_draft_revisions.c.draft_id == draft["id"],
                        copilot_draft_revisions.c.revision_no == draft["current_revision_no"],
                    )
                )
            )
            .mappings()
            .one()
        )
        chunks = split_telegram_text(revision["content_text"])
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == turn["id"])
            .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=60))
        )
        group_id = await self._create_group(
            run=run,
            turn=turn,
            source="copilot_approved",
            chunks=chunks,
            entropy=entropy,
            now=now,
            output_digest=revision["content_sha256"],
            copilot_draft_id=draft["id"],
            approved_revision_id=revision["id"],
        )
        await self._session.execute(
            update(copilot_drafts)
            .where(copilot_drafts.c.id == draft["id"])
            .values(
                state="send_queued",
                approved_by=f"admin:{admin_telegram_user_id}",
                approved_at=now,
            )
        )
        return group_id

    async def expire_copilot_drafts(self, *, now: datetime) -> int:
        result = await self._session.execute(
            update(copilot_drafts)
            .where(
                copilot_drafts.c.state.in_(("ready", "editing")),
                copilot_drafts.c.expires_at <= now,
            )
            .values(state="expired", terminal_at=now, terminal_reason="DRAFT_TTL_EXPIRED")
            .returning(copilot_drafts.c.id)
        )
        return len(tuple(result.scalars()))

    async def expire_temporary_human(self, *, now: datetime) -> int:
        rows = tuple(
            (
                await self._session.execute(
                    select(conversations)
                    .where(
                        conversations.c.temporary_human_until.is_not(None),
                        conversations.c.temporary_human_until <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).mappings()
        )
        for row in rows:
            latest = await self._latest_event_id(row["account_id"])
            next_version = row["mode_version"] + 1
            await self._session.execute(
                update(conversations)
                .where(
                    conversations.c.id == row["id"],
                    conversations.c.temporary_human_until == row["temporary_human_until"],
                )
                .values(
                    temporary_human_until=None,
                    mode_version=next_version,
                    automation_resume_floor_event_id=latest,
                    updated_at=now,
                )
            )
            await self._session.execute(
                insert(conversation_mode_history).values(
                    account_id=row["account_id"],
                    conversation_id=row["id"],
                    mode_version=next_version,
                    change_kind="temporary_human_expired",
                    previous_state="temporary_human",
                    new_state="base_mode",
                    reason="deadline_expired",
                    actor_type="system",
                    actor_ref="orchestrator_scan",
                    created_at=now,
                )
            )
        return len(rows)

    async def end_temporary_human(
        self,
        *,
        conversation_id: UUID,
        actor_ref: str,
        expected_version: int,
        now: datetime,
    ) -> ControlResult:
        scope = await self._locked_scope(conversation_id, now)
        row = scope.conversation
        if row["mode_version"] != expected_version:
            raise OrchestratorConflictError("MODE_VERSION_CONFLICT")
        if row["temporary_human_until"] is None:
            return ControlResult(False, "NO_TEMPORARY_TAKEOVER", scope.resolution, False)
        latest = await self._latest_event_id(row["account_id"])
        next_version = expected_version + 1
        await self._session.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(
                temporary_human_until=None,
                mode_version=next_version,
                automation_resume_floor_event_id=latest,
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(conversation_mode_history).values(
                account_id=row["account_id"],
                conversation_id=conversation_id,
                mode_version=next_version,
                change_kind="temporary_human_ended",
                previous_state="temporary_human",
                new_state="base_mode",
                reason="control_command",
                actor_type="admin",
                actor_ref=actor_ref,
                created_at=now,
            )
        )
        cancelled = await self._invalidate_pre_send(
            account_id=row["account_id"],
            conversation_id=conversation_id,
            now=now,
            reason="TEMPORARY_TAKEOVER_ENDED",
        )
        resolution = (await self._locked_scope(conversation_id, now)).resolution
        return ControlResult(True, "TAKEOVER_ENDED", resolution, cancelled)

    async def human_takeover_after_ingest(
        self,
        *,
        conversation_id: UUID,
        now: datetime,
        actor_ref: str,
        human_event_id: int | None = None,
    ) -> None:
        scope = await self._locked_scope(conversation_id, now)
        temporary_until = None
        if scope.account["temporary_takeover_enabled"]:
            temporary_until = now + timedelta(seconds=scope.account["temporary_takeover_seconds"])
        if temporary_until is not None:
            await self._session.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id)
                .values(temporary_human_until=temporary_until, updated_at=now)
            )
        if human_event_id is not None:
            covered = await self._session.scalar(
                select(func.max(message_revisions.c.source_event_id))
                .select_from(
                    messages.join(
                        message_revisions,
                        and_(
                            message_revisions.c.message_id == messages.c.id,
                            message_revisions.c.revision_no == messages.c.current_revision_no,
                        ),
                    )
                )
                .where(
                    messages.c.conversation_id == conversation_id,
                    messages.c.direction == "incoming",
                    message_revisions.c.source_event_id < human_event_id,
                )
            )
            if covered is not None:
                await self._session.execute(
                    update(conversations)
                    .where(conversations.c.id == conversation_id)
                    .values(
                        last_response_covered_event_id=func.greatest(
                            func.coalesce(conversations.c.last_response_covered_event_id, 0),
                            covered,
                        )
                    )
                )
        await self._invalidate_pre_send(
            account_id=scope.conversation["account_id"],
            conversation_id=conversation_id,
            now=now,
            reason="HUMAN_OUTGOING",
        )
        await self._session.execute(
            insert(conversation_mode_history).values(
                account_id=scope.conversation["account_id"],
                conversation_id=conversation_id,
                mode_version=scope.conversation["mode_version"],
                change_kind="human_outgoing",
                previous_state=None,
                new_state="temporary_human" if temporary_until else None,
                reason="confirmed_human_outgoing",
                actor_type="human",
                actor_ref=actor_ref,
                created_at=now,
            )
        )

    async def invalidate_after_content_change(
        self, *, conversation_id: UUID, now: datetime, reason: str
    ) -> None:
        scope = await self._locked_scope(conversation_id, now)
        await self._invalidate_pre_send(
            account_id=scope.conversation["account_id"],
            conversation_id=conversation_id,
            now=now,
            reason=reason,
        )

    async def queue_memory_refresh(self, *, turn_id: UUID, now: datetime) -> None:
        account_id = await self._session.scalar(
            select(conversation_turns.c.account_id).where(conversation_turns.c.id == turn_id)
        )
        if account_id is None:
            return
        await DurableJobRepository(self._session).create(
            NewJobRecord(
                id=self._new_uuid(),
                account_id=cast(UUID, account_id),
                queue_name="memory",
                job_type="memory.refresh_completed_turn",
                idempotency_key=sha256(f"memory-turn-v1:{turn_id}".encode()).digest(),
                payload={"turn_id": str(turn_id)},
                available_at=now,
            )
        )

    async def reconcile_completed_delivery(
        self, *, conversation_id: UUID, telegram_message_id: int, now: datetime
    ) -> bool:
        group_id = await self._session.scalar(
            select(outbound_intents.c.delivery_group_id).where(
                outbound_intents.c.conversation_id == conversation_id,
                outbound_intents.c.telegram_message_id == telegram_message_id,
            )
        )
        if group_id is None:
            return False
        group_identity = (
            (
                await self._session.execute(
                    select(
                        outbound_delivery_groups.c.conversation_id,
                        outbound_delivery_groups.c.turn_id,
                    ).where(outbound_delivery_groups.c.id == group_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if group_identity is None or group_identity["conversation_id"] != conversation_id:
            return False
        await self._locked_scope(conversation_id, now)
        await self._session.execute(
            select(conversation_turns.c.id)
            .where(conversation_turns.c.id == group_identity["turn_id"])
            .with_for_update()
        )
        group = (
            (
                await self._session.execute(
                    select(outbound_delivery_groups)
                    .where(outbound_delivery_groups.c.id == group_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if group["turn_id"] != group_identity["turn_id"]:
            raise OrchestratorConflictError("DELIVERY_GROUP_SCOPE_CHANGED")
        counts = (
            await self._session.execute(
                select(
                    func.count(outbound_intents.c.id),
                    func.count(messages.c.id),
                )
                .select_from(
                    outbound_intents.outerjoin(
                        messages,
                        and_(
                            messages.c.account_id == outbound_intents.c.account_id,
                            messages.c.conversation_id == outbound_intents.c.conversation_id,
                            messages.c.telegram_message_id
                            == outbound_intents.c.telegram_message_id,
                            messages.c.direction == "outgoing",
                            messages.c.source == group["source"],
                            messages.c.source_status.in_(("resolved", "corrected")),
                        ),
                    )
                )
                .where(outbound_intents.c.delivery_group_id == group_id)
            )
        ).one()
        if counts[0] == 0 or counts[0] != counts[1]:
            return False
        coverage = await self._session.scalar(
            select(func.max(turn_messages.c.source_event_id)).where(
                turn_messages.c.turn_id == group["turn_id"]
            )
        )
        await self._session.execute(
            update(conversation_turns)
            .where(conversation_turns.c.id == group["turn_id"])
            .values(
                state="completed",
                completed_at=now,
                lease_owner=None,
                lease_expires_at=None,
                terminal_reason=None,
            )
        )
        if coverage is not None:
            await self._session.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id)
                .values(
                    last_response_covered_event_id=func.greatest(
                        func.coalesce(conversations.c.last_response_covered_event_id, 0), coverage
                    ),
                    last_completed_turn_at=now,
                    updated_at=now,
                )
            )
        if group["copilot_draft_id"] is not None:
            await self._session.execute(
                update(copilot_drafts)
                .where(copilot_drafts.c.id == group["copilot_draft_id"])
                .values(state="sent", terminal_at=now, terminal_reason=None)
            )
        await self.queue_memory_refresh(turn_id=group["turn_id"], now=now)
        if group["source"] == "ai":
            try:
                await self.create_pending_turn(
                    conversation_id=conversation_id,
                    trigger_kind="replacement",
                    now=now,
                )
            except OrchestratorConflictError as error:
                if error.code not in {"NO_PENDING_SEGMENT", "ACTIVE_TURN_EXISTS"}:
                    raise
        return True

    async def record_control_command(  # noqa: PLR0913 - durable identity is explicit
        self,
        *,
        command_id: UUID,
        account_id: UUID,
        conversation_id: UUID | None,
        bot_identity: str,
        telegram_update_id: int,
        admin_telegram_user_id: int,
        bot_chat_id: int,
        command_kind: str,
        idempotency_key: bytes,
        now: datetime,
        expected_control_version: int | None = None,
        expected_mode_version: int | None = None,
    ) -> ControlCommandRecord:
        inserted = (
            (
                await self._session.execute(
                    postgresql_insert(control_commands)
                    .values(
                        id=command_id,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        bot_identity=bot_identity,
                        telegram_update_id=telegram_update_id,
                        admin_telegram_user_id=admin_telegram_user_id,
                        bot_chat_id=bot_chat_id,
                        command_kind=command_kind,
                        idempotency_key=idempotency_key,
                        expected_control_version=expected_control_version,
                        expected_mode_version=expected_mode_version,
                        state="pending",
                        created_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_control_commands_bot_update")
                    .returning(*control_commands.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if inserted is not None:
            return _control_command(inserted)
        existing = (
            (
                await self._session.execute(
                    select(control_commands).where(
                        control_commands.c.bot_identity == bot_identity,
                        control_commands.c.telegram_update_id == telegram_update_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise RuntimeError("idempotent control command disappeared")
        if (
            existing["account_id"] != account_id
            or existing["conversation_id"] != conversation_id
            or existing["admin_telegram_user_id"] != admin_telegram_user_id
            or existing["bot_chat_id"] != bot_chat_id
            or existing["command_kind"] != command_kind
            or existing["idempotency_key"] != idempotency_key
        ):
            raise OrchestratorConflictError("CONTROL_COMMAND_IDENTITY_MISMATCH")
        return _control_command(existing)

    async def get_control_command(
        self, *, bot_identity: str, telegram_update_id: int
    ) -> ControlCommandRecord | None:
        row = (
            (
                await self._session.execute(
                    select(control_commands).where(
                        control_commands.c.bot_identity == bot_identity,
                        control_commands.c.telegram_update_id == telegram_update_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _control_command(row) if row is not None else None

    async def claim_pending_control_command(
        self,
        *,
        account_id: UUID,
        bot_identity: str,
        command_id: UUID | None = None,
    ) -> ControlCommandRecord | None:
        """Lock the oldest queued command for one app-owned transaction."""

        conditions = [
            control_commands.c.account_id == account_id,
            control_commands.c.bot_identity == bot_identity,
            control_commands.c.state == "pending",
        ]
        if command_id is not None:
            conditions.append(control_commands.c.id == command_id)
        row = (
            (
                await self._session.execute(
                    select(control_commands)
                    .where(*conditions)
                    .order_by(control_commands.c.created_at, control_commands.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        return _control_command(row) if row is not None else None

    async def bind_control_command_versions(
        self,
        *,
        command_id: UUID,
        expected_control_version: int | None,
        expected_mode_version: int | None,
    ) -> None:
        result = cast(
            Any,
            await self._session.execute(
                update(control_commands)
                .where(control_commands.c.id == command_id, control_commands.c.state == "pending")
                .values(
                    expected_control_version=expected_control_version,
                    expected_mode_version=expected_mode_version,
                )
            ),
        )
        if result.rowcount != 1:
            raise OrchestratorConflictError("CONTROL_COMMAND_NOT_PENDING")

    async def finish_control_command(  # noqa: PLR0913 - terminal evidence is explicit
        self,
        *,
        command_id: UUID,
        result_code: str,
        accepted: bool,
        result_changed: bool,
        now: datetime,
        result_control_version: int | None = None,
        result_mode_version: int | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "state": "applied" if accepted else "rejected",
            "result_code": result_code,
            "result_changed": result_changed,
            "result_control_version": result_control_version,
            "result_mode_version": result_mode_version,
            "completed_at": now,
        }
        # JSONB binds Python None as JSON null.  Non-status commands must leave
        # the pending row's SQL NULL untouched so the object-only constraint
        # remains meaningful.
        if result_payload is not None:
            values["result_payload"] = result_payload
        result = cast(
            Any,
            await self._session.execute(
                update(control_commands)
                .where(control_commands.c.id == command_id, control_commands.c.state == "pending")
                .values(**values)
            ),
        )
        if result.rowcount != 1:
            raise OrchestratorConflictError("CONTROL_COMMAND_NOT_PENDING")

    async def add_invalidation_outbox(
        self, *, account_id: UUID, aggregate_id: str, version: int, now: datetime
    ) -> None:
        await self._session.execute(
            postgresql_insert(transactional_outbox)
            .values(
                account_id=account_id,
                topic="orchestrator.invalidated",
                aggregate_type="conversation_control",
                aggregate_id=aggregate_id,
                aggregate_version=version,
                payload_schema_version=1,
                payload={"aggregate_id": aggregate_id, "version": version},
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_transactional_outbox_generation")
        )

    async def add_control_command_outbox(
        self,
        *,
        command_id: UUID,
        account_id: UUID,
        topic: str,
        now: datetime,
    ) -> None:
        if topic not in {"control.command.requested", "control.command.completed"}:
            raise ValueError("unsupported control command outbox topic")
        await self._session.execute(
            postgresql_insert(transactional_outbox)
            .values(
                account_id=account_id,
                topic=topic,
                aggregate_type="control_command",
                aggregate_id=str(command_id),
                aggregate_version=1 if topic.endswith("requested") else 2,
                payload_schema_version=1,
                payload={"command_id": str(command_id)},
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_transactional_outbox_generation")
        )
