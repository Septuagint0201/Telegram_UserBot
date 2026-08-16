"""Private image validation and storage adapters."""

from telegram_userbot.adapters.media.cleanup import (
    DurableMediaCleanup,
    DurableMediaCleanupReport,
)
from telegram_userbot.adapters.media.storage import (
    CleanupCandidate,
    CleanupReport,
    MediaQuota,
    PrivateMediaStore,
    StoredMedia,
)
from telegram_userbot.adapters.media.validation import (
    ImageIngestionError,
    ImageIngestor,
    ImageLimits,
    ValidatedImage,
)

__all__ = [
    "CleanupCandidate",
    "CleanupReport",
    "DurableMediaCleanup",
    "DurableMediaCleanupReport",
    "ImageIngestionError",
    "ImageIngestor",
    "ImageLimits",
    "MediaQuota",
    "PrivateMediaStore",
    "StoredMedia",
    "ValidatedImage",
]
