"""Crash-recoverable bridge between media cleanup leases and filesystem deletion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from telegram_userbot.adapters.media.storage import PrivateMediaStore

if TYPE_CHECKING:
    from telegram_userbot.adapters.persistence.media_repository import MediaRepository


@dataclass(frozen=True, slots=True)
class DurableMediaCleanupReport:
    deleted: int
    already_missing: int
    failed: int


class DurableMediaCleanup:
    def __init__(
        self,
        *,
        repository: MediaRepository,
        store: PrivateMediaStore,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._on_failure = on_failure

    async def run_once(self, *, now: datetime, limit: int = 50) -> DurableMediaCleanupReport:
        leases = await self._repository.claim_expired(now=now, limit=limit)
        if leases:
            await self._repository.commit_cleanup_boundary()
        deleted = 0
        already_missing = 0
        failed = 0
        for lease in leases:
            try:
                existed = self._store.delete_verified(
                    storage_key=lease.storage_key,
                    expected_sha256=lease.sha256,
                )
            except Exception:
                completed = await self._repository.finish_deletion(
                    deletion=lease,
                    deleted=False,
                    now=now,
                    error_code="media_delete_failed",
                )
                await self._repository.commit_cleanup_boundary()
                if completed:
                    failed += 1
                    if self._on_failure is not None:
                        self._on_failure("media_delete_failed")
            else:
                completed = await self._repository.finish_deletion(
                    deletion=lease,
                    deleted=True,
                    now=now,
                )
                await self._repository.commit_cleanup_boundary()
                if completed:
                    if existed:
                        deleted += 1
                    else:
                        already_missing += 1
        return DurableMediaCleanupReport(deleted, already_missing, failed)
