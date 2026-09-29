"""Durable-offset long polling loop for the Control Bot."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from telegram_userbot.adapters.telegram_bot.dispatcher import DispatchOutcome
from telegram_userbot.adapters.telegram_bot.http import BotAPIError, TelegramBotAPI


class BotUpdateOffsetStore(Protocol):
    """The process root supplies a durable compare-and-set implementation."""

    async def load_next_offset(self) -> int: ...

    async def commit_next_offset(self, *, expected: int, replacement: int) -> bool: ...


class ControlUpdateExecutor(Protocol):
    async def recover_pending_responses(self, *, batch_size: int = 100) -> int: ...

    async def handle_update(
        self, *, update_id: int, update: Mapping[str, Any]
    ) -> DispatchOutcome: ...


class ControlBotPoller:
    def __init__(
        self,
        *,
        api: TelegramBotAPI,
        dispatcher: ControlUpdateExecutor,
        offsets: BotUpdateOffsetStore,
        long_poll_seconds: int = 45,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        if (
            type(long_poll_seconds) is not int
            or not 1 <= long_poll_seconds <= 50
            or isinstance(retry_delay_seconds, bool)
            or not 0.1 <= retry_delay_seconds <= 60
        ):
            raise ValueError("Control Bot polling settings are invalid")
        self._api = api
        self._dispatcher = dispatcher
        self._offsets = offsets
        self._long_poll_seconds = long_poll_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._identity_verified = False

    @property
    def identity_verified(self) -> bool:
        """Return only the current run's exact getMe identity state."""

        return self._identity_verified

    async def poll_once(self) -> int:
        await self._dispatcher.recover_pending_responses()
        current = await self._offsets.load_next_offset()
        if type(current) is not int or current < 0:
            raise BotAPIError("BOT_OFFSET_INVALID")
        updates = await self._api.get_updates(
            offset=current,
            long_poll_seconds=self._long_poll_seconds,
        )
        parsed = [(_update_id(update), update) for update in updates]
        parsed.sort(key=lambda item: item[0])
        processed = 0
        for update_id, update in parsed:
            if update_id < current:
                continue
            if update_id > 2**63 - 2:
                raise BotAPIError("BOT_UPDATE_INVALID")
            await self._dispatcher.handle_update(update_id=update_id, update=update)
            replacement = update_id + 1
            if not await self._offsets.commit_next_offset(
                expected=current,
                replacement=replacement,
            ):
                raise BotAPIError("BOT_OFFSET_CONFLICT")
            current = replacement
            processed += 1
        return processed

    async def run(self, stop: asyncio.Event) -> None:
        if not isinstance(stop, asyncio.Event):
            raise TypeError("Control Bot stop signal is invalid")
        try:
            await self._api.verify_identity()
            self._identity_verified = True
            while not stop.is_set():
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except BotAPIError:
                    await _wait_or_stop(stop, self._retry_delay_seconds)
        finally:
            self._identity_verified = False
            await self._api.aclose()


def _update_id(update: Any) -> int:
    try:
        value = update.get("update_id")
    except AttributeError:
        raise BotAPIError("BOT_UPDATE_INVALID") from None
    if type(value) is not int or value < 0:
        raise BotAPIError("BOT_UPDATE_INVALID")
    return value


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass


__all__ = ["BotUpdateOffsetStore", "ControlBotPoller", "ControlUpdateExecutor"]
