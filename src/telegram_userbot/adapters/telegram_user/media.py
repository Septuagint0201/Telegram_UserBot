"""Telethon media streaming with application-owned source resolution."""

import base64
import binascii
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from telethon import types  # type: ignore[import-untyped]
from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]

from telegram_userbot.application.ports.media import (
    TelegramImageDownloadRequest,
    TelegramImageSource,
)
from telegram_userbot.application.ports.telegram import (
    TelegramFloodWaitError,
    TelegramPermanentError,
    TelegramTransientError,
)
from telegram_userbot.application.ports.telegram_peer import TelegramMediaBinding


class TelethonMediaClient(Protocol):
    def iter_download(self, file: object, *, request_size: int) -> AsyncIterator[bytes]: ...


MediaResolver = Callable[[TelegramImageDownloadRequest], Awaitable[object]]
MediaBindingLoader = Callable[
    [TelegramImageDownloadRequest], Awaitable[TelegramMediaBinding | None]
]

_DOCUMENT_REFERENCE = re.compile(
    r"v1:(document):([1-9][0-9]{0,18}):(0|-?[1-9][0-9]{0,18}):"
    r"([1-9][0-9]{0,9}):([A-Za-z0-9_-]{0,8192})\Z"
)
_PHOTO_REFERENCE = re.compile(
    r"v2:(photo):([1-9][0-9]{0,18}):(0|-?[1-9][0-9]{0,18}):"
    r"([1-9][0-9]{0,9}):([A-Za-z0-9]):([A-Za-z0-9_-]{0,8192})\Z"
)


class TelethonMediaReferenceError(RuntimeError):
    """Stable, content-free rejection of an unbound or malformed media reference."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TelethonOpaqueMediaResolver:
    """Turn only an exact DB-bound opaque reference back into a Telethon location."""

    def __init__(self, load_binding: MediaBindingLoader) -> None:
        self._load_binding = load_binding

    async def __call__(self, request: TelegramImageDownloadRequest) -> object:
        binding = await self._load_binding(request)
        if binding is None:
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_NOT_BOUND")
        if (
            binding.account_id != request.account_id.value
            or binding.conversation_id != request.conversation_id.value
            or binding.message_id != request.message_id.value
            or binding.revision_no != request.revision_no
            or binding.position != request.position
        ):
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_SCOPE_MISMATCH")
        match = _PHOTO_REFERENCE.fullmatch(
            binding.opaque_file_reference
        ) or _DOCUMENT_REFERENCE.fullmatch(binding.opaque_file_reference)
        if match is None:
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_REFERENCE_INVALID")
        groups = match.groups()
        if len(groups) == 6:
            (
                kind,
                media_id_raw,
                access_hash_raw,
                dc_id_raw,
                thumb_size,
                encoded_reference,
            ) = groups
        else:
            kind, media_id_raw, access_hash_raw, dc_id_raw, encoded_reference = groups
            thumb_size = ""
        if (kind == "photo") != (binding.kind.value == "photo"):
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_KIND_MISMATCH")
        try:
            media_id = int(media_id_raw)
            access_hash = int(access_hash_raw)
            dc_id = int(dc_id_raw)
            padding = "=" * (-len(encoded_reference) % 4)
            file_reference = base64.b64decode(
                encoded_reference + padding, altchars=b"-_", validate=True
            )
        except ValueError, binascii.Error:
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_REFERENCE_INVALID") from None
        if (
            not 0 < media_id <= (1 << 63) - 1
            or not -(1 << 63) <= access_hash < (1 << 63)
            or not 0 < dc_id <= 10_000
            or len(file_reference) > 6144
            or base64.urlsafe_b64encode(file_reference).decode("ascii").rstrip("=")
            != encoded_reference
        ):
            raise TelethonMediaReferenceError("TELEGRAM_MEDIA_REFERENCE_INVALID")
        location_type = (
            types.InputPhotoFileLocation if kind == "photo" else types.InputDocumentFileLocation
        )
        return location_type(
            id=media_id,
            access_hash=access_hash,
            file_reference=file_reference,
            thumb_size=thumb_size,
        )


class TelethonImageSource(TelegramImageSource):
    """Stream a resolver-approved Telegram photo/image-document reference."""

    def __init__(
        self,
        client: TelethonMediaClient,
        resolve_media: MediaResolver,
        *,
        request_size: int = 512 * 1024,
    ) -> None:
        if request_size <= 0 or request_size % 4096:
            raise ValueError("Telethon request size must be a positive 4096-byte multiple")
        self._client = client
        self._resolve_media = resolve_media
        self._request_size = request_size

    async def iter_image(self, request: TelegramImageDownloadRequest) -> AsyncIterator[bytes]:
        media = await self._resolve_media(request)
        try:
            async for chunk in self._client.iter_download(media, request_size=self._request_size):
                if not isinstance(chunk, bytes):
                    raise TelegramTransientError("telegram_media_chunk_invalid")
                yield chunk
        except FloodWaitError as error:
            raise TelegramFloodWaitError(error.seconds) from error
        except (ConnectionError, TimeoutError, OSError) as error:
            raise TelegramTransientError("telegram_media_download_failed") from error
        except RPCError as error:
            raise TelegramPermanentError("telegram_media_rejected") from error


class ReplayImageSource(TelegramImageSource):
    def __init__(
        self, payloads: dict[tuple[str, int, int], bytes], *, chunk_size: int = 8192
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("fake media chunk size must be positive")
        self._payloads = payloads
        self._chunk_size = chunk_size

    async def iter_image(self, request: TelegramImageDownloadRequest) -> AsyncIterator[bytes]:
        key = (str(request.message_id), request.revision_no, request.position)
        try:
            payload = self._payloads[key]
        except KeyError as error:
            raise TelegramPermanentError("telegram_media_unavailable") from error
        for offset in range(0, len(payload), self._chunk_size):
            yield payload[offset : offset + self._chunk_size]


__all__ = [
    "ReplayImageSource",
    "TelethonImageSource",
    "TelethonMediaReferenceError",
    "TelethonOpaqueMediaResolver",
]
