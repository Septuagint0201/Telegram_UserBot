"""M4 runtime sequencing with every external call outside database transactions."""

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.orchestrator_records import GenerationClaim, RunResult
from telegram_userbot.adapters.persistence.orchestrator_repository import (
    ConversationOrchestratorRepository,
    OrchestratorConflictError,
)
from telegram_userbot.adapters.persistence.records import (
    ReadHighWatermarkRecord,
    TelegramIngestResult,
    TypingLeaseRecord,
)
from telegram_userbot.adapters.persistence.schema import outbound_intents
from telegram_userbot.adapters.persistence.telegram_delivery import TelegramDeliveryService
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.application.ports.model import ModelGateway, ModelRequest, ModelResponse
from telegram_userbot.application.ports.telegram import (
    TelegramGateway,
    TelegramReadRequest,
    TelegramTypingAction,
    TelegramTypingRequest,
)
from telegram_userbot.domain.messaging import Direction, EventKind, NormalizedTelegramEvent
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, RunId


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryReport:
    expired_generations: int
    stale_outbound_intents: int


class OrchestratedTelegramIngestService:
    """Project one event and apply its M4 invalidation/collection effect atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        new_uuid: Callable[[], UUID],
    ) -> None:
        self._session_factory = session_factory
        self._new_uuid = new_uuid

    async def ingest(self, event: NormalizedTelegramEvent) -> TelegramIngestResult:
        async with self._session_factory() as session, session.begin():
            orchestrator = ConversationOrchestratorRepository(session, new_uuid=self._new_uuid)
            if event.conversation_id is not None:
                # Acquire account/conversation orchestration locks before M3 updates the
                # conversation revision. This preserves the documented lock order.
                await orchestrator.resolve(event.conversation_id, event.observed_at)
            lifecycle = TelegramLifecycleRepository(session, new_uuid=self._new_uuid)
            result = await lifecycle.ingest(event)
            if result.duplicate or not result.projected or event.conversation_id is None:
                return result
            if (
                event.event_kind is EventKind.MESSAGE_CREATED
                and event.direction is Direction.INCOMING
                and result.message_id is not None
            ):
                await orchestrator.handle_new_incoming(
                    conversation_id=event.conversation_id,
                    message_id=result.message_id,
                    observed_at=event.observed_at,
                )
            elif (
                event.event_kind is EventKind.MESSAGE_CREATED
                and event.direction is Direction.OUTGOING
                and result.source == "human"
            ):
                await orchestrator.human_takeover_after_ingest(
                    conversation_id=event.conversation_id,
                    now=event.observed_at,
                    actor_ref=f"telegram_message:{event.telegram_message_id}",
                    human_event_id=result.event_id,
                )
            elif event.event_kind in {EventKind.MESSAGE_EDITED, EventKind.MESSAGE_DELETED}:
                await orchestrator.invalidate_after_content_change(
                    conversation_id=event.conversation_id,
                    now=event.observed_at,
                    reason=(
                        "MESSAGE_EDITED"
                        if event.event_kind is EventKind.MESSAGE_EDITED
                        else "MESSAGE_DELETED"
                    ),
                )
            if (
                event.direction is Direction.OUTGOING
                and result.source in {"ai", "copilot_approved", "proactive_ai"}
                and event.telegram_message_id is not None
            ):
                await orchestrator.reconcile_completed_delivery(
                    conversation_id=event.conversation_id,
                    telegram_message_id=event.telegram_message_id,
                    now=event.observed_at,
                )
            return result


class ConversationRuntimeService:
    """Drive one sealed turn through fake/provider and fake/Telegram ports."""

    def __init__(  # noqa: PLR0913 - runtime dependencies are explicit and injectable
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model: ModelGateway,
        telegram: TelegramGateway,
        new_uuid: Callable[[], UUID],
        now: Callable[[], datetime] | None = None,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
        max_model_attempts: int = 2,
        max_generation_seconds: int = 60,
        outbound_lease_seconds: int = 60,
    ) -> None:
        if max_model_attempts < 1:
            raise ValueError("max_model_attempts must be positive")
        if max_generation_seconds < 1 or outbound_lease_seconds < 1:
            raise ValueError("runtime lease and deadline values must be positive")
        self._session_factory = session_factory
        self._model = model
        self._telegram = telegram
        self._new_uuid = new_uuid
        self._now = now or (lambda: datetime.now(UTC))
        self._entropy = entropy
        self._max_model_attempts = max_model_attempts
        self._max_generation_seconds = max_generation_seconds
        self._outbound_lease_seconds = outbound_lease_seconds

    async def run_due_turn(self, *, turn_id: UUID, owner: UUID) -> RunResult:
        async with self._session_factory() as session, session.begin():
            repository = ConversationOrchestratorRepository(session, new_uuid=self._new_uuid)
            await repository.seal_turn(turn_id=turn_id, now=self._now())
            claim = await repository.start_generation(turn_id=turn_id, owner=owner, now=self._now())
        typing_started = claim.typing_lease_token is not None
        if claim.typing_lease_token is not None:
            # Read acknowledgement and typing are UX feedback, never a
            # prerequisite for a durable Main AI result.
            with suppress(Exception):
                await self._start_auto_feedback(claim, claim.typing_lease_token)
        invalidated = asyncio.Event()
        try:
            loop = asyncio.get_running_loop()
            deadline_seconds = float(self._max_generation_seconds)
            deadline = loop.time() + deadline_seconds
            error_code = "PROVIDER_TIMEOUT"
            for attempt_no in range(1, self._max_model_attempts + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                model_task = asyncio.create_task(
                    self._model.generate(
                        ModelRequest(
                            RunId(claim.run.id),
                            "main_ai",
                            claim.run.input_fingerprint.hex(),
                        )
                    )
                )
                monitor_task = asyncio.create_task(
                    self._maintain_generation(
                        claim=claim,
                        owner=owner,
                        model_task=model_task,
                        invalidated=invalidated,
                    )
                )
                try:
                    response = await asyncio.wait_for(model_task, timeout=remaining)
                except asyncio.CancelledError:
                    if not invalidated.is_set():
                        raise
                    async with self._session_factory() as session, session.begin():
                        return await ConversationOrchestratorRepository(
                            session, new_uuid=self._new_uuid
                        ).complete_generation(
                            run_id=claim.run.id,
                            owner=owner,
                            text_output="stale generation discarded",
                            completed_at=self._now(),
                            entropy=self._entropy(32),
                        )
                except Exception as error:
                    error_code = (
                        "PROVIDER_TIMEOUT" if isinstance(error, TimeoutError) else "PROVIDER_ERROR"
                    )
                    can_retry = (
                        attempt_no < self._max_model_attempts
                        and self._is_retryable_model_error(error)
                        and deadline - loop.time() > 0
                    )
                    if can_retry and await self._record_retry(
                        run_id=claim.run.id,
                        owner=owner,
                        error_code=error_code,
                    ):
                        continue
                    break
                else:
                    async with self._session_factory() as session, session.begin():
                        result = await ConversationOrchestratorRepository(
                            session, new_uuid=self._new_uuid
                        ).complete_generation(
                            run_id=claim.run.id,
                            owner=owner,
                            text_output=response.text.reveal_for_use(),
                            completed_at=self._now(),
                            entropy=self._entropy(32),
                        )
                    if result.delivery_group_id is not None:
                        await self.dispatch_group(
                            group_id=result.delivery_group_id,
                            owner=owner,
                        )
                    return result
                finally:
                    monitor_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor_task
            async with self._session_factory() as session, session.begin():
                await ConversationOrchestratorRepository(
                    session, new_uuid=self._new_uuid
                ).fail_generation(
                    run_id=claim.run.id,
                    owner=owner,
                    now=self._now(),
                    error_code=error_code,
                )
            return RunResult(claim.run.id, "failed", error_code)
        finally:
            if typing_started:
                await self._best_effort_stop_typing(claim.run.account_id, claim.run.conversation_id)

    @staticmethod
    def _is_retryable_model_error(error: Exception) -> bool:
        return isinstance(error, TimeoutError | ConnectionError) or bool(
            getattr(error, "retryable", False)
        )

    async def _record_retry(self, *, run_id: UUID, owner: UUID, error_code: str) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = ConversationOrchestratorRepository(session, new_uuid=self._new_uuid)
            retry = getattr(repository, "retry_generation_attempt", None)
            if retry is None:
                # Lightweight fakes from M0-M4 predate durable attempt history.
                return True
            return bool(
                await retry(run_id=run_id, owner=owner, now=self._now(), error_code=error_code)
            )

    async def _maintain_generation(
        self,
        *,
        claim: GenerationClaim,
        owner: UUID,
        model_task: asyncio.Task[ModelResponse],
        invalidated: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(4)
            now = self._now()
            try:
                async with self._session_factory() as session, session.begin():
                    renewed = await ConversationOrchestratorRepository(
                        session, new_uuid=self._new_uuid
                    ).renew_generation_lease(
                        run_id=claim.run.id,
                        owner=owner,
                        now=now,
                    )
            except Exception:
                return
            if not renewed:
                invalidated.set()
                model_task.cancel()
                return
            if claim.typing_lease_token is not None:
                try:
                    await self._telegram.set_typing(
                        TelegramTypingRequest(
                            AccountId(claim.run.account_id),
                            ConversationId(claim.run.conversation_id),
                            TelegramTypingAction.REFRESH,
                        )
                    )
                    async with self._session_factory() as session, session.begin():
                        await TelegramLifecycleRepository(
                            session, new_uuid=self._new_uuid
                        ).set_typing_lease(
                            record=TypingLeaseRecord(
                                claim.run.account_id,
                                claim.run.conversation_id,
                                claim.typing_lease_token,
                                now + timedelta(seconds=10),
                                now,
                            )
                        )
                except Exception:
                    return

    async def recover_once(self) -> RuntimeRecoveryReport:
        """Recover only durable state; no model or Telegram call is made here."""

        now = self._now()
        async with self._session_factory() as session, session.begin():
            expired_generations = await ConversationOrchestratorRepository(
                session, new_uuid=self._new_uuid
            ).recover_expired_generations(now=now)
        async with self._session_factory() as session, session.begin():
            stale_outbound_intents = await TelegramLifecycleRepository(
                session, new_uuid=self._new_uuid
            ).recover_stale_sending(
                older_than=now - timedelta(seconds=self._outbound_lease_seconds),
                now=now,
            )
        return RuntimeRecoveryReport(expired_generations, stale_outbound_intents)

    async def _start_auto_feedback(self, claim: GenerationClaim, lease_token: UUID) -> None:
        run = claim.run
        max_message_id = claim.max_telegram_message_id
        if max_message_id is not None:
            with suppress(Exception):
                receipt = await self._telegram.acknowledge_read(
                    TelegramReadRequest(
                        AccountId(run.account_id),
                        ConversationId(run.conversation_id),
                        max_message_id,
                    )
                )
                now = self._now()
                async with self._session_factory() as session, session.begin():
                    await TelegramLifecycleRepository(
                        session, new_uuid=self._new_uuid
                    ).record_read_high_watermark(
                        record=ReadHighWatermarkRecord(
                            self._new_uuid(),
                            run.account_id,
                            run.conversation_id,
                            receipt.max_telegram_message_id,
                            sha256(
                                f"m4-read-v1:{run.id}:{receipt.max_telegram_message_id}".encode()
                            ).digest(),
                            now,
                        )
                    )
        await self._telegram.set_typing(
            TelegramTypingRequest(
                AccountId(run.account_id),
                ConversationId(run.conversation_id),
                TelegramTypingAction.START,
            )
        )
        now = self._now()
        async with self._session_factory() as session, session.begin():
            await TelegramLifecycleRepository(session, new_uuid=self._new_uuid).set_typing_lease(
                record=TypingLeaseRecord(
                    run.account_id,
                    run.conversation_id,
                    lease_token,
                    now + timedelta(seconds=10),
                    now,
                )
            )

    async def _stop_typing(self, account_id: UUID, conversation_id: UUID) -> None:
        await self._telegram.set_typing(
            TelegramTypingRequest(
                AccountId(account_id),
                ConversationId(conversation_id),
                TelegramTypingAction.STOP,
            )
        )
        async with self._session_factory() as session, session.begin():
            await TelegramLifecycleRepository(session, new_uuid=self._new_uuid).set_typing_lease(
                record=TypingLeaseRecord(
                    account_id,
                    conversation_id,
                    None,
                    None,
                    self._now(),
                )
            )

    async def _best_effort_stop_typing(self, account_id: UUID, conversation_id: UUID) -> None:
        try:
            await self._stop_typing(account_id, conversation_id)
        except Exception:
            return

    async def dispatch_group(self, *, group_id: UUID, owner: UUID) -> int:
        async with self._session_factory() as session:
            intent_ids = tuple(
                (
                    await session.execute(
                        select(outbound_intents.c.id)
                        .where(outbound_intents.c.delivery_group_id == group_id)
                        .order_by(outbound_intents.c.sequence_no)
                    )
                ).scalars()
            )
        sent = 0
        delivery = TelegramDeliveryService(self._telegram)
        for intent_id in intent_ids:
            async with self._session_factory() as session, session.begin():
                intent = await ConversationOrchestratorRepository(
                    session, new_uuid=self._new_uuid
                ).preflight_intent(intent_id=intent_id, owner=owner, now=self._now())
            if intent is None:
                break
            completion = await delivery.send_prepared(intent=intent, now=self._now())
            # The gateway call can outlive the send lease.  Fence persistence
            # against the time it returned, not the time dispatch began.
            completion = replace(completion, finished_at=self._now())
            async with self._session_factory() as session, session.begin():
                completed = await TelegramLifecycleRepository(
                    session, new_uuid=self._new_uuid
                ).finish_attempt(intent=intent, completion=completion)
            if not completed:
                break
            if completion.outcome == "succeeded":
                sent += 1
            else:
                break
        return sent


__all__ = [
    "ConversationRuntimeService",
    "OrchestratedTelegramIngestService",
    "OrchestratorConflictError",
    "RuntimeRecoveryReport",
]
