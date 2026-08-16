"""Bounded image ingestion without trusting Telegram MIME declarations."""

import asyncio
import hashlib
import io
import warnings
from collections.abc import AsyncIterable
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

from telegram_userbot.domain.shared.redaction import SensitiveValue

ALLOWED_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class ImageIngestionError(ValueError):
    """Stable content-free validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageLimits:
    max_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_side: int = 16_384
    timeout_seconds: float = 30.0
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if (
            min(
                self.max_bytes,
                self.max_pixels,
                self.max_side,
                self.max_concurrency,
            )
            <= 0
            or self.timeout_seconds <= 0
        ):
            raise ValueError("image limits must be positive")


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    content: SensitiveValue[bytes] = field(repr=False)
    sha256: bytes
    mime_type: str
    width: int
    height: int
    byte_size: int

    def __post_init__(self) -> None:
        if len(self.sha256) != 32 or self.mime_type not in ALLOWED_MIME_BY_FORMAT.values():
            raise ValueError("invalid validated image metadata")


class ImageIngestor:
    """Download and fully decode one image at a time by default."""

    def __init__(self, limits: ImageLimits | None = None) -> None:
        self.limits = limits or ImageLimits()
        self._semaphore = asyncio.Semaphore(self.limits.max_concurrency)

    async def ingest(
        self,
        chunks: AsyncIterable[bytes],
        *,
        declared_mime: str,
        declared_size: int | None = None,
    ) -> ValidatedImage:
        if declared_mime not in ALLOWED_MIME_BY_FORMAT.values():
            raise ImageIngestionError("image_mime_unsupported")
        if declared_size is not None and (
            declared_size < 0 or declared_size > self.limits.max_bytes
        ):
            raise ImageIngestionError("image_byte_limit")
        async with self._semaphore:
            try:
                async with asyncio.timeout(self.limits.timeout_seconds):
                    payload = await self._read_bounded(chunks)
                    return await asyncio.to_thread(
                        self.validate_bytes, payload, declared_mime=declared_mime
                    )
            except TimeoutError as error:
                raise ImageIngestionError("image_download_timeout") from error

    async def _read_bounded(self, chunks: AsyncIterable[bytes]) -> bytes:
        payload = bytearray()
        async for chunk in chunks:
            if not isinstance(chunk, bytes) or not chunk:
                raise ImageIngestionError("image_download_chunk_invalid")
            if len(payload) + len(chunk) > self.limits.max_bytes:
                raise ImageIngestionError("image_byte_limit")
            payload.extend(chunk)
        if not payload:
            raise ImageIngestionError("image_empty")
        return bytes(payload)

    def validate_bytes(self, payload: bytes, *, declared_mime: str) -> ValidatedImage:
        if not payload:
            raise ImageIngestionError("image_empty")
        if len(payload) > self.limits.max_bytes:
            raise ImageIngestionError("image_byte_limit")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    actual_mime = ALLOWED_MIME_BY_FORMAT.get(image.format or "")
                    if actual_mime is None:
                        raise ImageIngestionError("image_format_unsupported")
                    if actual_mime != declared_mime:
                        raise ImageIngestionError("image_mime_mismatch")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ImageIngestionError("image_animation_unsupported")
                    width, height = image.size
                    if width <= 0 or height <= 0 or max(width, height) > self.limits.max_side:
                        raise ImageIngestionError("image_dimension_limit")
                    if width * height > self.limits.max_pixels:
                        raise ImageIngestionError("image_pixel_limit")
                    image.load()
        except Image.DecompressionBombError as error:
            raise ImageIngestionError("image_decompression_bomb") from error
        except Image.DecompressionBombWarning as error:
            raise ImageIngestionError("image_decompression_bomb") from error
        except (OSError, UnidentifiedImageError) as error:
            raise ImageIngestionError("image_decode_failed") from error
        return ValidatedImage(
            SensitiveValue(payload),
            hashlib.sha256(payload).digest(),
            declared_mime,
            width,
            height,
            len(payload),
        )
