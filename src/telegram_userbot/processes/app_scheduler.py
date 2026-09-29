"""Concrete app-side scheduler for Telegram-owned durable work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid7

from sqlalchemy import or_, select

from telegram_userbot.adapters.persistence.schema import (
    conversation_turns,
    outbound_delivery_groups,
)
from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationControlCommandProcessor,
)
from telegram_userbot.platform.config.production import ProductionProcess, ProductionSettings
from telegram_userbot.platform.runtime import TerminationDeadline
from telegram_userbot.processes.conversation_runtime import (
    ConversationRuntimeService,
    OperationAdmissionError,
    OrchestratorConflictError,
)

if TYPE_CHECKING:
    from telegram_userbot.processes.app import AppSchedulerContext

APP_SCHEDULER_POLL_SECONDS = 0.5
APP_RECOVERY_INTERVAL_SECONDS = 30.0
APP_MEDIA_RECOVERY_INTERVAL_SECONDS = 60.0
APP_MEDIA_CLEANUP_INTERVAL_SECONDS = 60.0 * 60.0
APP_MEDIA_CLEANUP_RETRY_SECONDS = 60.0
APP_CONTROL_BATCH = 10


class ProductionAppScheduler:
    """Serialize work that may ultimately invoke Telethon under the app owner.

    IDs are selected in short transactions.  Provider and Telegram calls occur
    only inside ``ConversationRuntimeService`` after those transactions close.
    Unknown sends are intentionally excluded from blind replay.
    """

    def __init__(  # noqa: PLR0913 - deterministic scheduler seams are explicit
        self,
        *,
        account_id: UUID,
        allowed_admin_ids: frozenset[int],
        bot_identity: str,
        new_uuid: Callable[[], UUID] = uuid7,
        now: Callable[[], datetime] | None = None,
        poll_seconds: float = APP_SCHEDULER_POLL_SECONDS,
    ) -> None:
        if (
            not isinstance(account_id, UUID)
            or account_id.int == 0
            or not allowed_admin_ids
            or not bot_identity
            or not 0.05 <= poll_seconds <= 60
        ):
            raise ValueError("app scheduler configuration is invalid")
        self._account_id = account_id
        self._allowed_admin_ids = allowed_admin_ids
        self._bot_identity = bot_identity
        self._new_uuid = new_uuid
        self._now = now or (lambda: datetime.now(UTC))
        self._poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._control_wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._running = False
        self._pending_image_recovery: Callable[[], Awaitable[int]] | None = None
        self._media_cleanup: Callable[[], Awaitable[int]] | None = None
        self._last_recovery_at: datetime | None = None
        self._last_media_recovery_at: datetime | None = None
        self._last_media_cleanup_at: datetime | None = None
        self._last_media_cleanup_attempt_at: datetime | None = None

    def bind_pending_image_recovery(self, callback: Callable[[], Awaitable[int]]) -> None:
        """Bind the app-owned Telethon/media compensation scan exactly once."""

        if self._running or self._pending_image_recovery is not None or not callable(callback):
            raise RuntimeError("app media recovery binding is invalid")
        self._pending_image_recovery = callback

    def bind_media_cleanup(self, callback: Callable[[], Awaitable[int]]) -> None:
        """Bind the app-owned filesystem retention loop exactly once."""

        if self._running or self._media_cleanup is not None or not callable(callback):
            raise RuntimeError("app media cleanup binding is invalid")
        self._media_cleanup = callback

    def ready(self) -> bool:
        return self._running and not self._stop.is_set()

    def wake(self) -> None:
        """Interrupt the short polling wait after a canonical command wakeup."""

        self._wake.set()
        self._control_wake.set()

    async def run(self, context: AppSchedulerContext) -> None:
        if self._running:
            raise RuntimeError("app scheduler is already running")
        if self._pending_image_recovery is None:
            raise RuntimeError("app pending-image recovery is not bound")
        if self._media_cleanup is None:
            raise RuntimeError("app media cleanup is not bound")
        self._running = True
        self._stopped.clear()
        runtime = ConversationRuntimeService(
            context.sessions,
            model=context.model,
            telegram=context.telegram,
            new_uuid=self._new_uuid,
            now=self._now,
            operation_admission=context.operation_admission,
            account_id=self._account_id,
        )
        try:
            # Process commands once before admitting new work, then keep the
            # control lane independent from model/Telegram/media operations.
            # A provider call may take the full generation deadline; HUMAN or
            # pause commands must still reach the durable final gate while it
            # is in flight.
            await self._process_control_commands(context, self._now())
            async with asyncio.TaskGroup() as group:
                group.create_task(self._run_control_lane(context), name="app-control-lane")
                group.create_task(self._run_work_lane(context, runtime), name="app-work-lane")
        finally:
            self._running = False
            self._stopped.set()

    async def _run_control_lane(self, context: AppSchedulerContext) -> None:
        while not self._stop.is_set():
            await self._wait_for_work(self._control_wake)
            if self._stop.is_set():
                return
            await self._process_control_commands(context, self._now())

    async def _run_work_lane(
        self,
        context: AppSchedulerContext,
        runtime: ConversationRuntimeService,
    ) -> None:
        while not self._stop.is_set():
            await self._run_cycle(context, runtime)
            await self._wait_for_work()

    async def drain(self, _deadline: TerminationDeadline) -> None:
        self._stop.set()
        self._wake.set()
        self._control_wake.set()
        if self._running:
            await self._stopped.wait()

    async def _wait_for_work(self, wake: asyncio.Event | None = None) -> None:
        wake = self._wake if wake is None else wake
        if wake.is_set():
            wake.clear()
            return
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(wake.wait())
        try:
            done, pending = await asyncio.wait(
                (stop_task, wake_task),
                timeout=self._poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                await task
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            wake.clear()

    async def _run_cycle(
        self,
        context: AppSchedulerContext,
        runtime: ConversationRuntimeService,
    ) -> None:
        now = self._now()
        if self._due(self._last_recovery_at, now, APP_RECOVERY_INTERVAL_SECONDS):
            await runtime.recover_once()
            self._last_recovery_at = now
        if self._due(
            self._last_media_recovery_at,
            now,
            APP_MEDIA_RECOVERY_INTERVAL_SECONDS,
        ):
            recovery = self._pending_image_recovery
            if recovery is None:
                raise RuntimeError("app pending-image recovery disappeared")
            await recovery()
            self._last_media_recovery_at = now
        if self._due(
            self._last_media_cleanup_at,
            now,
            APP_MEDIA_CLEANUP_INTERVAL_SECONDS,
        ) and self._due(
            self._last_media_cleanup_attempt_at,
            now,
            APP_MEDIA_CLEANUP_RETRY_SECONDS,
        ):
            cleanup = self._media_cleanup
            if cleanup is None:
                raise RuntimeError("app media cleanup disappeared")
            self._last_media_cleanup_attempt_at = now
            try:
                await cleanup()
            except Exception:
                # A transient database/filesystem failure must not stop the sole
                # Telethon owner. Keep the success watermark unchanged so the
                # bounded cleanup is retried after the short retry interval.
                cleanup_succeeded = False
            else:
                cleanup_succeeded = True
            if cleanup_succeeded:
                self._last_media_cleanup_at = now

        try:
            operation_allowed = await context.operation_admission()
        except Exception:
            operation_allowed = False
        if operation_allowed is not True:
            return
        turn_id = await self._next_due_turn(context, now)
        if turn_id is not None:
            with suppress(OrchestratorConflictError, OperationAdmissionError):
                await runtime.run_due_turn(turn_id=turn_id, owner=context.owner_instance_id)
                # A concurrent Telegram event or control command may legitimately
                # invalidate the selected identity before the runtime acquires it.
            return
        group_id = await self._next_delivery_group(context)
        if group_id is not None:
            await runtime.dispatch_group(group_id=group_id, owner=context.owner_instance_id)

    async def _process_control_commands(
        self,
        context: AppSchedulerContext,
        now: datetime,
    ) -> None:
        for _ in range(APP_CONTROL_BATCH):
            async with context.sessions() as session, session.begin():
                result = await ConversationControlCommandProcessor(
                    session=session,
                    account_id=self._account_id,
                    allowed_admin_ids=self._allowed_admin_ids,
                    bot_identity=self._bot_identity,
                    new_uuid=self._new_uuid,
                ).process_next(now=now)
            if result is None:
                break

    async def _next_due_turn(
        self,
        context: AppSchedulerContext,
        now: datetime,
    ) -> UUID | None:
        async with context.sessions() as session:
            return await session.scalar(
                select(conversation_turns.c.id)
                .where(
                    conversation_turns.c.account_id == self._account_id,
                    or_(
                        conversation_turns.c.state == "ready",
                        (
                            (conversation_turns.c.state == "collecting")
                            & or_(
                                conversation_turns.c.quiet_deadline_at <= now,
                                conversation_turns.c.hard_deadline_at <= now,
                            )
                        ),
                    ),
                )
                .order_by(
                    conversation_turns.c.hard_deadline_at,
                    conversation_turns.c.quiet_deadline_at,
                    conversation_turns.c.id,
                )
                .limit(1)
            )

    async def _next_delivery_group(self, context: AppSchedulerContext) -> UUID | None:
        async with context.sessions() as session:
            return await session.scalar(
                select(outbound_delivery_groups.c.id)
                .where(
                    outbound_delivery_groups.c.account_id == self._account_id,
                    outbound_delivery_groups.c.state.in_(("planned", "partial")),
                )
                .order_by(outbound_delivery_groups.c.created_at, outbound_delivery_groups.c.id)
                .limit(1)
            )

    @staticmethod
    def _due(previous: datetime | None, now: datetime, seconds: float) -> bool:
        return previous is None or now >= previous + timedelta(seconds=seconds)


def build_production_app_scheduler(settings: ProductionSettings) -> ProductionAppScheduler:
    """Build the only scheduler allowed to own Telegram-side work."""

    if settings.process is not ProductionProcess.APP:
        raise ValueError("app scheduler requires app process settings")
    identity = settings.deployment.runtime_identity
    return ProductionAppScheduler(
        account_id=identity.account_id,
        allowed_admin_ids=frozenset(identity.control_admin_user_ids),
        bot_identity=identity.control_bot_username,
    )


__all__ = [
    "APP_CONTROL_BATCH",
    "APP_MEDIA_CLEANUP_INTERVAL_SECONDS",
    "APP_MEDIA_CLEANUP_RETRY_SECONDS",
    "APP_MEDIA_RECOVERY_INTERVAL_SECONDS",
    "APP_RECOVERY_INTERVAL_SECONDS",
    "APP_SCHEDULER_POLL_SECONDS",
    "ProductionAppScheduler",
    "build_production_app_scheduler",
]
