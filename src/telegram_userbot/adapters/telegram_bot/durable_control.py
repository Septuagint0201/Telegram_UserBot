"""PostgreSQL receipt/offset composition for Control Bot updates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.runtime_cursors import RuntimeCursorRepository
from telegram_userbot.adapters.persistence.schema import control_bot_update_receipts
from telegram_userbot.adapters.persistence.service_status import RestoreGateRepository
from telegram_userbot.adapters.telegram_bot.dispatcher import (
    ControlBotDispatcher,
    DispatchOutcome,
    PreparedDispatch,
    UpdateDisposition,
)
from telegram_userbot.adapters.telegram_bot.http import BotAPIError, BotMutationState
from telegram_userbot.platform.health.status import RestoreGateState
from telegram_userbot.platform.runtime.cursors import (
    ControlUpdateClaim,
    ControlUpdateClaimOutcome,
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
)

_MAX_TELEGRAM_UPDATE_ID = 2**63 - 2


class DispatcherFactory(Protocol):
    """Build controllers against the update's transaction-owned session."""

    def __call__(self, session: AsyncSession) -> ControlBotDispatcher: ...


class PostgresBotOffsetStore:
    """Expose the content-free cursor through the poller's CAS protocol."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        deployment_id: str,
        bot_user_id: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._deployment_id = deployment_id
        self._bot_user_id = bot_user_id
        self._now = now or (lambda: datetime.now(UTC))

    async def load_next_offset(self) -> int:
        async with self._sessions() as session, session.begin():
            cursor = await RuntimeCursorRepository(session).get_or_create_control_cursor(
                deployment_id=self._deployment_id,
                bot_user_id=self._bot_user_id,
                initial_offset=0,
                now=_now(self._now),
            )
            return cursor.next_offset

    async def commit_next_offset(self, *, expected: int, replacement: int) -> bool:
        if (
            type(expected) is not int
            or expected < 0
            or type(replacement) is not int
            or replacement <= expected
        ):
            raise BotAPIError("BOT_OFFSET_INVALID")
        async with self._sessions() as session, session.begin():
            await self._require_restore_gate(session)
            repository = RuntimeCursorRepository(session)
            cursor = await repository.load_control_cursor(
                deployment_id=self._deployment_id,
                bot_user_id=self._bot_user_id,
            )
            if cursor is None or cursor.next_offset != expected:
                return False
            advanced = await repository.advance_control_offset(
                deployment_id=self._deployment_id,
                bot_user_id=self._bot_user_id,
                through_update_id=replacement - 1,
                expected_version=cursor.version,
                now=_now(self._now),
            )
            return advanced is not None and advanced.next_offset == replacement

    async def _require_restore_gate(self, session: AsyncSession) -> None:
        gate = await RestoreGateRepository(session).get(self._deployment_id)
        if gate is None or gate.state is not RestoreGateState.OPEN:
            raise BotAPIError("BOT_RESTORE_GATE_CLOSED")


class DurableControlUpdateExecutor:
    """Execute commands once, commit them, and only then perform Bot mutations.

    Raw Telegram updates and reply bodies remain process-local.  The database
    stores only the update identity, disposition, lease, and coarse send state.
    """

    def __init__(  # noqa: PLR0913 - every durable identity/fence is explicit
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher_factory: DispatcherFactory,
        deployment_id: str,
        bot_user_id: int,
        owner_instance_id: UUID,
        now: Callable[[], datetime] | None = None,
        claim_lease: timedelta = timedelta(minutes=2),
    ) -> None:
        if not isinstance(owner_instance_id, UUID) or owner_instance_id.int == 0:
            raise ValueError("Control Bot owner instance id is invalid")
        if claim_lease <= timedelta(seconds=10) or claim_lease > timedelta(minutes=10):
            raise ValueError("Control Bot claim lease is invalid")
        self._sessions = sessions
        self._dispatcher_factory = dispatcher_factory
        self._deployment_id = deployment_id
        self._bot_user_id = bot_user_id
        self._owner_instance_id = owner_instance_id
        self._now = now or (lambda: datetime.now(UTC))
        self._claim_lease = claim_lease

    async def recover_pending_responses(self, *, batch_size: int = 100) -> int:
        """Conservatively terminalize replies whose ephemeral plan cannot be replayed.

        A completed update's response body/token is intentionally never persisted.
        Therefore a process that observes ``pending`` after the delivery boundary
        cannot safely resend it and records ``unknown`` using the receipt CAS.
        """

        if type(batch_size) is not int or not 1 <= batch_size <= 1_000:
            raise ValueError("Control Bot recovery batch size is invalid")
        # One bounded pass avoids selecting the same CAS-conflicted row forever.
        # The poller calls this again before its next batch, so a concurrent
        # owner can finish while stale rows remain eligible for a later pass.
        async with self._sessions() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        select(
                            control_bot_update_receipts.c.update_id,
                            control_bot_update_receipts.c.version,
                        )
                        .where(
                            control_bot_update_receipts.c.deployment_id == self._deployment_id,
                            control_bot_update_receipts.c.bot_user_id == self._bot_user_id,
                            control_bot_update_receipts.c.state
                            == ControlUpdateState.COMPLETED.value,
                            control_bot_update_receipts.c.send_state
                            == ControlUpdateSendState.PENDING.value,
                        )
                        .order_by(control_bot_update_receipts.c.update_id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            repository = RuntimeCursorRepository(session)
            recovered = 0
            for row in rows:
                marked = await repository.mark_control_response(
                    deployment_id=self._deployment_id,
                    bot_user_id=self._bot_user_id,
                    update_id=int(row["update_id"]),
                    expected_version=int(row["version"]),
                    send_state=ControlUpdateSendState.UNKNOWN,
                )
                if marked is not None:
                    recovered += 1
            return recovered

    async def handle_update(self, *, update_id: int, update: Mapping[str, Any]) -> DispatchOutcome:
        if (
            type(update_id) is not int
            or not 0 <= update_id <= _MAX_TELEGRAM_UPDATE_ID
            or not isinstance(update, Mapping)
        ):
            raise BotAPIError("BOT_UPDATE_INVALID")
        if _requires_transaction_boundary(update):
            return await self._handle_boundary(update_id=update_id, update=update)
        return await self._handle_transactional(update_id=update_id, update=update)

    async def _handle_transactional(
        self, *, update_id: int, update: Mapping[str, Any]
    ) -> DispatchOutcome:
        prepared: PreparedDispatch
        completed: ControlUpdateReceipt
        dispatcher: ControlBotDispatcher
        async with self._sessions() as session, session.begin():
            await self._require_restore_gate(session)
            repository = RuntimeCursorRepository(session)
            claim = await self._claim(repository, update_id=update_id)
            replay = await self._replay_outcome(
                repository, claim.outcome, claim.receipt, update_id=update_id
            )
            if replay is not None:
                return replay
            assert claim.receipt is not None
            dispatcher = self._dispatcher_factory(session)
            prepared = await dispatcher.route_update(update_id=update_id, update=update)
            completed = await self._complete(
                repository,
                receipt=claim.receipt,
                prepared=prepared,
                update_id=update_id,
            )
        return await self._deliver_and_record(
            dispatcher=dispatcher,
            prepared=prepared,
            completed=completed,
            update_id=update_id,
        )

    async def _require_restore_gate(self, session: AsyncSession) -> None:
        gate = await RestoreGateRepository(session).get(self._deployment_id)
        if gate is None or gate.state is not RestoreGateState.OPEN:
            raise BotAPIError("BOT_RESTORE_GATE_CLOSED")

    async def _handle_boundary(
        self, *, update_id: int, update: Mapping[str, Any]
    ) -> DispatchOutcome:
        # Callback backends consume one-time tokens, context preview delivery has
        # its own journal, and model validation performs external provider I/O.
        # Claim first, then let those paths commit their explicit boundaries
        # without an outer transaction spanning a Bot/provider request.
        async with self._sessions() as session, session.begin():
            await self._require_restore_gate(session)
            repository = RuntimeCursorRepository(session)
            claim = await self._claim(repository, update_id=update_id)
            replay = await self._replay_outcome(
                repository, claim.outcome, claim.receipt, update_id=update_id
            )
            if replay is not None:
                return replay
            assert claim.receipt is not None
            claimed = claim.receipt

        async with self._sessions() as callback_session:
            dispatcher = self._dispatcher_factory(callback_session)
            prepared = await dispatcher.route_update(update_id=update_id, update=update)
            await callback_session.commit()

        async with self._sessions() as session, session.begin():
            completed = await self._complete(
                RuntimeCursorRepository(session),
                receipt=claimed,
                prepared=prepared,
                update_id=update_id,
            )
        return await self._deliver_and_record(
            dispatcher=dispatcher,
            prepared=prepared,
            completed=completed,
            update_id=update_id,
        )

    async def _claim(
        self, repository: RuntimeCursorRepository, *, update_id: int
    ) -> ControlUpdateClaim:
        now = _now(self._now)
        await repository.get_or_create_control_cursor(
            deployment_id=self._deployment_id,
            bot_user_id=self._bot_user_id,
            initial_offset=0,
            now=now,
        )
        return await repository.claim_control_update(
            deployment_id=self._deployment_id,
            bot_user_id=self._bot_user_id,
            update_id=update_id,
            owner_instance_id=self._owner_instance_id,
            now=now,
            lease_expires_at=now + self._claim_lease,
        )

    async def _replay_outcome(
        self,
        repository: RuntimeCursorRepository,
        outcome: ControlUpdateClaimOutcome,
        receipt: ControlUpdateReceipt | None,
        *,
        update_id: int,
    ) -> DispatchOutcome | None:
        if outcome is ControlUpdateClaimOutcome.BELOW_OFFSET:
            return DispatchOutcome(UpdateDisposition.IGNORED)
        if outcome is ControlUpdateClaimOutcome.BUSY:
            raise BotAPIError("BOT_UPDATE_BUSY")
        if outcome is ControlUpdateClaimOutcome.ACQUIRED:
            return None
        if outcome is not ControlUpdateClaimOutcome.COMPLETED or receipt is None:
            raise BotAPIError("BOT_RECEIPT_INVALID")
        if receipt.send_state is ControlUpdateSendState.PENDING:
            marked = await repository.mark_control_response(
                deployment_id=self._deployment_id,
                bot_user_id=self._bot_user_id,
                update_id=update_id,
                expected_version=receipt.version,
                send_state=ControlUpdateSendState.UNKNOWN,
            )
            if marked is None:
                raise BotAPIError("BOT_RECEIPT_CONFLICT")
            receipt = marked
        return DispatchOutcome(
            _dispatcher_disposition(receipt.disposition),
            _bot_send_state(receipt.send_state),
        )

    async def _complete(
        self,
        repository: RuntimeCursorRepository,
        *,
        receipt: ControlUpdateReceipt,
        prepared: PreparedDispatch,
        update_id: int,
    ) -> ControlUpdateReceipt:
        completed = await repository.complete_control_update(
            deployment_id=self._deployment_id,
            bot_user_id=self._bot_user_id,
            update_id=update_id,
            owner_instance_id=self._owner_instance_id,
            expected_version=receipt.version,
            disposition=ControlUpdateDisposition(prepared.disposition.value),
            response_required=prepared.response_required,
            now=_now(self._now),
        )
        if completed is None:
            raise BotAPIError("BOT_RECEIPT_CONFLICT")
        return completed

    async def _deliver_and_record(
        self,
        *,
        dispatcher: ControlBotDispatcher,
        prepared: PreparedDispatch,
        completed: ControlUpdateReceipt,
        update_id: int,
    ) -> DispatchOutcome:
        try:
            outcome = await dispatcher.deliver(prepared)
        except asyncio.CancelledError:
            await self._mark_response(
                completed,
                update_id=update_id,
                send_state=ControlUpdateSendState.UNKNOWN,
            )
            raise
        if outcome.send_state is BotMutationState.KNOWN:
            await self._mark_response(
                completed,
                update_id=update_id,
                send_state=ControlUpdateSendState.SENT,
            )
        elif outcome.send_state is BotMutationState.REJECTED:
            await self._mark_response(
                completed,
                update_id=update_id,
                send_state=ControlUpdateSendState.NOT_SENT,
            )
        elif outcome.send_state is BotMutationState.UNKNOWN:
            await self._mark_response(
                completed,
                update_id=update_id,
                send_state=ControlUpdateSendState.UNKNOWN,
            )
        return outcome

    async def _mark_response(
        self,
        receipt: ControlUpdateReceipt,
        *,
        update_id: int,
        send_state: ControlUpdateSendState,
    ) -> None:
        async with self._sessions() as session, session.begin():
            marked = await RuntimeCursorRepository(session).mark_control_response(
                deployment_id=self._deployment_id,
                bot_user_id=self._bot_user_id,
                update_id=update_id,
                expected_version=receipt.version,
                send_state=send_state,
            )
            if marked is None:
                raise BotAPIError("BOT_RECEIPT_CONFLICT")


def _is_callback(update: Mapping[str, Any]) -> bool:
    return "callback_query" in update and "message" not in update


def _requires_transaction_boundary(update: Mapping[str, Any]) -> bool:
    if _is_callback(update):
        return True
    message = update.get("message")
    if not isinstance(message, Mapping) or "callback_query" in update:
        return False
    text = message.get("text")
    if not isinstance(text, str):
        return False
    parts = text.lstrip().split(maxsplit=1)
    if not parts:
        return False
    command = parts[0].split("@", maxsplit=1)[0].casefold()
    return command == "/model_validate"


def _dispatcher_disposition(
    value: ControlUpdateDisposition | None,
) -> UpdateDisposition:
    if value is None:
        raise BotAPIError("BOT_RECEIPT_INVALID")
    return UpdateDisposition(value.value)


def _bot_send_state(value: ControlUpdateSendState) -> BotMutationState | None:
    if value is ControlUpdateSendState.NOT_REQUIRED:
        return None
    if value is ControlUpdateSendState.SENT:
        return BotMutationState.KNOWN
    if value is ControlUpdateSendState.NOT_SENT:
        return BotMutationState.REJECTED
    if value is ControlUpdateSendState.UNKNOWN:
        return BotMutationState.UNKNOWN
    if value is ControlUpdateSendState.PENDING:
        return BotMutationState.REJECTED
    raise BotAPIError("BOT_RECEIPT_INVALID")


def _now(source: Callable[[], datetime]) -> datetime:
    value = source()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BotAPIError("BOT_CLOCK_INVALID")
    return value


__all__ = ["DispatcherFactory", "DurableControlUpdateExecutor", "PostgresBotOffsetStore"]
