"""Crash-recoverable bridge between media cleanup leases and filesystem deletion."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

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
        account_id: UUID | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._on_failure = on_failure
        self._account_id = account_id

    async def run_once(self, *, now: datetime, limit: int = 50) -> DurableMediaCleanupReport:
        if self._account_id is not None and await self._repository.account_is_deleting(
            self._account_id
        ):
            try:
                await asyncio.to_thread(self._store.erase_legacy_account_uploads, self._account_id)
            except Exception:
                if self._on_failure is not None:
                    self._on_failure("media_account_cleanup_failed")
                return DurableMediaCleanupReport(0, 0, 1)
        leases = (
            await self._repository.claim_expired(now=now, limit=limit)
            if self._account_id is None
            else await self._repository.claim_expired(
                now=now, limit=limit, account_id=self._account_id
            )
        )
        if leases:
            await self._repository.commit_cleanup_boundary()
        deleted = 0
        already_missing = 0
        failed = 0
        for lease in leases:
            try:
                existed = await asyncio.to_thread(
                    self._store.erase_object,
                    account_id=lease.account_id,
                    object_id=lease.object_id,
                    storage_key=lease.storage_key,
                    expected_sha256=lease.sha256,
                )
            except Exception:
                outcome = await self._repository.finish_deletion(
                    deletion=lease,
                    deleted=False,
                    now=now,
                    error_code="media_delete_failed",
                )
                await self._repository.commit_cleanup_boundary()
                if outcome.completed:
                    failed += 1
                    if self._on_failure is not None:
                        self._on_failure(
                            "media_delete_failed_critical"
                            if outcome.critical_alert
                            else "media_delete_failed"
                        )
            else:
                outcome = await self._repository.finish_deletion(
                    deletion=lease,
                    deleted=True,
                    now=now,
                )
                await self._repository.commit_cleanup_boundary()
                if outcome.completed:
                    if existed:
                        deleted += 1
                    else:
                        already_missing += 1
        failed += await self._verify_inventory(now)
        return DurableMediaCleanupReport(deleted, already_missing, failed)

    async def _verify_inventory(self, now: datetime) -> int:
        if self._account_id is not None:
            try:
                requests = await self._repository.inventory_requests(self._account_id)
                if requests:
                    await asyncio.to_thread(
                        self._store.verify_erasure_inventory,
                        account_id=self._account_id,
                        objects=await self._repository.inventory_objects(self._account_id),
                        account_wipe=await self._repository.account_is_deleting(self._account_id),
                    )
                    # Release account admission before the request FK takes its
                    # key-share lock (worker lock order is request -> account).
                    # Erased-source guards and durable object markers keep the
                    # verified namespace closed across this acknowledgment gap.
                    await self._repository.commit_cleanup_boundary()
                    await self._repository.record_inventory(requests, now)
                await self._repository.commit_cleanup_boundary()
            except Exception:
                # Do not certify an incomplete or unattributed inventory.
                if self._on_failure is not None:
                    self._on_failure("media_inventory_failed")
                return 1
        return 0
