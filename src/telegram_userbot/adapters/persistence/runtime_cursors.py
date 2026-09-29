"""Durable Bot API receipt/offset and Telethon ingest watermark repositories."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import RowMapping, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    control_bot_cursors,
    control_bot_update_receipts,
    telegram_ingest_watermarks,
)
from telegram_userbot.domain.shared.time import require_aware
from telegram_userbot.platform.runtime.cursors import (
    ControlBotCursor,
    ControlUpdateClaim,
    ControlUpdateClaimOutcome,
    ControlUpdateDisposition,
    ControlUpdateReceipt,
    ControlUpdateSendState,
    ControlUpdateState,
    TelegramIngestWatermark,
)


def _control_cursor(row: RowMapping) -> ControlBotCursor:
    return ControlBotCursor(
        deployment_id=cast(str, row["deployment_id"]),
        bot_user_id=cast(int, row["bot_user_id"]),
        next_offset=cast(int, row["next_offset"]),
        version=cast(int, row["version"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def _control_receipt(row: RowMapping) -> ControlUpdateReceipt:
    disposition = cast(str | None, row["disposition"])
    return ControlUpdateReceipt(
        deployment_id=cast(str, row["deployment_id"]),
        bot_user_id=cast(int, row["bot_user_id"]),
        update_id=cast(int, row["update_id"]),
        state=ControlUpdateState(row["state"]),
        disposition=(None if disposition is None else ControlUpdateDisposition(disposition)),
        send_state=ControlUpdateSendState(row["send_state"]),
        owner_instance_id=cast(UUID, row["owner_instance_id"]),
        claimed_at=cast(datetime, row["claimed_at"]),
        lease_expires_at=cast(datetime, row["lease_expires_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        attempt_count=cast(int, row["attempt_count"]),
        version=cast(int, row["version"]),
    )


def _ingest_watermark(row: RowMapping) -> TelegramIngestWatermark:
    return TelegramIngestWatermark(
        account_id=cast(UUID, row["account_id"]),
        scope=cast(str, row["scope"]),
        pts=cast(int, row["pts"]),
        pts_count=cast(int, row["pts_count"]),
        update_identity=cast(str, row["update_identity"]),
        durable_ingested_at=cast(datetime, row["durable_ingested_at"]),
        version=cast(int, row["version"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


class RuntimeCursorRepository:
    """Persist content-free process cursors behind explicit CAS fences.

    Command mutations and ``complete_control_update`` belong in the same database
    transaction.  The offset advances only after every receipt known up to the
    selected post-batch update is completed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_control_cursor(
        self, *, deployment_id: str, bot_user_id: int
    ) -> ControlBotCursor | None:
        row = (
            (
                await self._session.execute(
                    select(control_bot_cursors).where(
                        control_bot_cursors.c.deployment_id == deployment_id,
                        control_bot_cursors.c.bot_user_id == bot_user_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _control_cursor(row)

    async def get_or_create_control_cursor(
        self,
        *,
        deployment_id: str,
        bot_user_id: int,
        initial_offset: int,
        now: datetime,
    ) -> ControlBotCursor:
        now = require_aware(now, "now")
        # Validate before constructing SQL or depending on a database CHECK.
        ControlBotCursor(deployment_id, bot_user_id, initial_offset, 1, now)
        await self._session.execute(
            postgresql_insert(control_bot_cursors)
            .values(
                deployment_id=deployment_id,
                bot_user_id=bot_user_id,
                next_offset=initial_offset,
                version=1,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    control_bot_cursors.c.deployment_id,
                    control_bot_cursors.c.bot_user_id,
                ]
            )
        )
        loaded = await self.load_control_cursor(
            deployment_id=deployment_id, bot_user_id=bot_user_id
        )
        if loaded is None:
            raise RuntimeError("control bot cursor insert was not observable")
        return loaded

    async def claim_control_update(  # noqa: PLR0913 - explicit durable identity/CAS boundary
        self,
        *,
        deployment_id: str,
        bot_user_id: int,
        update_id: int,
        owner_instance_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ControlUpdateClaim:
        now = require_aware(now, "now")
        lease_expires_at = require_aware(lease_expires_at, "lease_expires_at")
        if type(update_id) is not int or update_id < 0:
            raise ValueError("control update id is invalid")
        if not isinstance(owner_instance_id, UUID) or owner_instance_id.int == 0:
            raise ValueError("control update owner instance id is invalid")
        if lease_expires_at <= now:
            raise ValueError("control update lease must end after claim time")

        cursor_row = (
            (
                await self._session.execute(
                    select(control_bot_cursors)
                    .where(
                        control_bot_cursors.c.deployment_id == deployment_id,
                        control_bot_cursors.c.bot_user_id == bot_user_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if cursor_row is None:
            raise RuntimeError("control bot cursor must be initialized before claiming updates")
        cursor = _control_cursor(cursor_row)
        if update_id < cursor.next_offset:
            return ControlUpdateClaim(ControlUpdateClaimOutcome.BELOW_OFFSET, None)

        await self._session.execute(
            postgresql_insert(control_bot_update_receipts)
            .values(
                deployment_id=deployment_id,
                bot_user_id=bot_user_id,
                update_id=update_id,
                state=ControlUpdateState.CLAIMED.value,
                disposition=None,
                send_state=ControlUpdateSendState.NOT_REQUIRED.value,
                owner_instance_id=owner_instance_id,
                claimed_at=now,
                lease_expires_at=lease_expires_at,
                completed_at=None,
                attempt_count=1,
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    control_bot_update_receipts.c.deployment_id,
                    control_bot_update_receipts.c.bot_user_id,
                    control_bot_update_receipts.c.update_id,
                ]
            )
        )
        receipt_row = (
            (
                await self._session.execute(
                    select(control_bot_update_receipts)
                    .where(
                        control_bot_update_receipts.c.deployment_id == deployment_id,
                        control_bot_update_receipts.c.bot_user_id == bot_user_id,
                        control_bot_update_receipts.c.update_id == update_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        receipt = _control_receipt(receipt_row)
        if receipt.state is ControlUpdateState.COMPLETED:
            return ControlUpdateClaim(ControlUpdateClaimOutcome.COMPLETED, receipt)
        if receipt.owner_instance_id == owner_instance_id and receipt.lease_expires_at > now:
            return ControlUpdateClaim(ControlUpdateClaimOutcome.ACQUIRED, receipt)
        if receipt.lease_expires_at > now:
            return ControlUpdateClaim(ControlUpdateClaimOutcome.BUSY, receipt)

        reclaimed_row = (
            (
                await self._session.execute(
                    update(control_bot_update_receipts)
                    .where(
                        control_bot_update_receipts.c.deployment_id == deployment_id,
                        control_bot_update_receipts.c.bot_user_id == bot_user_id,
                        control_bot_update_receipts.c.update_id == update_id,
                        control_bot_update_receipts.c.state == ControlUpdateState.CLAIMED.value,
                        control_bot_update_receipts.c.version == receipt.version,
                    )
                    .values(
                        owner_instance_id=owner_instance_id,
                        claimed_at=now,
                        lease_expires_at=lease_expires_at,
                        attempt_count=control_bot_update_receipts.c.attempt_count + 1,
                        version=control_bot_update_receipts.c.version + 1,
                    )
                    .returning(*control_bot_update_receipts.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if reclaimed_row is None:
            raise RuntimeError("control update reclaim compare-and-set failed")
        return ControlUpdateClaim(
            ControlUpdateClaimOutcome.ACQUIRED, _control_receipt(reclaimed_row)
        )

    async def complete_control_update(  # noqa: PLR0913 - explicit durable identity/CAS boundary
        self,
        *,
        deployment_id: str,
        bot_user_id: int,
        update_id: int,
        owner_instance_id: UUID,
        expected_version: int,
        disposition: ControlUpdateDisposition,
        response_required: bool,
        now: datetime,
    ) -> ControlUpdateReceipt | None:
        now = require_aware(now, "now")
        if not isinstance(disposition, ControlUpdateDisposition):
            raise TypeError("control update disposition must use the stable vocabulary")
        if type(response_required) is not bool:
            raise TypeError("response_required must be a boolean")
        row = (
            (
                await self._session.execute(
                    update(control_bot_update_receipts)
                    .where(
                        control_bot_update_receipts.c.deployment_id == deployment_id,
                        control_bot_update_receipts.c.bot_user_id == bot_user_id,
                        control_bot_update_receipts.c.update_id == update_id,
                        control_bot_update_receipts.c.owner_instance_id == owner_instance_id,
                        control_bot_update_receipts.c.state == ControlUpdateState.CLAIMED.value,
                        control_bot_update_receipts.c.version == expected_version,
                    )
                    .values(
                        state=ControlUpdateState.COMPLETED.value,
                        disposition=disposition.value,
                        send_state=(
                            ControlUpdateSendState.PENDING.value
                            if response_required
                            else ControlUpdateSendState.NOT_REQUIRED.value
                        ),
                        completed_at=now,
                        version=control_bot_update_receipts.c.version + 1,
                    )
                    .returning(*control_bot_update_receipts.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _control_receipt(row)

    async def mark_control_response(
        self,
        *,
        deployment_id: str,
        bot_user_id: int,
        update_id: int,
        expected_version: int,
        send_state: ControlUpdateSendState,
    ) -> ControlUpdateReceipt | None:
        if send_state not in (
            ControlUpdateSendState.SENT,
            ControlUpdateSendState.NOT_SENT,
            ControlUpdateSendState.UNKNOWN,
        ):
            raise ValueError("control response terminal state is invalid")
        row = (
            (
                await self._session.execute(
                    update(control_bot_update_receipts)
                    .where(
                        control_bot_update_receipts.c.deployment_id == deployment_id,
                        control_bot_update_receipts.c.bot_user_id == bot_user_id,
                        control_bot_update_receipts.c.update_id == update_id,
                        control_bot_update_receipts.c.state == ControlUpdateState.COMPLETED.value,
                        control_bot_update_receipts.c.send_state
                        == ControlUpdateSendState.PENDING.value,
                        control_bot_update_receipts.c.version == expected_version,
                    )
                    .values(
                        send_state=send_state.value,
                        version=control_bot_update_receipts.c.version + 1,
                    )
                    .returning(*control_bot_update_receipts.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _control_receipt(row)

    async def advance_control_offset(
        self,
        *,
        deployment_id: str,
        bot_user_id: int,
        through_update_id: int,
        expected_version: int,
        now: datetime,
    ) -> ControlBotCursor | None:
        """Advance once after a batch; completed/ignored/rejected receipts all count."""

        now = require_aware(now, "now")
        if type(through_update_id) is not int or through_update_id < 0:
            raise ValueError("control update id is invalid")
        cursor_row = (
            (
                await self._session.execute(
                    select(control_bot_cursors)
                    .where(
                        control_bot_cursors.c.deployment_id == deployment_id,
                        control_bot_cursors.c.bot_user_id == bot_user_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if cursor_row is None:
            return None
        cursor = _control_cursor(cursor_row)
        desired_offset = through_update_id + 1
        if desired_offset <= cursor.next_offset:
            return cursor
        if cursor.version != expected_version:
            return None

        receipts = (
            (
                await self._session.execute(
                    select(
                        control_bot_update_receipts.c.update_id,
                        control_bot_update_receipts.c.state,
                    )
                    .where(
                        control_bot_update_receipts.c.deployment_id == deployment_id,
                        control_bot_update_receipts.c.bot_user_id == bot_user_id,
                        control_bot_update_receipts.c.update_id >= cursor.next_offset,
                        control_bot_update_receipts.c.update_id <= through_update_id,
                    )
                    .order_by(control_bot_update_receipts.c.update_id)
                )
            )
            .mappings()
            .all()
        )
        if (
            not receipts
            or cast(int, receipts[-1]["update_id"]) != through_update_id
            or any(row["state"] != ControlUpdateState.COMPLETED.value for row in receipts)
        ):
            return None
        updated_row = (
            (
                await self._session.execute(
                    update(control_bot_cursors)
                    .where(
                        control_bot_cursors.c.deployment_id == deployment_id,
                        control_bot_cursors.c.bot_user_id == bot_user_id,
                        control_bot_cursors.c.version == expected_version,
                    )
                    .values(
                        next_offset=desired_offset,
                        version=control_bot_cursors.c.version + 1,
                        updated_at=now,
                    )
                    .returning(*control_bot_cursors.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if updated_row is None else _control_cursor(updated_row)

    async def load_ingest_watermark(
        self, *, account_id: UUID, scope: str
    ) -> TelegramIngestWatermark | None:
        row = (
            (
                await self._session.execute(
                    select(telegram_ingest_watermarks).where(
                        telegram_ingest_watermarks.c.account_id == account_id,
                        telegram_ingest_watermarks.c.scope == scope,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _ingest_watermark(row)

    async def record_durable_ingest(  # noqa: PLR0911, PLR0913 - explicit CAS outcomes
        self,
        *,
        account_id: UUID,
        scope: str,
        pts: int,
        pts_count: int,
        update_identity: str,
        durable_ingested_at: datetime,
        expected_version: int | None,
    ) -> TelegramIngestWatermark | None:
        """Record only after durable input writes, in their same transaction."""

        durable_ingested_at = require_aware(durable_ingested_at, "durable_ingested_at")
        if expected_version is not None and (
            type(expected_version) is not int or expected_version <= 0
        ):
            raise ValueError("Telegram ingest expected version must be positive")
        candidate = TelegramIngestWatermark(
            account_id=account_id,
            scope=scope,
            pts=pts,
            pts_count=pts_count,
            update_identity=update_identity,
            durable_ingested_at=durable_ingested_at,
            version=1 if expected_version is None else expected_version + 1,
            updated_at=durable_ingested_at,
        )
        current_row = (
            (
                await self._session.execute(
                    select(telegram_ingest_watermarks)
                    .where(
                        telegram_ingest_watermarks.c.account_id == account_id,
                        telegram_ingest_watermarks.c.scope == scope,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if current_row is None:
            if expected_version is not None:
                return None
            row = (
                (
                    await self._session.execute(
                        postgresql_insert(telegram_ingest_watermarks)
                        .values(
                            account_id=account_id,
                            scope=scope,
                            pts=pts,
                            pts_count=pts_count,
                            update_identity=update_identity,
                            durable_ingested_at=durable_ingested_at,
                            version=1,
                            updated_at=durable_ingested_at,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                telegram_ingest_watermarks.c.account_id,
                                telegram_ingest_watermarks.c.scope,
                            ]
                        )
                        .returning(*telegram_ingest_watermarks.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                return _ingest_watermark(row)
            raced = await self.load_ingest_watermark(account_id=account_id, scope=scope)
            if raced is not None and (
                raced.pts == candidate.pts
                and raced.pts_count == candidate.pts_count
                and raced.update_identity == candidate.update_identity
            ):
                return raced
            return None

        current = _ingest_watermark(current_row)
        exact_replay = (
            current.pts == candidate.pts
            and current.pts_count == candidate.pts_count
            and current.update_identity == candidate.update_identity
        )
        if current.pts == candidate.pts:
            if exact_replay:
                return current
            raise ValueError("Telegram ingest pts replay conflicts with current watermark")
        if candidate.pts < current.pts:
            raise ValueError("Telegram ingest pts cannot move backwards")
        if candidate.durable_ingested_at < current.durable_ingested_at:
            raise ValueError("Telegram durable ingest time cannot move backwards")
        if expected_version != current.version:
            return None

        row = (
            (
                await self._session.execute(
                    update(telegram_ingest_watermarks)
                    .where(
                        telegram_ingest_watermarks.c.account_id == account_id,
                        telegram_ingest_watermarks.c.scope == scope,
                        telegram_ingest_watermarks.c.version == expected_version,
                    )
                    .values(
                        pts=pts,
                        pts_count=pts_count,
                        update_identity=update_identity,
                        durable_ingested_at=durable_ingested_at,
                        version=telegram_ingest_watermarks.c.version + 1,
                        updated_at=durable_ingested_at,
                    )
                    .returning(*telegram_ingest_watermarks.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _ingest_watermark(row)


__all__ = ["RuntimeCursorRepository"]
