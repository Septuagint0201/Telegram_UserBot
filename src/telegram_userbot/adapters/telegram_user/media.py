"""Telethon media streaming with application-owned source resolution."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

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


class TelethonMediaClient(Protocol):
    def iter_download(self, file: object, *, request_size: int) -> AsyncIterator[bytes]: ...


MediaResolver = Callable[[TelegramImageDownloadRequest], Awaitable[object]]


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
