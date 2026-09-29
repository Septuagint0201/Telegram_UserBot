"""PostgreSQL service-status projection and durable restore-gate repositories."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    deployment_restore_state,
    service_instances,
    service_status_events,
)
from telegram_userbot.domain.shared.time import require_aware
from telegram_userbot.platform.health.model import ServiceName
from telegram_userbot.platform.health.status import (
    RestoreGateRecord,
    RestoreGateState,
    RestoreVerification,
    ServiceHeartbeat,
    ServiceReadiness,
    ServiceStatusCode,
    ServiceStatusMetadata,
    ServiceStatusUpdate,
)


def _restore_gate(row: RowMapping) -> RestoreGateRecord:
    return RestoreGateRecord(
        deployment_id=cast(str, row["deployment_id"]),
        account_id=cast(UUID, row["account_id"]),
        state=RestoreGateState(row["gate_state"]),
        restore_generation=cast(int, row["restore_generation"]),
        verification=RestoreVerification(
            erasure_replay_verified=cast(bool, row["erasure_replay_verified"]),
            unknown_send_reconciled=cast(bool, row["unknown_send_reconciled"]),
            credentials_verified=cast(bool, row["credentials_verified"]),
            session_verified=cast(bool, row["session_verified"]),
        ),
        verified_at=cast(datetime | None, row["verified_at"]),
        version=cast(int, row["version"]),
    )


def _service_heartbeat(row: RowMapping) -> ServiceHeartbeat:
    return ServiceHeartbeat(
        instance_id=cast(UUID, row["instance_id"]),
        service_name=ServiceName(row["service_name"]),
        started_at=cast(datetime, row["started_at"]),
        heartbeat_at=cast(datetime, row["last_heartbeat_at"]),
        readiness=ServiceReadiness(row["readiness"]),
        status_code=ServiceStatusCode(row["status_code"]),
        schema_revision=cast(str, row["schema_revision"]),
        last_successful_operation_at=cast(datetime | None, row["last_successful_operation_at"]),
        metadata=ServiceStatusMetadata.from_mapping(cast(dict[str, object], row["metadata"])),
    )


class ServiceStatusRepository:
    """Write one service's projection and append events only for status changes."""

    def __init__(self, session: AsyncSession, service_name: ServiceName) -> None:
        if not isinstance(service_name, ServiceName):
            raise TypeError("service name must use the stable vocabulary")
        self._session = session
        self._service_name = service_name

    async def heartbeat(self, heartbeat: ServiceHeartbeat) -> ServiceStatusUpdate:
        if heartbeat.service_name is not self._service_name:
            raise ValueError("service heartbeat scope does not match repository")
        current = (
            (
                await self._session.execute(
                    select(service_instances)
                    .where(service_instances.c.instance_id == heartbeat.instance_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        metadata = dict(heartbeat.metadata.as_mapping())
        if current is None:
            await self._session.execute(
                insert(service_instances).values(
                    instance_id=heartbeat.instance_id,
                    service_name=heartbeat.service_name.value,
                    started_at=heartbeat.started_at,
                    last_heartbeat_at=heartbeat.heartbeat_at,
                    readiness=heartbeat.readiness.value,
                    status_code=heartbeat.status_code.value,
                    schema_revision=heartbeat.schema_revision,
                    last_successful_operation_at=heartbeat.last_successful_operation_at,
                    metadata_schema_version=1,
                    metadata=metadata,
                    version=1,
                )
            )
            await self._insert_event(
                heartbeat,
                event_kind="started",
                previous_readiness=None,
                previous_status_code=None,
            )
            return ServiceStatusUpdate(transition_recorded=True, version=1)

        if (
            current["service_name"] != heartbeat.service_name.value
            or current["started_at"] != heartbeat.started_at
            or current["schema_revision"] != heartbeat.schema_revision
            or current["last_heartbeat_at"] > heartbeat.heartbeat_at
        ):
            raise ValueError("service heartbeat conflicts with current projection")
        previous_success = cast(datetime | None, current["last_successful_operation_at"])
        if (
            previous_success is not None
            and heartbeat.last_successful_operation_at is not None
            and heartbeat.last_successful_operation_at < previous_success
        ):
            raise ValueError("last successful operation cannot move backwards")
        last_success = heartbeat.last_successful_operation_at or previous_success
        same_heartbeat = current["last_heartbeat_at"] == heartbeat.heartbeat_at
        exact_replay = (
            same_heartbeat
            and current["readiness"] == heartbeat.readiness.value
            and current["status_code"] == heartbeat.status_code.value
            and previous_success == last_success
            and current["metadata"] == metadata
        )
        if exact_replay:
            return ServiceStatusUpdate(
                transition_recorded=False, version=cast(int, current["version"])
            )
        if same_heartbeat or current["readiness"] == ServiceReadiness.STOPPED.value:
            raise ValueError("service heartbeat conflicts with terminal or replay state")
        transition = (
            current["readiness"] != heartbeat.readiness.value
            or current["status_code"] != heartbeat.status_code.value
        )
        version = cast(int, current["version"]) + 1
        result = await self._session.execute(
            update(service_instances)
            .where(
                service_instances.c.instance_id == heartbeat.instance_id,
                service_instances.c.version == current["version"],
            )
            .values(
                last_heartbeat_at=heartbeat.heartbeat_at,
                readiness=heartbeat.readiness.value,
                status_code=heartbeat.status_code.value,
                last_successful_operation_at=last_success,
                metadata=metadata,
                version=version,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise RuntimeError("service heartbeat compare-and-set failed")
        if transition:
            await self._insert_event(
                heartbeat,
                event_kind=(
                    "stopped"
                    if heartbeat.readiness is ServiceReadiness.STOPPED
                    else "status_changed"
                ),
                previous_readiness=cast(str, current["readiness"]),
                previous_status_code=cast(str, current["status_code"]),
            )
        return ServiceStatusUpdate(transition_recorded=transition, version=version)

    async def _insert_event(
        self,
        heartbeat: ServiceHeartbeat,
        *,
        event_kind: str,
        previous_readiness: str | None,
        previous_status_code: str | None,
    ) -> None:
        await self._session.execute(
            insert(service_status_events).values(
                instance_id=heartbeat.instance_id,
                service_name=heartbeat.service_name.value,
                event_kind=event_kind,
                previous_readiness=previous_readiness,
                readiness=heartbeat.readiness.value,
                previous_status_code=previous_status_code,
                status_code=heartbeat.status_code.value,
                schema_revision=heartbeat.schema_revision,
                occurred_at=heartbeat.heartbeat_at,
                metadata_schema_version=1,
                metadata=dict(heartbeat.metadata.as_mapping()),
            )
        )

    async def get(self, instance_id: UUID) -> ServiceHeartbeat | None:
        row = (
            (
                await self._session.execute(
                    select(service_instances).where(
                        service_instances.c.instance_id == instance_id,
                        service_instances.c.service_name == self._service_name.value,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _service_heartbeat(row)

    async def latest(self) -> ServiceHeartbeat | None:
        """Return the freshest current instance for this service scope."""

        row = (
            (
                await self._session.execute(
                    select(service_instances)
                    .where(service_instances.c.service_name == self._service_name.value)
                    .order_by(
                        service_instances.c.last_heartbeat_at.desc(),
                        service_instances.c.instance_id.desc(),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _service_heartbeat(row)


class RestoreGateRepository:
    """CAS-only durable restore gate for explicit maintenance workflows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, deployment_id: str) -> RestoreGateRecord | None:
        row = (
            (
                await self._session.execute(
                    select(deployment_restore_state).where(
                        deployment_restore_state.c.deployment_id == deployment_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _restore_gate(row)

    async def create_closed(
        self, *, deployment_id: str, account_id: UUID, now: datetime
    ) -> RestoreGateRecord:
        observed_at = require_aware(now, "now")
        await self._session.execute(
            postgresql_insert(deployment_restore_state)
            .values(
                deployment_id=deployment_id,
                account_id=account_id,
                gate_state=RestoreGateState.CLOSED.value,
                restore_generation=1,
                version=1,
                updated_at=observed_at,
            )
            .on_conflict_do_nothing(index_elements=[deployment_restore_state.c.deployment_id])
        )
        created = await self.get(deployment_id)
        if created is None:
            raise RuntimeError("created restore gate disappeared inside transaction")
        if created.account_id != account_id:
            raise ValueError("restore gate deployment identity conflicts with current state")
        return created

    async def reset_for_restore(
        self,
        *,
        deployment_id: str,
        account_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> RestoreGateRecord | None:
        observed_at = require_aware(now, "now")
        statement = (
            update(deployment_restore_state)
            .where(
                deployment_restore_state.c.deployment_id == deployment_id,
                deployment_restore_state.c.account_id == account_id,
                deployment_restore_state.c.version == expected_version,
            )
            .values(
                gate_state=RestoreGateState.VALIDATING.value,
                restore_generation=deployment_restore_state.c.restore_generation + 1,
                erasure_replay_verified=False,
                unknown_send_reconciled=False,
                credentials_verified=False,
                session_verified=False,
                verified_at=None,
                version=deployment_restore_state.c.version + 1,
                updated_at=observed_at,
            )
            .returning(*deployment_restore_state.c)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _restore_gate(row)

    async def record_verification(
        self,
        *,
        deployment_id: str,
        account_id: UUID,
        expected_version: int,
        verification: RestoreVerification,
        now: datetime,
    ) -> RestoreGateRecord | None:
        observed_at = require_aware(now, "now")
        statement = (
            update(deployment_restore_state)
            .where(
                deployment_restore_state.c.deployment_id == deployment_id,
                deployment_restore_state.c.account_id == account_id,
                deployment_restore_state.c.version == expected_version,
                deployment_restore_state.c.gate_state == RestoreGateState.VALIDATING.value,
            )
            .values(
                erasure_replay_verified=verification.erasure_replay_verified,
                unknown_send_reconciled=verification.unknown_send_reconciled,
                credentials_verified=verification.credentials_verified,
                session_verified=verification.session_verified,
                verified_at=observed_at if verification.complete else None,
                version=deployment_restore_state.c.version + 1,
                updated_at=observed_at,
            )
            .returning(*deployment_restore_state.c)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _restore_gate(row)

    async def open_if_verified(
        self,
        *,
        deployment_id: str,
        account_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> RestoreGateRecord | None:
        observed_at = require_aware(now, "now")
        statement = (
            update(deployment_restore_state)
            .where(
                deployment_restore_state.c.deployment_id == deployment_id,
                deployment_restore_state.c.account_id == account_id,
                deployment_restore_state.c.version == expected_version,
                deployment_restore_state.c.gate_state == RestoreGateState.VALIDATING.value,
                deployment_restore_state.c.erasure_replay_verified.is_(True),
                deployment_restore_state.c.unknown_send_reconciled.is_(True),
                deployment_restore_state.c.credentials_verified.is_(True),
                deployment_restore_state.c.session_verified.is_(True),
                deployment_restore_state.c.verified_at.is_not(None),
            )
            .values(
                gate_state=RestoreGateState.OPEN.value,
                version=deployment_restore_state.c.version + 1,
                updated_at=observed_at,
            )
            .returning(*deployment_restore_state.c)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _restore_gate(row)


__all__ = ["RestoreGateRepository", "ServiceStatusRepository"]
