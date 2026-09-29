"""Single-owner Telethon Session lifecycle and durable update intake seam."""

import asyncio
import os
import re
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from uuid import RFC_4122, UUID, uuid7

from telethon import TelegramClient, events, types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import PeerAdmission, normalize_update
from telegram_userbot.adapters.telegram_user.telethon_updates import (
    TelegramUpdateWatermark,
    TelethonUpdateScope,
    convert_telethon_update,
)
from telegram_userbot.domain.messaging import NormalizedTelegramEvent, PeerKind
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import MonotonicInstant
from telegram_userbot.platform.runtime.drain import TerminationDeadline


class TelethonSessionRuntimeError(RuntimeError):
    """Stable, content-free lifecycle failure."""


class TelethonRuntimeClient(Protocol):
    def __call__(self, request: object) -> Awaitable[object]: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool: ...

    async def get_me(self) -> object: ...

    def add_event_handler(
        self, callback: Callable[[object], Awaitable[None]], event: object
    ) -> None: ...

    def remove_event_handler(
        self, callback: Callable[[object], Awaitable[None]], event: object | None = None
    ) -> int: ...

    async def catch_up(self) -> None: ...

    async def run_until_disconnected(self) -> None: ...


TelethonClientFactory = Callable[[Path, int, str], TelethonRuntimeClient]
TelegramEventIngest = Callable[[NormalizedTelegramEvent], Awaitable[object]]
TelegramEventBatchIngest = Callable[
    [tuple[NormalizedTelegramEvent, ...], TelegramUpdateWatermark | None], Awaitable[object]
]
WatermarkLoader = Callable[[], Awaitable[TelegramUpdateWatermark | None]]
WatermarkRecorder = Callable[[TelegramUpdateWatermark], Awaitable[None]]
OutboundMessageIdReconciler = Callable[[int, int, datetime], Awaitable[None]]
MonotonicClock = Callable[[], MonotonicInstant]
_AwaitT = TypeVar("_AwaitT")


class PeerAdmissionResolver(Protocol):
    async def __call__(self, scope: TelethonUpdateScope) -> PeerAdmission:
        """Fail closed as BOT/SELF/UNKNOWN unless a private user is verified."""

        ...


@dataclass(frozen=True, slots=True)
class TelethonSessionSettings:
    account_id: UUID
    telegram_user_id: int
    session_path: Path
    api_id: int
    api_hash: SensitiveValue[str]

    def __post_init__(self) -> None:
        if (
            self.account_id.int == 0
            or self.account_id.variant != RFC_4122
            or self.account_id.version not in range(1, 9)
        ):
            raise ValueError("TELETHON_ACCOUNT_ID_INVALID")
        if self.telegram_user_id <= 0:
            raise ValueError("TELETHON_TELEGRAM_USER_ID_INVALID")
        if self.api_id <= 0 or self.api_id > 2_147_483_647:
            raise ValueError("TELETHON_API_ID_INVALID")
        if re.fullmatch(r"[0-9a-fA-F]{32}", self.api_hash.reveal_for_use()) is None:
            raise ValueError("TELETHON_API_HASH_INVALID")


def default_telethon_client_factory(
    session_path: Path, api_id: int, api_hash: str
) -> TelethonRuntimeClient:
    """Construct a real client without starting an interactive authorization flow."""

    client = TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        sequential_updates=True,
        auto_reconnect=True,
    )
    return cast(TelethonRuntimeClient, client)


def _validate_session_path(path: Path) -> None:
    if not path.is_absolute() or path.suffix != ".session":
        raise TelethonSessionRuntimeError("TELETHON_SESSION_PATH_INVALID")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise TelethonSessionRuntimeError("TELETHON_SESSION_MISSING") from None
    except OSError:
        raise TelethonSessionRuntimeError("TELETHON_SESSION_PATH_UNAVAILABLE") from None
    if path.is_symlink():
        raise TelethonSessionRuntimeError("TELETHON_SESSION_SYMLINK_REJECTED")
    if not stat.S_ISREG(info.st_mode):
        raise TelethonSessionRuntimeError("TELETHON_SESSION_NOT_REGULAR")
    try:
        with path.open("rb") as session_file:
            header = session_file.read(16)
    except OSError:
        raise TelethonSessionRuntimeError("TELETHON_SESSION_PATH_UNAVAILABLE") from None
    # SQLiteSession would initialize an empty file during construction. Reject it
    # first so the normal runtime can never silently provision a replacement Session.
    if header != b"SQLite format 3\x00":
        raise TelethonSessionRuntimeError("TELETHON_SESSION_FORMAT_INVALID")
    if os.name == "posix":
        effective_uid = cast(
            Callable[[], int],
            getattr(os, "geteuid"),  # noqa: B009 - absent from the Windows os module
        )()
        if info.st_uid != effective_uid:
            raise TelethonSessionRuntimeError("TELETHON_SESSION_OWNER_INVALID")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TelethonSessionRuntimeError("TELETHON_SESSION_MODE_INVALID")


def _validate_client(client: object) -> TelethonRuntimeClient:
    required = (
        "__call__",
        "connect",
        "disconnect",
        "is_user_authorized",
        "get_me",
        "add_event_handler",
        "remove_event_handler",
        "catch_up",
        "run_until_disconnected",
    )
    if any(not callable(getattr(client, name, None)) for name in required):
        raise TelethonSessionRuntimeError("TELETHON_CLIENT_CONTRACT_INVALID")
    return cast(TelethonRuntimeClient, client)


class TelethonSessionRuntime:
    """Own exactly one pre-authorized Session and serialize update projection."""

    def __init__(  # noqa: PLR0913 - runtime dependencies stay explicit and injectable
        self,
        settings: TelethonSessionSettings,
        *,
        resolve_admission: PeerAdmissionResolver,
        ingest: TelegramEventIngest,
        ingest_batch: TelegramEventBatchIngest | None = None,
        client_factory: TelethonClientFactory = default_telethon_client_factory,
        load_watermark: WatermarkLoader | None = None,
        record_watermark: WatermarkRecorder | None = None,
        reconcile_outbound_message_id: OutboundMessageIdReconciler | None = None,
        new_uuid: Callable[[], UUID] = uuid7,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self._settings = settings
        self._resolve_admission = resolve_admission
        self._ingest = ingest
        self._ingest_batch = ingest_batch
        self._client_factory = client_factory
        self._load_watermark = load_watermark
        self._record_watermark = record_watermark
        self._reconcile_outbound_message_id = reconcile_outbound_message_id
        self._new_uuid = new_uuid
        self._now = now
        self._monotonic_clock = monotonic_clock or (
            lambda: MonotonicInstant(asyncio.get_running_loop().time())
        )
        self._client: TelethonRuntimeClient | None = None
        self._intake_client: TelethonRuntimeClient | None = None
        self._raw_builder: object | None = None
        # Keep a failed teardown's client reference until disconnect has actually
        # settled.  This prevents a timed-out close from looking successful to the
        # composition root and then releasing Redis/account ownership underneath a
        # still-live Telethon client.
        self._closing = False
        self._disconnect_task: asyncio.Future[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._ingest_lock = asyncio.Lock()
        self._last_catch_up_watermark: TelegramUpdateWatermark | None = None

    @staticmethod
    def _consume_finished_task(task: asyncio.Future[Any]) -> None:
        """Consume a detached task's terminal exception without awaiting it.

        A cancellation-resistant Telethon operation may still be running when the
        process-level shutdown budget expires.  The composition root owns the
        force-termination decision in that case, so this adapter must not await the
        task again.  Reading the terminal exception once it eventually settles keeps
        asyncio from emitting an unhandled-task warning during test teardown or a
        graceful event-loop shutdown.
        """

        with suppress(BaseException):
            task.exception()

    @classmethod
    def _detach_task(cls, task: asyncio.Future[Any]) -> None:
        if task.done():
            cls._consume_finished_task(task)
        else:
            task.add_done_callback(cls._consume_finished_task)

    @classmethod
    async def _cancel_task(cls, task: asyncio.Future[Any]) -> None:
        if not task.done():
            task.cancel()
        # A timer is expected to honour cancellation.  This helper is only used
        # where waiting for settlement cannot cross a caller's cancellation boundary.
        with suppress(BaseException):
            await task

    async def _await_with_deadline(
        self,
        awaitable: Awaitable[_AwaitT],
        deadline: TerminationDeadline | None,
    ) -> _AwaitT:
        """Await one Telethon operation without crossing a process drain deadline."""

        if deadline is None:
            return await awaitable

        task: asyncio.Future[_AwaitT] = asyncio.ensure_future(awaitable)
        remaining = deadline.remaining_seconds(self._monotonic_clock())
        if remaining <= 0:
            if not task.done():
                task.cancel()
            self._detach_task(task)
            raise TelethonSessionRuntimeError("TELETHON_DEADLINE_EXCEEDED")

        timer = asyncio.create_task(asyncio.sleep(remaining), name="telethon-deadline")
        try:
            done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # Preserve the caller's cancellation semantics.  Do not wait for a
            # provider/Telethon coroutine that suppresses cancellation indefinitely.
            await self._cancel_task(timer)
            if not task.done():
                task.cancel()
            self._detach_task(task)
            raise

        if task in done:
            await self._cancel_task(timer)
            return await task

        # The timer won.  Cancel is best effort; a cancellation-resistant task is
        # intentionally detached so the composition root can force-terminate the PID.
        if not task.done():
            task.cancel()
        self._detach_task(task)
        raise TelethonSessionRuntimeError("TELETHON_DEADLINE_EXCEEDED")

    async def _disconnect_with_deadline(
        self,
        client: TelethonRuntimeClient,
        deadline: TerminationDeadline | None,
    ) -> None:
        task = self._disconnect_task
        if task is None:
            task = asyncio.ensure_future(client.disconnect())
            # A deadline may detach this task while Telethon is still winding
            # down.  Consume a later exception even when no retry observes it;
            # the runtime keeps the task reference so a retry can still surface
            # the failure to its caller.
            task.add_done_callback(self._consume_finished_task)
            self._disconnect_task = task
        try:
            await self._await_with_deadline(asyncio.shield(task), deadline)
        except BaseException:
            if task.done():
                self._disconnect_task = None
            raise
        self._disconnect_task = None

    @property
    def started(self) -> bool:
        return self._client is not None and not self._closing

    @property
    def teardown_pending(self) -> bool:
        """Whether a client remains live after an interrupted/failed close."""

        return self._client is not None and self._closing

    @property
    def client(self) -> TelethonRuntimeClient:
        if self._closing:
            raise TelethonSessionRuntimeError("TELETHON_CLIENT_CLOSING")
        if self._client is None:
            raise TelethonSessionRuntimeError("TELETHON_CLIENT_NOT_STARTED")
        return self._client

    @property
    def intake_client(self) -> TelethonRuntimeClient:
        """Return the verified client used by catch-up callbacks or steady intake."""

        if self._closing:
            raise TelethonSessionRuntimeError("TELETHON_CLIENT_CLOSING")
        client = self._client or self._intake_client
        if client is None:
            raise TelethonSessionRuntimeError("TELETHON_CLIENT_NOT_VERIFIED")
        return client

    @property
    def last_catch_up_watermark(self) -> TelegramUpdateWatermark | None:
        return self._last_catch_up_watermark

    async def _verify_identity(
        self,
        client: TelethonRuntimeClient,
        deadline: TerminationDeadline | None = None,
    ) -> None:
        if await self._await_with_deadline(client.is_user_authorized(), deadline) is not True:
            raise TelethonSessionRuntimeError("TELETHON_SESSION_UNAUTHORIZED")
        me = await self._await_with_deadline(client.get_me(), deadline)
        user_id = getattr(me, "id", None)
        is_bot = getattr(me, "bot", False)
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id != self._settings.telegram_user_id
            or is_bot is True
        ):
            raise TelethonSessionRuntimeError("TELETHON_SESSION_IDENTITY_MISMATCH")

    async def _catch_up(
        self,
        client: TelethonRuntimeClient,
        deadline: TerminationDeadline | None = None,
    ) -> None:
        watermark = None
        if self._load_watermark is not None:
            watermark = await self._await_with_deadline(self._load_watermark(), deadline)
            if watermark is not None and not isinstance(watermark, TelegramUpdateWatermark):
                raise TelethonSessionRuntimeError("TELETHON_WATERMARK_INVALID")
        self._last_catch_up_watermark = watermark
        await self._await_with_deadline(client.catch_up(), deadline)

    async def _cleanup_failed_start(
        self,
        client: TelethonRuntimeClient,
        builder: object,
        deadline: TerminationDeadline | None = None,
    ) -> None:
        self._client = client
        self._raw_builder = builder
        self._intake_client = None
        self._closing = True
        with suppress(Exception):
            client.remove_event_handler(self._handle_update, builder)
        try:
            await self._disconnect_with_deadline(client, deadline)
        except BaseException:
            return
        self._client = None
        self._raw_builder = None
        self._closing = False

    async def start(self, *, deadline: TerminationDeadline | None = None) -> None:
        """Connect only an existing authorized Session; never call start/sign_in."""

        await self._await_with_deadline(self._lifecycle_lock.acquire(), deadline)
        try:
            if self._closing:
                raise TelethonSessionRuntimeError("TELETHON_CLIENT_CLOSING")
            if self._client is not None:
                return
            _validate_session_path(self._settings.session_path)
            try:
                client = _validate_client(
                    self._client_factory(
                        self._settings.session_path,
                        self._settings.api_id,
                        self._settings.api_hash.reveal_for_use(),
                    )
                )
            except TelethonSessionRuntimeError:
                raise
            except Exception as error:
                raise TelethonSessionRuntimeError("TELETHON_CLIENT_FACTORY_FAILED") from error

            builder = events.Raw()
            try:
                client.add_event_handler(self._handle_update, builder)
                await self._await_with_deadline(client.connect(), deadline)
                await self._verify_identity(client, deadline)
                # ``catch_up`` dispatches through the registered raw handler before
                # startup completes. Admission may need this exact verified client
                # for a first-seen private peer, but the public sending client stays
                # unavailable until all catch-up updates have committed.
                self._intake_client = client
                await self._catch_up(client, deadline)
            except asyncio.CancelledError:
                self._intake_client = None
                cleanup = self._cleanup_failed_start(client, builder, deadline)
                with suppress(TelethonSessionRuntimeError):
                    await asyncio.shield(cleanup)
                raise
            except TelethonSessionRuntimeError:
                self._intake_client = None
                await self._cleanup_failed_start(client, builder, deadline)
                raise
            except Exception as error:
                self._intake_client = None
                await self._cleanup_failed_start(client, builder, deadline)
                raise TelethonSessionRuntimeError("TELETHON_START_FAILED") from error
            self._raw_builder = builder
            self._client = client
            self._intake_client = None
            self._closing = False
        finally:
            self._lifecycle_lock.release()

    async def catch_up(self, *, deadline: TerminationDeadline | None = None) -> None:
        """Explicit reconnect seam: replay may duplicate already durable events."""

        await self._await_with_deadline(self._lifecycle_lock.acquire(), deadline)
        try:
            client = self.client
            try:
                await self._verify_identity(client, deadline)
                await self._catch_up(client, deadline)
            except TelethonSessionRuntimeError:
                raise
            except Exception as error:
                raise TelethonSessionRuntimeError("TELETHON_CATCH_UP_FAILED") from error
        finally:
            self._lifecycle_lock.release()

    async def reconnect(self, *, deadline: TerminationDeadline | None = None) -> None:
        """Reopen the same explicit Session and run catch-up before becoming usable."""

        await self.close(deadline=deadline)
        await self.start(deadline=deadline)

    async def run_until_disconnected(self, *, deadline: TerminationDeadline | None = None) -> None:
        if not self.started:
            await self.start(deadline=deadline)
        client = self.client
        primary_error: BaseException | None = None
        try:
            await self._await_with_deadline(client.run_until_disconnected(), deadline)
        except BaseException as error:
            primary_error = error
        finally:
            try:
                await self.close(deadline=deadline)
            except BaseException:
                # Preserve the operation/cancellation that caused the run to
                # end.  A close failure leaves teardown_pending set and can be
                # observed or retried by the composition root.
                if primary_error is None:
                    raise
        if primary_error is not None:
            raise primary_error

    async def close(self, *, deadline: TerminationDeadline | None = None) -> None:
        await self._await_with_deadline(self._lifecycle_lock.acquire(), deadline)
        try:
            client = self._client
            builder = self._raw_builder
            if client is None:
                self._intake_client = None
                self._closing = False
                return
            self._closing = True
            self._intake_client = None
            removal_failed = False
            try:
                client.remove_event_handler(self._handle_update, builder)
            except Exception:
                removal_failed = True
            await self._await_with_deadline(self._ingest_lock.acquire(), deadline)
            self._ingest_lock.release()
            try:
                await self._disconnect_with_deadline(client, deadline)
            except TelethonSessionRuntimeError:
                raise
            except Exception as error:
                raise TelethonSessionRuntimeError("TELETHON_DISCONNECT_FAILED") from error
            self._client = None
            self._raw_builder = None
            self._closing = False
            if removal_failed:
                raise TelethonSessionRuntimeError("TELETHON_HANDLER_REMOVE_FAILED")
        finally:
            self._lifecycle_lock.release()

    async def _handle_update(self, update: object) -> None:
        async with self._ingest_lock:
            if isinstance(update, types.UpdateShort):
                update = update.update
            if isinstance(update, types.UpdateMessageID):
                # This is Telegram's only exact, content-free crash-recovery mapping
                # from our stable random_id to the accepted message id.  It has no
                # peer payload and therefore bypasses ordinary message projection.
                random_id = update.random_id
                message_id = update.id
                if (
                    isinstance(random_id, bool)
                    or not isinstance(random_id, int)
                    or random_id <= 0
                    or isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id <= 0
                ):
                    raise TelethonSessionRuntimeError("TELETHON_MESSAGE_ID_MAPPING_INVALID")
                if self._reconcile_outbound_message_id is not None:
                    await self._reconcile_outbound_message_id(
                        random_id,
                        message_id,
                        self._now(),
                    )
                return
            candidates = convert_telethon_update(
                update,
                managed_user_id=self._settings.telegram_user_id,
                observed_at=self._now(),
            )
            completed_watermark: TelegramUpdateWatermark | None = None
            normalized_events: list[NormalizedTelegramEvent] = []
            for candidate in candidates:
                admission = await self._resolve_admission(candidate.scope)
                self._validate_admission(candidate.scope, admission)
                event = normalize_update(
                    event_uuid=self._new_uuid(), admission=admission, raw=candidate.raw
                )
                normalized_events.append(event)
                if candidate.watermark is not None:
                    if completed_watermark is not None and (
                        candidate.watermark.scope,
                        candidate.watermark.pts,
                        candidate.watermark.pts_count,
                    ) != (
                        completed_watermark.scope,
                        completed_watermark.pts,
                        completed_watermark.pts_count,
                    ):
                        raise TelethonSessionRuntimeError(
                            "TELETHON_UPDATE_BATCH_WATERMARK_MISMATCH"
                        )
                    completed_watermark = candidate.watermark
            if self._ingest_batch is not None:
                await self._ingest_batch(tuple(normalized_events), completed_watermark)
                return
            for event in normalized_events:
                await self._ingest(event)
            # A raw Telegram update can contain multiple message operations (notably
            # UpdateDeleteMessages). Advance at most once, and only after every
            # canonical ingest returned successfully. The ingest and cursor writes are
            # separate transactions in M8, so replay safety still relies on the durable
            # event fingerprint if a crash happens between these two calls.
            if completed_watermark is not None and self._record_watermark is not None:
                await self._record_watermark(completed_watermark)

    def _validate_admission(self, scope: TelethonUpdateScope, admission: PeerAdmission) -> None:
        if admission.account_id != self._settings.account_id:
            raise TelethonSessionRuntimeError("TELETHON_ADMISSION_ACCOUNT_MISMATCH")
        if (
            scope.telegram_chat_id is not None
            and admission.telegram_chat_id != scope.telegram_chat_id
        ):
            raise TelethonSessionRuntimeError("TELETHON_ADMISSION_CHAT_MISMATCH")
        if (
            scope.peer_kind_hint in {PeerKind.GROUP, PeerKind.CHANNEL, PeerKind.SELF}
            and admission.peer_kind is PeerKind.PRIVATE_USER
        ):
            raise TelethonSessionRuntimeError("TELETHON_ADMISSION_PEER_MISMATCH")


__all__ = [
    "MonotonicClock",
    "PeerAdmissionResolver",
    "TelegramEventBatchIngest",
    "TelegramEventIngest",
    "TelethonClientFactory",
    "TelethonRuntimeClient",
    "TelethonSessionRuntime",
    "TelethonSessionRuntimeError",
    "TelethonSessionSettings",
    "WatermarkLoader",
    "WatermarkRecorder",
    "default_telethon_client_factory",
]
