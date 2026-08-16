"""Transactional Telegram ingest, outbound identity, and recovery repository."""

from collections.abc import Callable, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import RowMapping, insert, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    AttemptCompletionRecord,
    NewDeliveryGroupRecord,
    NewJobRecord,
    OutboundIntentRecord,
    ReadHighWatermarkRecord,
    TelegramIngestResult,
    TypingLeaseRecord,
)
from telegram_userbot.adapters.persistence.repositories import DurableJobRepository
from telegram_userbot.adapters.persistence.schema import (
    conversations,
    message_events,
    message_media,
    message_reactions,
    message_revisions,
    messages,
    outbound_attempts,
    outbound_delivery_groups,
    outbound_intents,
    telegram_operations,
    telegram_peers,
    telegram_read_states,
    telegram_typing_states,
)
from telegram_userbot.domain.messaging import (
    AttemptOutcome,
    BodyKind,
    DeliveryGroupState,
    Direction,
    EventKind,
    NormalizedTelegramEvent,
    OutboundChunk,
    OutboundIntentState,
)


def _intent(row: RowMapping) -> OutboundIntentRecord:
    return OutboundIntentRecord(
        id=row["id"],
        delivery_group_id=row["delivery_group_id"],
        account_id=row["account_id"],
        conversation_id=row["conversation_id"],
        model_run_id=row["model_run_id"],
        sequence_no=row["sequence_no"],
        telegram_random_id=row["telegram_random_id"],
        text_content=row["text_content"],
        payload_sha256=row["payload_sha256"],
        state=row["state"],
        telegram_message_id=row["telegram_message_id"],
        attempt_count=row["attempt_count"],
    )


class TelegramLifecycleRepository:
    """No method commits; the caller owns the enclosing transaction."""

    def __init__(self, session: AsyncSession, *, new_uuid: Callable[[], UUID] = uuid7) -> None:
        self._session = session
        self._new_uuid = new_uuid

    async def ingest(self, event: NormalizedTelegramEvent) -> TelegramIngestResult:
        event_metadata = {
            "peer_kind": event.peer_kind,
            "telegram_chat_id": event.telegram_chat_id,
            "sender_telegram_peer_id": event.sender_telegram_peer_id,
            "outbound_random_id": event.outbound_random_id,
            "service_kind": event.service_kind,
            **event.metadata,
        }
        event_statement = (
            postgresql_insert(message_events)
            .values(
                event_uuid=event.event_uuid,
                account_id=event.account_id,
                conversation_id=event.conversation_id,
                event_kind=event.event_kind,
                telegram_message_id=event.telegram_message_id,
                grouped_id=event.grouped_id,
                fingerprint_version=1,
                update_fingerprint=event.update_fingerprint,
                telegram_event_at=event.telegram_event_at,
                observed_at=event.observed_at,
                ordering_key=event.ordering_key,
                metadata_schema_version=1,
                metadata=event_metadata,
            )
            .on_conflict_do_nothing(constraint="uq_message_events_fingerprint")
            .returning(message_events.c.id)
        )
        event_id = await self._session.scalar(event_statement)
        if event_id is None:
            existing_id = await self._session.scalar(
                select(message_events.c.id).where(
                    message_events.c.account_id == event.account_id,
                    message_events.c.fingerprint_version == 1,
                    message_events.c.update_fingerprint == event.update_fingerprint,
                )
            )
            if existing_id is None:
                raise RuntimeError("deduplicated Telegram event disappeared")
            return TelegramIngestResult(int(existing_id), True, False)

        if not event.peer_kind.supported or event.conversation_id is None:
            await self._mark_projected(int(event_id), event.observed_at)
            return TelegramIngestResult(int(event_id), False, True)

        result = TelegramIngestResult(int(event_id), False, False)
        if event.event_kind in {
            EventKind.MESSAGE_CREATED,
            EventKind.MESSAGE_EDITED,
            EventKind.MESSAGE_DELETED,
        }:
            result = await self._project_message(int(event_id), event)
        elif event.event_kind is EventKind.REACTION_CHANGED:
            result = await self._project_reaction(int(event_id), event)
        await self._mark_projected(int(event_id), event.observed_at)
        return result

    async def _mark_projected(self, event_id: int, now: datetime) -> None:
        await self._session.execute(
            update(message_events).where(message_events.c.id == event_id).values(projected_at=now)
        )

    async def _project_message(
        self, event_id: int, event: NormalizedTelegramEvent
    ) -> TelegramIngestResult:
        assert event.conversation_id is not None
        assert event.telegram_message_id is not None
        row = (
            (
                await self._session.execute(
                    select(messages)
                    .where(
                        messages.c.account_id == event.account_id,
                        messages.c.conversation_id == event.conversation_id,
                        messages.c.telegram_message_id == event.telegram_message_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return await self._create_message(event_id, event)
        return await self._update_message(event_id, event, row)

    async def _classify_source(
        self, event: NormalizedTelegramEvent
    ) -> tuple[str, str, UUID | None]:
        if event.direction is not Direction.OUTGOING:
            return "telegram_user", "resolved", None
        assert event.conversation_id is not None
        predicates = []
        if event.outbound_random_id is not None:
            predicates.append(outbound_intents.c.telegram_random_id == event.outbound_random_id)
        if event.telegram_message_id is not None:
            predicates.append(outbound_intents.c.telegram_message_id == event.telegram_message_id)
        matched: RowMapping | None = None
        if predicates:
            matched = (
                (
                    await self._session.execute(
                        select(
                            outbound_intents.c.id,
                            outbound_intents.c.state,
                            outbound_delivery_groups.c.source,
                        )
                        .select_from(
                            outbound_intents.join(
                                outbound_delivery_groups,
                                outbound_intents.c.delivery_group_id
                                == outbound_delivery_groups.c.id,
                            )
                        )
                        .where(
                            outbound_intents.c.account_id == event.account_id,
                            outbound_intents.c.conversation_id == event.conversation_id,
                            or_(*predicates),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if matched is not None:
            return str(matched["source"]), "resolved", cast(UUID, matched["id"])
        active = await self._session.scalar(
            select(outbound_intents.c.id)
            .where(
                outbound_intents.c.account_id == event.account_id,
                outbound_intents.c.conversation_id == event.conversation_id,
                outbound_intents.c.state.in_(("sending", "unknown")),
            )
            .limit(1)
        )
        if active is not None:
            return "system_pending", "pending", None
        return "human", "resolved", None

    async def _create_message(
        self, event_id: int, event: NormalizedTelegramEvent
    ) -> TelegramIngestResult:
        assert event.conversation_id is not None
        assert event.telegram_message_id is not None
        source, source_status, intent_id = await self._classify_source(event)
        message_id = self._new_uuid()
        created_at = event.telegram_event_at or event.observed_at
        is_delete = event.event_kind is EventKind.MESSAGE_DELETED
        direction = event.direction or Direction.INCOMING
        await self._session.execute(
            insert(messages).values(
                id=message_id,
                account_id=event.account_id,
                conversation_id=event.conversation_id,
                telegram_message_id=event.telegram_message_id,
                direction=direction,
                role="assistant" if direction is Direction.OUTGOING else "user",
                source=source,
                source_status=source_status,
                current_revision_no=1,
                grouped_id=event.grouped_id,
                reply_to_telegram_message_id=event.reply_to_telegram_message_id,
                telegram_created_at=created_at,
                edited_at=created_at if event.event_kind is EventKind.MESSAGE_EDITED else None,
                deleted_at=event.observed_at if is_delete else None,
                is_tombstone=is_delete,
                first_observed_at=event.observed_at,
                last_observed_at=event.observed_at,
                metadata_schema_version=1,
                metadata={"created_from_event_kind": event.event_kind},
            )
        )
        body = event.body if not is_delete else None
        await self._insert_revision(message_id, 1, event_id, event, body)
        if body is not None:
            await self._insert_media(message_id, event.account_id, 1, event)
        if intent_id is not None:
            await self._reconcile_intent(intent_id, event.telegram_message_id, event.observed_at)
        await self._advance_conversation(event, human_takeover=source == "human")
        if is_delete:
            await self._queue_delete_reconciliation(event, message_id)
        return TelegramIngestResult(event_id, False, True, message_id, 1, source)

    async def _insert_revision(
        self,
        message_id: UUID,
        revision_no: int,
        event_id: int,
        event: NormalizedTelegramEvent,
        body: object | None,
    ) -> None:
        normalized_body = event.body if body is not None else None
        kind = normalized_body.kind if normalized_body is not None else BodyKind.NONE
        text_content = (
            normalized_body.text if normalized_body is not None and kind is BodyKind.TEXT else None
        )
        caption = (
            normalized_body.text
            if normalized_body is not None and kind is BodyKind.CAPTION
            else None
        )
        await self._session.execute(
            insert(message_revisions).values(
                id=self._new_uuid(),
                account_id=event.account_id,
                message_id=message_id,
                revision_no=revision_no,
                body_kind=kind,
                text_content=text_content,
                caption=caption,
                entities_schema_version=1,
                entities=list(normalized_body.entities) if normalized_body is not None else None,
                content_sha256=(
                    normalized_body.content_sha256 if normalized_body is not None else None
                ),
                source_event_id=event_id,
                telegram_edited_at=(
                    event.telegram_event_at
                    if event.event_kind is EventKind.MESSAGE_EDITED
                    else None
                ),
            )
        )

    async def _insert_media(
        self, message_id: UUID, account_id: UUID, revision_no: int, event: NormalizedTelegramEvent
    ) -> None:
        revision_id = await self._session.scalar(
            select(message_revisions.c.id).where(
                message_revisions.c.message_id == message_id,
                message_revisions.c.revision_no == revision_no,
            )
        )
        assert revision_id is not None
        for item in event.media:
            await self._session.execute(
                insert(message_media).values(
                    id=self._new_uuid(),
                    account_id=account_id,
                    message_revision_id=revision_id,
                    media_kind=item.kind,
                    position=item.position,
                    telegram_file_ref=item.telegram_file_ref,
                    declared_mime=item.declared_mime,
                    declared_size=item.declared_size,
                    duration_ms=item.duration_ms,
                    original_name_sanitized=item.original_name_sanitized,
                    metadata_schema_version=1,
                    metadata=item.metadata,
                )
            )

    async def _update_message(
        self, event_id: int, event: NormalizedTelegramEvent, row: RowMapping
    ) -> TelegramIngestResult:
        message_id = cast(UUID, row["id"])
        current_revision = int(row["current_revision_no"])
        effective_source = str(row["source"])
        if bool(row["is_tombstone"]):
            await self._session.execute(
                update(messages)
                .where(messages.c.id == message_id)
                .values(last_observed_at=event.observed_at)
            )
            return TelegramIngestResult(
                event_id, False, True, message_id, current_revision, effective_source
            )
        if (
            effective_source == "system_pending"
            and event.direction is Direction.OUTGOING
            and event.event_kind is not EventKind.MESSAGE_DELETED
        ):
            corrected_source, _, intent_id = await self._classify_source(event)
            if intent_id is not None:
                effective_source = corrected_source
                await self._session.execute(
                    update(messages)
                    .where(messages.c.id == message_id)
                    .values(
                        source=corrected_source,
                        source_status="corrected",
                        last_observed_at=event.observed_at,
                    )
                )
                assert event.telegram_message_id is not None
                await self._reconcile_intent(
                    intent_id, event.telegram_message_id, event.observed_at
                )
                await self._advance_conversation(event)
        if event.event_kind is EventKind.MESSAGE_DELETED:
            await self._session.execute(
                update(message_revisions)
                .where(
                    message_revisions.c.message_id == message_id,
                    message_revisions.c.redacted_at.is_(None),
                )
                .values(
                    text_content=None,
                    caption=None,
                    entities=null(),
                    content_sha256=None,
                    redacted_at=event.observed_at,
                    redaction_reason="telegram_delete",
                )
            )
            await self._session.execute(
                update(messages)
                .where(messages.c.id == message_id)
                .values(
                    is_tombstone=True,
                    deleted_at=event.observed_at,
                    last_observed_at=event.observed_at,
                )
            )
            await self._advance_conversation(event)
            await self._queue_delete_reconciliation(event, message_id)
            return TelegramIngestResult(
                event_id, False, True, message_id, current_revision, effective_source
            )

        if event.event_kind is EventKind.MESSAGE_CREATED and row["edited_at"] is not None:
            return TelegramIngestResult(
                event_id, False, True, message_id, current_revision, effective_source
            )
        event_time = event.telegram_event_at or event.observed_at
        if (
            event.event_kind is EventKind.MESSAGE_EDITED
            and row["edited_at"] is not None
            and event_time <= cast(datetime, row["edited_at"])
        ):
            return TelegramIngestResult(
                event_id, False, True, message_id, current_revision, effective_source
            )
        current_hash = await self._session.scalar(
            select(message_revisions.c.content_sha256).where(
                message_revisions.c.message_id == message_id,
                message_revisions.c.revision_no == current_revision,
            )
        )
        next_hash = event.body.content_sha256 if event.body is not None else None
        if current_hash == next_hash:
            await self._session.execute(
                update(messages)
                .where(messages.c.id == message_id)
                .values(last_observed_at=event.observed_at)
            )
            return TelegramIngestResult(
                event_id, False, True, message_id, current_revision, effective_source
            )
        revision_no = current_revision + 1
        await self._insert_revision(message_id, revision_no, event_id, event, event.body)
        if event.body is not None:
            await self._insert_media(message_id, event.account_id, revision_no, event)
        await self._session.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(
                current_revision_no=revision_no,
                grouped_id=event.grouped_id,
                edited_at=(event_time if event.event_kind is EventKind.MESSAGE_EDITED else None),
                last_observed_at=event.observed_at,
            )
        )
        await self._advance_conversation(event)
        return TelegramIngestResult(
            event_id, False, True, message_id, revision_no, effective_source
        )

    async def _advance_conversation(
        self, event: NormalizedTelegramEvent, *, human_takeover: bool = False
    ) -> None:
        if event.conversation_id is None:
            return
        values: dict[str, Any] = {
            "content_revision": conversations.c.content_revision + 1,
            "updated_at": event.observed_at,
        }
        if human_takeover:
            values["mode_version"] = conversations.c.mode_version + 1
        await self._session.execute(
            update(conversations)
            .where(
                conversations.c.id == event.conversation_id,
                conversations.c.account_id == event.account_id,
            )
            .values(**values)
        )

    async def _queue_delete_reconciliation(
        self, event: NormalizedTelegramEvent, message_id: UUID
    ) -> None:
        key = sha256(
            f"memory-reconcile-delete-v1:{event.account_id}:{message_id}".encode()
        ).digest()
        await DurableJobRepository(self._session).create(
            NewJobRecord(
                id=self._new_uuid(),
                account_id=event.account_id,
                queue_name="memory",
                job_type="memory.reconcile_message_delete",
                idempotency_key=key,
                payload={"message_id": str(message_id)},
                available_at=event.observed_at,
            )
        )

    async def _project_reaction(
        self, event_id: int, event: NormalizedTelegramEvent
    ) -> TelegramIngestResult:
        assert event.conversation_id is not None
        assert event.telegram_message_id is not None
        assert event.reaction is not None
        message_id = await self._session.scalar(
            select(messages.c.id).where(
                messages.c.account_id == event.account_id,
                messages.c.conversation_id == event.conversation_id,
                messages.c.telegram_message_id == event.telegram_message_id,
            )
        )
        if message_id is None:
            return TelegramIngestResult(event_id, False, True)
        actor_peer_id = None
        if event.reaction.actor_telegram_peer_id is not None:
            actor_peer_id = await self._session.scalar(
                select(telegram_peers.c.id).where(
                    telegram_peers.c.telegram_peer_id == event.reaction.actor_telegram_peer_id
                )
            )
        existing = await self._session.scalar(
            select(message_reactions.c.id).where(
                message_reactions.c.message_id == message_id,
                message_reactions.c.actor_peer_id.is_(actor_peer_id)
                if actor_peer_id is None
                else message_reactions.c.actor_peer_id == actor_peer_id,
                message_reactions.c.reaction_key == event.reaction.key,
            )
        )
        if existing is None:
            await self._session.execute(
                insert(message_reactions).values(
                    id=self._new_uuid(),
                    account_id=event.account_id,
                    message_id=message_id,
                    actor_peer_id=actor_peer_id,
                    reaction_key=event.reaction.key,
                    active=event.reaction.active,
                    updated_at=event.observed_at,
                )
            )
        else:
            await self._session.execute(
                update(message_reactions)
                .where(message_reactions.c.id == existing)
                .values(active=event.reaction.active, updated_at=event.observed_at)
            )
        return TelegramIngestResult(event_id, False, True, cast(UUID, message_id))

    async def create_delivery_group(
        self,
        *,
        group: NewDeliveryGroupRecord,
        chunks: Sequence[OutboundChunk],
    ) -> UUID:
        if not chunks:
            raise ValueError("delivery group requires at least one chunk")
        if tuple(chunk.sequence_no for chunk in chunks) != tuple(range(len(chunks))):
            raise ValueError("delivery chunks must use contiguous zero-based sequence numbers")
        statement = (
            postgresql_insert(outbound_delivery_groups)
            .values(
                id=group.id,
                account_id=group.account_id,
                conversation_id=group.conversation_id,
                turn_id=group.turn_id,
                model_run_id=group.model_run_id,
                model_role=group.model_role,
                copilot_draft_id=group.copilot_draft_id,
                approved_draft_revision_id=group.approved_draft_revision_id,
                proactive_decision_id=group.proactive_decision_id,
                source=group.source,
                generation_no=group.generation_no,
                state=DeliveryGroupState.PLANNED,
                intent_count=len(chunks),
                idempotency_key=group.idempotency_key,
                mode_version=group.mode_version,
                content_revision=group.content_revision,
                account_control_version=group.account_control_version,
                logical_content_sha256=group.logical_content_sha256,
                normalizer_version=group.normalizer_version,
                splitter_version=group.splitter_version,
                max_delivery_chunks=group.max_delivery_chunks,
                send_authorized_at=group.send_authorized_at,
                created_at=group.created_at,
                updated_at=group.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_outbound_delivery_groups_idempotency")
            .returning(outbound_delivery_groups.c.id)
        )
        inserted_id = await self._session.scalar(statement)
        if inserted_id is None:
            existing = await self._session.scalar(
                select(outbound_delivery_groups.c.id).where(
                    outbound_delivery_groups.c.account_id == group.account_id,
                    outbound_delivery_groups.c.idempotency_key == group.idempotency_key,
                )
            )
            if existing is None:
                raise RuntimeError("idempotent delivery group disappeared")
            return cast(UUID, existing)
        for chunk in chunks:
            if chunk.payload_sha256 != sha256(chunk.text.encode()).digest():
                raise ValueError("outbound payload hash mismatch")
            await self._session.execute(
                insert(outbound_intents).values(
                    id=chunk.intent_id,
                    delivery_group_id=group.id,
                    account_id=group.account_id,
                    conversation_id=group.conversation_id,
                    turn_id=group.turn_id,
                    model_run_id=group.model_run_id,
                    model_role=group.model_role,
                    source=group.source,
                    generation_no=group.generation_no,
                    account_control_version=group.account_control_version,
                    mode_version=group.mode_version,
                    content_revision=group.content_revision,
                    idempotency_key=sha256(
                        group.id.bytes + chunk.sequence_no.to_bytes(4, "big") + chunk.payload_sha256
                    ).digest(),
                    sequence_no=chunk.sequence_no,
                    chunk_count=len(chunks),
                    telegram_random_id=chunk.telegram_random_id,
                    text_content=chunk.text,
                    payload_sha256=chunk.payload_sha256,
                    state=OutboundIntentState.PENDING,
                    created_at=group.created_at,
                    updated_at=group.created_at,
                )
            )
        return group.id

    async def claim_intent(self, *, intent_id: UUID, now: datetime) -> OutboundIntentRecord | None:
        row = (
            (
                await self._session.execute(
                    select(outbound_intents)
                    .where(
                        outbound_intents.c.id == intent_id,
                        outbound_intents.c.state.in_(("pending", "retry_wait")),
                        or_(
                            outbound_intents.c.next_attempt_at.is_(None),
                            outbound_intents.c.next_attempt_at <= now,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        updated = (
            (
                await self._session.execute(
                    update(outbound_intents)
                    .where(outbound_intents.c.id == intent_id)
                    .values(
                        state=OutboundIntentState.SENDING,
                        attempt_count=outbound_intents.c.attempt_count + 1,
                        next_attempt_at=None,
                        last_error_code=None,
                        updated_at=now,
                    )
                    .returning(*outbound_intents.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.execute(
            insert(outbound_attempts).values(
                intent_id=intent_id,
                account_id=updated["account_id"],
                attempt_no=updated["attempt_count"],
                state="started",
                started_at=now,
            )
        )
        await self._session.execute(
            update(outbound_delivery_groups)
            .where(outbound_delivery_groups.c.id == row["delivery_group_id"])
            .values(state=DeliveryGroupState.SENDING, updated_at=now)
        )
        return _intent(updated)

    async def finish_attempt(
        self,
        *,
        intent: OutboundIntentRecord,
        completion: AttemptCompletionRecord,
    ) -> None:
        outcome = AttemptOutcome(completion.outcome)
        if outcome is AttemptOutcome.SUCCEEDED and completion.telegram_message_id is None:
            raise ValueError("successful attempt requires Telegram message id")
        await self._session.execute(
            update(outbound_attempts)
            .where(
                outbound_attempts.c.intent_id == intent.id,
                outbound_attempts.c.attempt_no == intent.attempt_count,
                outbound_attempts.c.state == "started",
            )
            .values(
                state=outcome,
                error_code=completion.error_code,
                retry_after_seconds=completion.retry_after_seconds,
                finished_at=completion.finished_at,
            )
        )
        states = {
            AttemptOutcome.SUCCEEDED: OutboundIntentState.SENT,
            AttemptOutcome.FLOOD_WAIT: OutboundIntentState.RETRY_WAIT,
            AttemptOutcome.TRANSIENT: OutboundIntentState.RETRY_WAIT,
            AttemptOutcome.PERMANENT: OutboundIntentState.FAILED,
            AttemptOutcome.UNKNOWN: OutboundIntentState.UNKNOWN,
        }
        values: dict[str, Any] = {
            "state": states[outcome],
            "last_error_code": completion.error_code,
            "updated_at": completion.finished_at,
            "next_attempt_at": completion.next_attempt_at,
            "unknown_since": (
                completion.finished_at if outcome is AttemptOutcome.UNKNOWN else None
            ),
        }
        if outcome is AttemptOutcome.SUCCEEDED:
            values.update(
                telegram_message_id=completion.telegram_message_id,
                sent_at=completion.finished_at,
            )
        await self._session.execute(
            update(outbound_intents).where(outbound_intents.c.id == intent.id).values(**values)
        )
        await self._refresh_group(intent.delivery_group_id, completion.finished_at)

    async def _reconcile_intent(
        self, intent_id: UUID, telegram_message_id: int, now: datetime
    ) -> None:
        group_id = await self._session.scalar(
            update(outbound_intents)
            .where(outbound_intents.c.id == intent_id)
            .values(
                state=OutboundIntentState.SENT,
                telegram_message_id=telegram_message_id,
                sent_at=now,
                updated_at=now,
                unknown_since=None,
                last_error_code=None,
            )
            .returning(outbound_intents.c.delivery_group_id)
        )
        if group_id is not None:
            await self._refresh_group(cast(UUID, group_id), now)

    async def _refresh_group(self, group_id: UUID, now: datetime) -> None:
        states = tuple(
            (
                await self._session.execute(
                    select(outbound_intents.c.state)
                    .where(outbound_intents.c.delivery_group_id == group_id)
                    .order_by(outbound_intents.c.sequence_no)
                )
            ).scalars()
        )
        sent_count = states.count(OutboundIntentState.SENT)
        if sent_count == len(states):
            state = DeliveryGroupState.SENT
        elif OutboundIntentState.UNKNOWN in states:
            state = DeliveryGroupState.UNKNOWN
        elif OutboundIntentState.FAILED in states:
            state = DeliveryGroupState.PARTIAL if sent_count else DeliveryGroupState.FAILED
        elif sent_count:
            state = DeliveryGroupState.PARTIAL
        else:
            state = DeliveryGroupState.SENDING
        await self._session.execute(
            update(outbound_delivery_groups)
            .where(outbound_delivery_groups.c.id == group_id)
            .values(
                state=state,
                sent_count=sent_count,
                updated_at=now,
                completed_at=now if state is DeliveryGroupState.SENT else None,
            )
        )

    async def recover_stale_sending(self, *, older_than: datetime, now: datetime) -> int:
        rows = (
            await self._session.execute(
                update(outbound_intents)
                .where(
                    outbound_intents.c.state == OutboundIntentState.SENDING,
                    outbound_intents.c.updated_at <= older_than,
                )
                .values(
                    state=OutboundIntentState.UNKNOWN,
                    unknown_since=now,
                    last_error_code="crash_after_dispatch",
                    updated_at=now,
                )
                .returning(outbound_intents.c.id, outbound_intents.c.delivery_group_id)
            )
        ).all()
        intent_ids = tuple(row.id for row in rows)
        group_ids = {row.delivery_group_id for row in rows}
        if intent_ids:
            await self._session.execute(
                update(outbound_attempts)
                .where(
                    outbound_attempts.c.intent_id.in_(intent_ids),
                    outbound_attempts.c.state == "started",
                )
                .values(
                    state=AttemptOutcome.UNKNOWN,
                    error_code="crash_after_dispatch",
                    finished_at=now,
                )
            )
        for group_id in group_ids:
            await self._refresh_group(group_id, now)
        return len(intent_ids)

    async def record_read_high_watermark(
        self,
        *,
        record: ReadHighWatermarkRecord,
    ) -> bool:
        inserted = await self._session.scalar(
            postgresql_insert(telegram_operations)
            .values(
                id=record.operation_id,
                account_id=record.account_id,
                conversation_id=record.conversation_id,
                operation_kind="read",
                idempotency_key=record.idempotency_key,
                max_telegram_message_id=record.max_telegram_message_id,
                state="succeeded",
                created_at=record.completed_at,
                completed_at=record.completed_at,
            )
            .on_conflict_do_nothing(constraint="uq_telegram_operations_idempotency")
            .returning(telegram_operations.c.id)
        )
        if inserted is None:
            return False
        await self._session.execute(
            postgresql_insert(telegram_read_states)
            .values(
                account_id=record.account_id,
                conversation_id=record.conversation_id,
                max_telegram_message_id=record.max_telegram_message_id,
                updated_at=record.completed_at,
            )
            .on_conflict_do_update(
                constraint="pk_telegram_read_states",
                set_={
                    "max_telegram_message_id": record.max_telegram_message_id,
                    "updated_at": record.completed_at,
                },
                where=telegram_read_states.c.max_telegram_message_id
                < record.max_telegram_message_id,
            )
        )
        return True

    async def set_typing_lease(
        self,
        *,
        record: TypingLeaseRecord,
    ) -> None:
        active = record.lease_token is not None
        if active != (record.lease_expires_at is not None):
            raise ValueError("typing lease token and expiry must be set together")
        await self._session.execute(
            postgresql_insert(telegram_typing_states)
            .values(
                account_id=record.account_id,
                conversation_id=record.conversation_id,
                active=active,
                lease_token=record.lease_token,
                lease_expires_at=record.lease_expires_at,
                updated_at=record.updated_at,
            )
            .on_conflict_do_update(
                constraint="pk_telegram_typing_states",
                set_={
                    "active": active,
                    "lease_token": record.lease_token,
                    "lease_expires_at": record.lease_expires_at,
                    "version": telegram_typing_states.c.version + 1,
                    "updated_at": record.updated_at,
                },
            )
        )

    async def get_intent(self, intent_id: UUID) -> OutboundIntentRecord | None:
        row = (
            (
                await self._session.execute(
                    select(outbound_intents).where(outbound_intents.c.id == intent_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _intent(row)
