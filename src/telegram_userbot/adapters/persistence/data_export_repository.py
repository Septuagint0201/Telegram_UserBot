"""PostgreSQL queue for root-operated, age-encrypted data exports."""

from __future__ import annotations

import re
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import RowMapping, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import data_export_requests
from telegram_userbot.domain.data_export import (
    DataExportRequest,
    DataExportState,
)
from telegram_userbot.domain.shared.time import require_aware

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _record(row: RowMapping) -> DataExportRequest:
    return DataExportRequest(
        id=cast(UUID, row["id"]),
        account_id=cast(UUID, row["account_id"]),
        contact_id=cast(UUID | None, row["contact_id"]),
        state=DataExportState(row["state"]),
        requested_by=cast(str, row["requested_by"]),
        format_version=cast(int, row["format_version"]),
        created_at=cast(datetime, row["created_at"]),
        expires_at=cast(datetime, row["expires_at"]),
        owner_instance_id=cast(UUID | None, row["owner_instance_id"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        artifact_sha256=cast(bytes | None, row["artifact_sha256"]),
        artifact_deleted_at=cast(datetime | None, row["artifact_deleted_at"]),
        last_error_code=cast(str | None, row["last_error_code"]),
        attempt_count=cast(int, row["attempt_count"]),
        version=cast(int, row["version"]),
    )


class DataExportRepository:
    """Claim/finalize exports without persisting paths, recipients, or plaintext."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: UUID) -> DataExportRequest | None:
        row = (
            (
                await self._session.execute(
                    select(data_export_requests).where(data_export_requests.c.id == request_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _record(row)

    async def create(self, request: DataExportRequest) -> DataExportRequest:
        if request.state is not DataExportState.REQUESTED:
            raise ValueError("new data export must be requested")
        await self._session.execute(
            postgresql_insert(data_export_requests)
            .values(
                id=request.id,
                account_id=request.account_id,
                contact_id=request.contact_id,
                state=request.state.value,
                requested_by=request.requested_by,
                format_version=request.format_version,
                created_at=request.created_at,
                expires_at=request.expires_at,
                attempt_count=0,
                version=1,
            )
            .on_conflict_do_nothing(index_elements=[data_export_requests.c.id])
        )
        loaded = await self.get(request.id)
        if loaded is None:
            raise RuntimeError("data export request insert was not observable")
        if loaded != request:
            raise ValueError("data export request id conflicts with durable state")
        return loaded

    async def claim_next(
        self,
        *,
        owner_instance_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> DataExportRequest | None:
        now = require_aware(now, "now")
        lease_expires_at = require_aware(lease_expires_at, "lease_expires_at")
        if not isinstance(owner_instance_id, UUID) or owner_instance_id.int == 0:
            raise ValueError("data export owner id is invalid")
        if lease_expires_at <= now:
            raise ValueError("data export lease must end after claim time")
        candidate = (
            (
                await self._session.execute(
                    select(data_export_requests)
                    .where(
                        data_export_requests.c.expires_at > now,
                        or_(
                            data_export_requests.c.state == DataExportState.REQUESTED.value,
                            (data_export_requests.c.state == DataExportState.CLAIMED.value)
                            & (data_export_requests.c.lease_expires_at <= now),
                        ),
                    )
                    .order_by(
                        data_export_requests.c.created_at.asc(),
                        data_export_requests.c.id.asc(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            return None
        row = (
            (
                await self._session.execute(
                    update(data_export_requests)
                    .where(
                        data_export_requests.c.id == candidate["id"],
                        data_export_requests.c.version == candidate["version"],
                    )
                    .values(
                        state=DataExportState.CLAIMED.value,
                        owner_instance_id=owner_instance_id,
                        lease_expires_at=lease_expires_at,
                        attempt_count=data_export_requests.c.attempt_count + 1,
                        version=data_export_requests.c.version + 1,
                    )
                    .returning(*data_export_requests.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError("data export claim compare-and-set failed")
        return _record(row)

    async def complete(
        self,
        *,
        request_id: UUID,
        owner_instance_id: UUID,
        expected_version: int,
        artifact_sha256: bytes,
        now: datetime,
    ) -> DataExportRequest | None:
        now = require_aware(now, "now")
        if len(artifact_sha256) != 32:
            raise ValueError("data export artifact digest must be SHA-256")
        row = await self._finalize_claim(
            request_id=request_id,
            owner_instance_id=owner_instance_id,
            expected_version=expected_version,
            now=now,
            values={
                "state": DataExportState.COMPLETED.value,
                "completed_at": now,
                "artifact_sha256": bytes(artifact_sha256),
                "last_error_code": None,
            },
        )
        return None if row is None else _record(row)

    async def renew(
        self,
        *,
        request_id: UUID,
        owner_instance_id: UUID,
        expected_version: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> DataExportRequest | None:
        """Advance a claim lease and its fencing version only for its current owner."""

        now = require_aware(now, "now")
        lease_expires_at = require_aware(lease_expires_at, "lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("data export lease must end after renewal time")
        if not isinstance(request_id, UUID) or request_id.int == 0:
            raise ValueError("data export request id is invalid")
        if not isinstance(owner_instance_id, UUID) or owner_instance_id.int == 0:
            raise ValueError("data export owner id is invalid")
        if expected_version <= 0:
            raise ValueError("data export expected version is invalid")
        row = (
            (
                await self._session.execute(
                    update(data_export_requests)
                    .where(
                        data_export_requests.c.id == request_id,
                        data_export_requests.c.state == DataExportState.CLAIMED.value,
                        data_export_requests.c.owner_instance_id == owner_instance_id,
                        data_export_requests.c.version == expected_version,
                        data_export_requests.c.lease_expires_at > now,
                        data_export_requests.c.expires_at > now,
                    )
                    .values(
                        lease_expires_at=lease_expires_at,
                        version=data_export_requests.c.version + 1,
                    )
                    .returning(*data_export_requests.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _record(row)

    async def fail(
        self,
        *,
        request_id: UUID,
        owner_instance_id: UUID,
        expected_version: int,
        error_code: str,
        now: datetime,
    ) -> DataExportRequest | None:
        now = require_aware(now, "now")
        if _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("data export error code is invalid")
        row = await self._finalize_claim(
            request_id=request_id,
            owner_instance_id=owner_instance_id,
            expected_version=expected_version,
            now=now,
            values={
                "state": DataExportState.FAILED.value,
                "completed_at": now,
                "artifact_sha256": None,
                "last_error_code": error_code,
            },
        )
        return None if row is None else _record(row)

    async def _finalize_claim(
        self,
        *,
        request_id: UUID,
        owner_instance_id: UUID,
        expected_version: int,
        now: datetime,
        values: dict[str, object],
    ) -> RowMapping | None:
        if not isinstance(request_id, UUID) or request_id.int == 0:
            raise ValueError("data export request id is invalid")
        if not isinstance(owner_instance_id, UUID) or owner_instance_id.int == 0:
            raise ValueError("data export owner id is invalid")
        if expected_version <= 0:
            raise ValueError("data export expected version is invalid")
        return (
            (
                await self._session.execute(
                    update(data_export_requests)
                    .where(
                        data_export_requests.c.id == request_id,
                        data_export_requests.c.state == DataExportState.CLAIMED.value,
                        data_export_requests.c.owner_instance_id == owner_instance_id,
                        data_export_requests.c.version == expected_version,
                        data_export_requests.c.lease_expires_at > now,
                        data_export_requests.c.expires_at > now,
                    )
                    .values(
                        **values,
                        owner_instance_id=None,
                        lease_expires_at=None,
                        version=data_export_requests.c.version + 1,
                    )
                    .returning(*data_export_requests.c)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def expire_due(self, *, now: datetime) -> int:
        now = require_aware(now, "now")
        result = await self._session.execute(
            update(data_export_requests)
            .where(
                data_export_requests.c.state.in_(
                    (DataExportState.REQUESTED.value, DataExportState.CLAIMED.value)
                ),
                data_export_requests.c.expires_at <= now,
            )
            .values(
                state=DataExportState.EXPIRED.value,
                owner_instance_id=None,
                lease_expires_at=None,
                completed_at=now,
                last_error_code="REQUEST_EXPIRED",
                version=data_export_requests.c.version + 1,
            )
        )
        return int(getattr(result, "rowcount", 0))

    async def mark_artifact_deleted(
        self,
        *,
        request_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> DataExportRequest | None:
        now = require_aware(now, "now")
        row = (
            (
                await self._session.execute(
                    update(data_export_requests)
                    .where(
                        data_export_requests.c.id == request_id,
                        data_export_requests.c.state == DataExportState.COMPLETED.value,
                        data_export_requests.c.version == expected_version,
                        data_export_requests.c.artifact_deleted_at.is_(None),
                        data_export_requests.c.completed_at <= now,
                    )
                    .values(
                        artifact_deleted_at=now,
                        version=data_export_requests.c.version + 1,
                    )
                    .returning(*data_export_requests.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _record(row)


__all__ = ["DataExportRepository"]
