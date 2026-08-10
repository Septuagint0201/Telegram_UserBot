"""Transactional repository for immutable model configuration and credentials."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import RowMapping, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.records import (
    ClaimedKeyLaunchRecord,
    ModelConfigSnapshotRecord,
    ModelControlDraftRecord,
    ModelControlProfileRecord,
    ModelProfileRecord,
)
from telegram_userbot.adapters.persistence.schema import (
    audit_log,
    control_input_sessions,
    model_capability_snapshots,
    model_config_drafts,
    model_config_versions,
    model_credential_versions,
    model_credentials,
    model_endpoints,
    model_key_launch_sessions,
    model_key_rate_limits,
    model_profiles,
    transactional_outbox,
)
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    profile_kind_for,
    validate_activation,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import (
    CredentialBinding,
    CredentialEnvelope,
    CredentialKeyring,
)


class ModelRepositoryError(RuntimeError):
    """Content-free repository conflict or invariant failure."""


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _config_values(config: CanonicalModelConfig) -> dict[str, object]:
    return {
        "endpoint_id": config.endpoint_id,
        "credential_id": config.credential_id,
        "protocol": config.protocol.value,
        "model_name": config.model_name,
        "temperature": config.temperature,
        "max_output_tokens": config.max_output_tokens,
        "timeout_seconds": config.timeout_seconds,
        "enabled": config.enabled,
        "protocol_options": dict(config.protocol_options),
    }


def _profile(row: RowMapping) -> ModelProfileRecord:
    return ModelProfileRecord(
        id=row["id"],
        logical_role=row["logical_role"],
        profile_kind=row["profile_kind"],
        state=row["state"],
        active_config_version_no=row["active_config_version_no"],
        version=row["version"],
        credential_id=row["credential_id"],
        credential_status=row["credential_status"],
        credential_active_version_no=row["credential_active_version_no"],
        credential_version=row["credential_version"],
    )


class ModelConfigurationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bootstrap_profiles(
        self,
        *,
        profile_ids: Mapping[LogicalRole, UUID],
        credential_ids: Mapping[LogicalRole, UUID],
    ) -> None:
        if set(profile_ids) != set(LogicalRole) or set(credential_ids) != set(LogicalRole):
            raise ModelRepositoryError("all logical roles require stable identities")
        for role in LogicalRole:
            await self._session.execute(
                postgresql_insert(model_profiles)
                .values(
                    id=profile_ids[role],
                    logical_role=role.value,
                    profile_kind=profile_kind_for(role).value,
                )
                .on_conflict_do_nothing(index_elements=[model_profiles.c.logical_role])
            )
            await self._session.execute(
                postgresql_insert(model_credentials)
                .values(id=credential_ids[role], profile_id=profile_ids[role])
                .on_conflict_do_nothing(index_elements=[model_credentials.c.profile_id])
            )

    async def list_profiles(self) -> tuple[ModelProfileRecord, ...]:
        statement = (
            select(
                *model_profiles.c,
                model_credentials.c.id.label("credential_id"),
                model_credentials.c.status.label("credential_status"),
                model_credentials.c.active_version_no.label("credential_active_version_no"),
                model_credentials.c.version.label("credential_version"),
            )
            .join(model_credentials, model_credentials.c.profile_id == model_profiles.c.id)
            .order_by(model_profiles.c.logical_role)
        )
        rows = (await self._session.execute(statement)).mappings()
        return tuple(_profile(row) for row in rows)

    async def list_control_profiles(self) -> tuple[ModelControlProfileRecord, ...]:
        statement = (
            select(
                model_profiles.c.id,
                model_profiles.c.logical_role,
                model_profiles.c.profile_kind,
                model_profiles.c.state,
                model_profiles.c.active_config_version_no,
                model_profiles.c.version,
                model_credentials.c.id.label("credential_id"),
                model_credentials.c.status.label("credential_status"),
                model_credentials.c.version.label("credential_version"),
                model_config_versions.c.protocol,
                model_config_versions.c.model_name,
                model_endpoints.c.label.label("endpoint_label"),
            )
            .join(model_credentials, model_credentials.c.profile_id == model_profiles.c.id)
            .outerjoin(
                model_config_versions,
                (model_config_versions.c.profile_id == model_profiles.c.id)
                & (model_config_versions.c.version_no == model_profiles.c.active_config_version_no),
            )
            .outerjoin(model_endpoints, model_endpoints.c.id == model_config_versions.c.endpoint_id)
            .order_by(model_profiles.c.logical_role)
        )
        rows = (await self._session.execute(statement)).mappings()
        return tuple(
            ModelControlProfileRecord(
                id=row["id"],
                logical_role=row["logical_role"],
                profile_kind=row["profile_kind"],
                state=row["state"],
                active_config_version_no=row["active_config_version_no"],
                version=row["version"],
                credential_id=row["credential_id"],
                credential_status=row["credential_status"],
                credential_version=row["credential_version"],
                protocol=row["protocol"],
                model_name=row["model_name"],
                endpoint_label=row["endpoint_label"],
            )
            for row in rows
        )

    async def start_control_session(  # noqa: PLR0913 - durable session contract
        self,
        *,
        session_id: UUID,
        draft_id: UUID,
        admin_id: int,
        logical_role: LogicalRole,
        now: datetime,
        expires_at: datetime,
        session_nonce_hash: bytes,
    ) -> ModelControlDraftRecord:
        if (
            len(session_nonce_hash) != 32
            or expires_at <= now
            or expires_at - now > timedelta(minutes=15)
        ):
            raise ModelRepositoryError("control session lifetime is invalid")
        profile = (
            (
                await self._session.execute(
                    select(
                        model_profiles,
                        model_credentials.c.id.label("credential_id"),
                    )
                    .join(model_credentials, model_credentials.c.profile_id == model_profiles.c.id)
                    .where(model_profiles.c.logical_role == logical_role.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise ModelRepositoryError("model profile is unavailable")
        previous = (
            (
                await self._session.execute(
                    select(control_input_sessions.c.id, control_input_sessions.c.draft_id)
                    .where(
                        control_input_sessions.c.admin_telegram_user_id == admin_id,
                        control_input_sessions.c.state == "active",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if previous:
            previous_session_ids = [row["id"] for row in previous]
            await self._session.execute(
                update(control_input_sessions)
                .where(control_input_sessions.c.id.in_(previous_session_ids))
                .values(state="cancelled", consumed_at=now)
            )
        matching_draft = (model_config_drafts.c.profile_id == profile["id"]) & (
            model_config_drafts.c.created_by_admin_id == admin_id
        )
        if previous:
            matching_draft = or_(
                matching_draft,
                model_config_drafts.c.id.in_([row["draft_id"] for row in previous]),
            )
        await self._session.execute(
            update(model_config_drafts)
            .where(
                matching_draft,
                model_config_drafts.c.state.in_(("editing", "validated")),
            )
            .values(state="cancelled", consumed_at=now, updated_at=now)
        )
        active = None
        if profile["active_config_version_no"] is not None:
            active = (
                (
                    await self._session.execute(
                        select(model_config_versions).where(
                            model_config_versions.c.profile_id == profile["id"],
                            model_config_versions.c.version_no
                            == profile["active_config_version_no"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        defaults: dict[str, object] = {
            "endpoint_id": None,
            "credential_id": profile["credential_id"],
            "protocol": (
                ModelProtocol.EMBEDDING.value
                if logical_role is LogicalRole.EMBEDDING
                else ModelProtocol.OPENAI_RESPONSES.value
            ),
            "model_name": None,
            "temperature": None if logical_role is LogicalRole.EMBEDDING else 0.2,
            "max_output_tokens": None if logical_role is LogicalRole.EMBEDDING else 1024,
            "timeout_seconds": 30,
            "enabled": False,
            "protocol_options": {},
        }
        if active is not None:
            defaults.update(
                {key: active[key] for key in defaults if key in active and key != "credential_id"}
            )
        await self._session.execute(
            insert(model_config_drafts).values(
                id=draft_id,
                profile_id=profile["id"],
                created_by_admin_id=admin_id,
                expected_profile_version=profile["version"],
                **defaults,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
        )
        await self._session.execute(
            insert(control_input_sessions).values(
                id=session_id,
                admin_telegram_user_id=admin_id,
                profile_id=profile["id"],
                draft_id=draft_id,
                expected_draft_version=1,
                pending_field="endpoint",
                session_nonce_hash=session_nonce_hash,
                created_at=now,
                expires_at=expires_at,
            )
        )
        return ModelControlDraftRecord(
            session_id=session_id,
            draft_id=draft_id,
            profile_id=profile["id"],
            logical_role=profile["logical_role"],
            profile_kind=profile["profile_kind"],
            credential_id=profile["credential_id"],
            expected_profile_version=profile["version"],
            draft_version=1,
            state="editing",
            pending_field="endpoint",
            endpoint_id=cast(UUID | None, defaults["endpoint_id"]),
            protocol=cast(str, defaults["protocol"]),
            model_name=cast(str | None, defaults["model_name"]),
            temperature=cast(float | None, defaults["temperature"]),
            max_output_tokens=cast(int | None, defaults["max_output_tokens"]),
            timeout_seconds=cast(int, defaults["timeout_seconds"]),
            enabled=cast(bool, defaults["enabled"]),
            protocol_options=cast(dict[str, object], defaults["protocol_options"]),
            capability_snapshot_id=None,
            expires_at=expires_at,
        )

    async def get_control_session(
        self, *, admin_id: int, now: datetime
    ) -> ModelControlDraftRecord | None:
        statement = (
            select(
                control_input_sessions.c.id.label("session_id"),
                control_input_sessions.c.pending_field,
                model_config_drafts,
                model_profiles.c.logical_role,
                model_profiles.c.profile_kind,
            )
            .join(
                model_config_drafts, model_config_drafts.c.id == control_input_sessions.c.draft_id
            )
            .join(model_profiles, model_profiles.c.id == control_input_sessions.c.profile_id)
            .where(
                control_input_sessions.c.admin_telegram_user_id == admin_id,
                control_input_sessions.c.state == "active",
                control_input_sessions.c.expires_at > now,
                model_config_drafts.c.state == "editing",
                model_config_drafts.c.expires_at > now,
            )
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _control_draft(row)

    async def cancel_control_session(self, *, admin_id: int, now: datetime) -> bool:
        rows = (
            (
                await self._session.execute(
                    select(control_input_sessions.c.id, control_input_sessions.c.draft_id)
                    .where(
                        control_input_sessions.c.admin_telegram_user_id == admin_id,
                        control_input_sessions.c.state == "active",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return False
        session_ids = [row["id"] for row in rows]
        draft_ids = [row["draft_id"] for row in rows]
        await self._session.execute(
            update(control_input_sessions)
            .where(control_input_sessions.c.id.in_(session_ids))
            .values(state="cancelled", consumed_at=now)
        )
        await self._session.execute(
            update(model_config_drafts)
            .where(
                model_config_drafts.c.id.in_(draft_ids),
                model_config_drafts.c.state.in_(("editing", "validated")),
            )
            .values(state="cancelled", consumed_at=now, updated_at=now)
        )
        return True

    async def advance_control_session(  # noqa: PLR0913 - explicit CAS contract
        self,
        *,
        session_id: UUID,
        draft_id: UUID,
        expected_draft_version: int,
        values: Mapping[str, object],
        next_field: str | None,
        now: datetime,
    ) -> int | None:
        allowed = {
            "endpoint_id",
            "protocol",
            "model_name",
            "temperature",
            "max_output_tokens",
            "timeout_seconds",
            "enabled",
            "protocol_options",
        }
        if not values or not set(values) <= allowed:
            raise ModelRepositoryError("control draft update is invalid")
        draft_version = (
            await self._session.execute(
                update(model_config_drafts)
                .where(
                    model_config_drafts.c.id == draft_id,
                    model_config_drafts.c.draft_version == expected_draft_version,
                    model_config_drafts.c.state == "editing",
                    model_config_drafts.c.expires_at > now,
                )
                .values(
                    **dict(values),
                    draft_version=model_config_drafts.c.draft_version + 1,
                    updated_at=now,
                    capability_snapshot_id=None,
                    validation_error_code=None,
                )
                .returning(model_config_drafts.c.draft_version)
            )
        ).scalar_one_or_none()
        if draft_version is None:
            return None
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(control_input_sessions)
                .where(
                    control_input_sessions.c.id == session_id,
                    control_input_sessions.c.draft_id == draft_id,
                    control_input_sessions.c.expected_draft_version == expected_draft_version,
                    control_input_sessions.c.state == "active",
                    control_input_sessions.c.expires_at > now,
                )
                .values(
                    expected_draft_version=draft_version,
                    pending_field=next_field,
                    state="active" if next_field is not None else "consumed",
                    consumed_at=None if next_field is not None else now,
                )
            ),
        )
        if result.rowcount != 1:
            raise ModelRepositoryError("control session version conflict")
        return cast(int, draft_version)

    async def get_latest_draft(
        self,
        *,
        admin_id: int,
        logical_role: LogicalRole,
        states: tuple[str, ...],
        now: datetime,
    ) -> ModelControlDraftRecord | None:
        statement = (
            select(
                control_input_sessions.c.id.label("session_id"),
                control_input_sessions.c.pending_field,
                model_config_drafts,
                model_profiles.c.logical_role,
                model_profiles.c.profile_kind,
            )
            .join(model_profiles, model_profiles.c.id == model_config_drafts.c.profile_id)
            .outerjoin(
                control_input_sessions,
                control_input_sessions.c.draft_id == model_config_drafts.c.id,
            )
            .where(
                model_config_drafts.c.created_by_admin_id == admin_id,
                model_profiles.c.logical_role == logical_role.value,
                model_config_drafts.c.state.in_(states),
                model_config_drafts.c.expires_at > now,
            )
            .order_by(model_config_drafts.c.updated_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        return None if row is None else _control_draft(row)

    async def add_endpoint(  # noqa: PLR0913 - explicit persisted endpoint contract
        self,
        *,
        endpoint_id: UUID,
        label: str,
        base_url: str,
        network_policy_id: UUID,
        network_policy_version: int,
        network_category: str,
        admin_id: int,
    ) -> None:
        await self._session.execute(
            insert(model_endpoints).values(
                id=endpoint_id,
                label=label,
                base_url=base_url,
                canonical_sha256=hashlib.sha256(base_url.encode()).digest(),
                network_policy_id=network_policy_id,
                network_policy_version=network_policy_version,
                network_category=network_category,
                created_by_admin_id=admin_id,
            )
        )

    async def ensure_endpoint(  # noqa: PLR0913 - immutable endpoint identity contract
        self,
        *,
        endpoint_id: UUID,
        label: str,
        base_url: str,
        network_policy_id: UUID,
        network_policy_version: int,
        network_category: str,
        admin_id: int,
    ) -> UUID:
        canonical_sha256 = hashlib.sha256(base_url.encode()).digest()
        inserted = (
            await self._session.execute(
                postgresql_insert(model_endpoints)
                .values(
                    id=endpoint_id,
                    label=label,
                    base_url=base_url,
                    canonical_sha256=canonical_sha256,
                    network_policy_id=network_policy_id,
                    network_policy_version=network_policy_version,
                    network_category=network_category,
                    created_by_admin_id=admin_id,
                )
                .on_conflict_do_nothing(constraint="uq_model_endpoints_canonical_policy")
                .returning(model_endpoints.c.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return cast(UUID, inserted)
        existing = (
            await self._session.execute(
                select(model_endpoints.c.id).where(
                    model_endpoints.c.canonical_sha256 == canonical_sha256,
                    model_endpoints.c.network_policy_id == network_policy_id,
                    model_endpoints.c.network_policy_version == network_policy_version,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise ModelRepositoryError("model endpoint conflict")
        return cast(UUID, existing)

    async def create_draft(  # noqa: PLR0913 - explicit draft ownership and lifetime
        self,
        *,
        draft_id: UUID,
        profile_id: UUID,
        expected_profile_version: int,
        admin_id: int,
        config: CanonicalModelConfig,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        if config.profile_id != profile_id:
            raise ModelRepositoryError("draft profile does not match configuration")
        await self._session.execute(
            insert(model_config_drafts).values(
                id=draft_id,
                profile_id=profile_id,
                created_by_admin_id=admin_id,
                expected_profile_version=expected_profile_version,
                **_config_values(config),
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
        )

    async def replace_draft(
        self,
        *,
        draft_id: UUID,
        expected_draft_version: int,
        config: CanonicalModelConfig,
        now: datetime,
    ) -> int | None:
        result = (
            await self._session.execute(
                update(model_config_drafts)
                .where(
                    model_config_drafts.c.id == draft_id,
                    model_config_drafts.c.profile_id == config.profile_id,
                    model_config_drafts.c.draft_version == expected_draft_version,
                    model_config_drafts.c.state == "editing",
                    model_config_drafts.c.expires_at > now,
                )
                .values(
                    **_config_values(config),
                    draft_version=model_config_drafts.c.draft_version + 1,
                    updated_at=now,
                    capability_snapshot_id=None,
                    validation_error_code=None,
                )
                .returning(model_config_drafts.c.draft_version)
            )
        ).scalar_one_or_none()
        return cast(int | None, result)

    async def record_capabilities(  # noqa: PLR0913 - immutable observation fields
        self,
        *,
        snapshot_id: UUID,
        endpoint_id: UUID,
        protocol: ModelProtocol,
        model_name: str,
        capabilities: ModelCapabilities,
        metadata: Mapping[str, object],
        observed_at: datetime,
        expires_at: datetime,
    ) -> None:
        await self._session.execute(
            insert(model_capability_snapshots).values(
                id=snapshot_id,
                endpoint_id=endpoint_id,
                protocol=protocol.value,
                model_name=model_name,
                supports_text=capabilities.supports_text,
                supports_temperature=capabilities.supports_temperature,
                supports_reasoning_effort=capabilities.supports_reasoning_effort,
                supports_image=capabilities.supports_images,
                supports_stream=capabilities.supports_streaming,
                supports_structured_output=capabilities.supports_structured_output,
                chat_token_limit_field=capabilities.chat_token_limit_field,
                max_context_tokens=capabilities.max_context_tokens,
                max_output_tokens_limit=capabilities.max_output_tokens_limit,
                supported_input_roles=sorted(capabilities.supported_input_roles),
                embedding_dimensions=sorted(capabilities.embedding_dimensions),
                status="valid",
                metadata=dict(metadata),
                observed_at=observed_at,
                expires_at=expires_at,
            )
        )

    async def validate_draft(
        self,
        *,
        draft_id: UUID,
        expected_draft_version: int,
        capability_snapshot_id: UUID,
        now: datetime,
    ) -> bool:
        row = (
            (
                await self._session.execute(
                    select(model_config_drafts, model_profiles.c.logical_role)
                    .join(model_profiles, model_profiles.c.id == model_config_drafts.c.profile_id)
                    .where(
                        model_config_drafts.c.id == draft_id,
                        model_config_drafts.c.draft_version == expected_draft_version,
                        model_config_drafts.c.state == "editing",
                        model_config_drafts.c.expires_at > now,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        config = _config_from_row(row["profile_id"], LogicalRole(row["logical_role"]), row)
        capability_row = (
            (
                await self._session.execute(
                    select(model_capability_snapshots).where(
                        model_capability_snapshots.c.id == capability_snapshot_id,
                        model_capability_snapshots.c.endpoint_id == config.endpoint_id,
                        model_capability_snapshots.c.protocol == config.protocol.value,
                        model_capability_snapshots.c.model_name == config.model_name,
                        model_capability_snapshots.c.status == "valid",
                        model_capability_snapshots.c.expires_at > now,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if capability_row is None:
            return False
        validate_activation(config, _capabilities_from_row(config, capability_row))
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(model_config_drafts)
                .where(
                    model_config_drafts.c.id == draft_id,
                    model_config_drafts.c.draft_version == expected_draft_version,
                )
                .values(
                    state="validated",
                    capability_snapshot_id=capability_snapshot_id,
                    validation_error_code=None,
                    draft_version=model_config_drafts.c.draft_version + 1,
                    updated_at=now,
                )
            ),
        )
        return result.rowcount == 1

    async def set_credential(  # noqa: PLR0913 - reviewed secret mutation boundary
        self,
        *,
        profile_id: UUID,
        logical_role: LogicalRole,
        expected_credential_version: int,
        secret: SensitiveValue[str],
        keyring: CredentialKeyring,
        now: datetime,
    ) -> int:
        row = (
            (
                await self._session.execute(
                    select(model_credentials)
                    .join(model_profiles, model_profiles.c.id == model_credentials.c.profile_id)
                    .where(
                        model_credentials.c.profile_id == profile_id,
                        model_credentials.c.version == expected_credential_version,
                        model_profiles.c.logical_role == logical_role.value,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ModelRepositoryError("credential version conflict")
        new_version_no = cast(int, row["latest_version_no"]) + 1
        binding = CredentialBinding(logical_role, profile_id, row["id"], new_version_no)
        envelope = keyring.encrypt(secret, binding=binding)
        await self._session.execute(
            insert(model_credential_versions).values(
                id=uuid7(),
                credential_id=row["id"],
                profile_id=profile_id,
                version_no=new_version_no,
                algorithm=envelope.algorithm,
                key_version=envelope.key_version,
                aad_schema_version=envelope.aad_schema_version,
                nonce=envelope.nonce,
                ciphertext=envelope.ciphertext,
                secret_fingerprint=envelope.secret_fingerprint,
                created_at=now,
            )
        )
        await self._session.execute(
            update(model_credentials)
            .where(model_credentials.c.id == row["id"])
            .values(
                status="active",
                active_version_no=new_version_no,
                latest_version_no=new_version_no,
                version=model_credentials.c.version + 1,
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(transactional_outbox).values(
                topic="model.credential.changed",
                aggregate_type="model_credential",
                aggregate_id=str(row["id"]),
                aggregate_version=row["version"] + 1,
                payload_schema_version=1,
                payload={
                    "profile_id": str(profile_id),
                    "credential_version_no": new_version_no,
                    "status": "active",
                },
                created_at=now,
            )
        )
        return cast(int, row["version"] + 1)

    async def delete_credential(
        self,
        *,
        profile_id: UUID,
        expected_credential_version: int,
        now: datetime,
    ) -> bool:
        row = (
            (
                await self._session.execute(
                    select(model_credentials)
                    .where(
                        model_credentials.c.profile_id == profile_id,
                        model_credentials.c.version == expected_credential_version,
                        model_credentials.c.status == "active",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        await self._session.execute(
            update(model_credential_versions)
            .where(
                model_credential_versions.c.credential_id == row["id"],
                model_credential_versions.c.destroyed_at.is_(None),
            )
            .values(
                nonce=None,
                ciphertext=None,
                secret_fingerprint=None,
                destroyed_at=now,
                destroy_reason="admin_delete",
            )
        )
        await self._session.execute(
            update(model_credentials)
            .where(model_credentials.c.id == row["id"])
            .values(
                status="deleted",
                active_version_no=None,
                version=model_credentials.c.version + 1,
                updated_at=now,
            )
        )
        await self._session.execute(
            update(model_profiles)
            .where(model_profiles.c.id == profile_id, model_profiles.c.state == "active")
            .values(
                state="disabled",
                version=model_profiles.c.version + 1,
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(transactional_outbox).values(
                topic="model.credential.changed",
                aggregate_type="model_credential",
                aggregate_id=str(row["id"]),
                aggregate_version=row["version"] + 1,
                payload_schema_version=1,
                payload={
                    "profile_id": str(profile_id),
                    "credential_version_no": None,
                    "status": "deleted",
                },
                created_at=now,
            )
        )
        return True

    async def activate_draft(
        self,
        *,
        draft_id: UUID,
        expected_draft_version: int,
        admin_id: int,
        now: datetime,
    ) -> ModelConfigSnapshotRecord:
        draft = (
            (
                await self._session.execute(
                    select(model_config_drafts)
                    .where(
                        model_config_drafts.c.id == draft_id,
                        model_config_drafts.c.draft_version == expected_draft_version,
                        model_config_drafts.c.state == "validated",
                        model_config_drafts.c.expires_at > now,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if draft is None:
            raise ModelRepositoryError("validated draft is unavailable")
        profile = (
            (
                await self._session.execute(
                    select(model_profiles)
                    .where(model_profiles.c.id == draft["profile_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if profile["version"] != draft["expected_profile_version"]:
            raise ModelRepositoryError("profile version conflict")
        config = _config_from_row(profile["id"], LogicalRole(profile["logical_role"]), draft)
        capability_row = (
            (
                await self._session.execute(
                    select(model_capability_snapshots).where(
                        model_capability_snapshots.c.id == draft["capability_snapshot_id"],
                        model_capability_snapshots.c.endpoint_id == config.endpoint_id,
                        model_capability_snapshots.c.protocol == config.protocol.value,
                        model_capability_snapshots.c.model_name == config.model_name,
                        model_capability_snapshots.c.status == "valid",
                        model_capability_snapshots.c.expires_at > now,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if capability_row is None:
            raise ModelRepositoryError("validated capability is unavailable")
        validate_activation(config, _capabilities_from_row(config, capability_row))
        credential = (
            (
                await self._session.execute(
                    select(model_credentials)
                    .where(
                        model_credentials.c.id == config.credential_id,
                        model_credentials.c.profile_id == profile["id"],
                        model_credentials.c.status == "active",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if credential is None or credential["active_version_no"] is None:
            raise ModelRepositoryError("active credential is unavailable")
        next_version = (profile["active_config_version_no"] or 0) + 1
        payload = config.canonical_payload()
        digest = hashlib.sha256(_canonical_bytes(payload)).digest()
        config_id = uuid7()
        await self._session.execute(
            insert(model_config_versions).values(
                id=config_id,
                profile_id=profile["id"],
                profile_kind=profile["profile_kind"],
                version_no=next_version,
                source_draft_id=draft_id,
                capability_snapshot_id=draft["capability_snapshot_id"],
                **_config_values(config),
                config_sha256=digest,
                created_by_admin_id=admin_id,
                validated_at=now,
                created_at=now,
            )
        )
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(model_profiles)
                .where(
                    model_profiles.c.id == profile["id"],
                    model_profiles.c.version == draft["expected_profile_version"],
                )
                .values(
                    state="active" if config.enabled else "disabled",
                    active_config_version_no=next_version,
                    version=model_profiles.c.version + 1,
                    updated_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            raise ModelRepositoryError("profile version conflict")
        await self._session.execute(
            update(model_config_drafts)
            .where(
                model_config_drafts.c.id == draft_id,
                model_config_drafts.c.draft_version == expected_draft_version,
            )
            .values(
                state="activated",
                consumed_at=now,
                draft_version=model_config_drafts.c.draft_version + 1,
                updated_at=now,
            )
        )
        await self._session.execute(
            insert(transactional_outbox).values(
                topic="model.config.activated",
                aggregate_type="model_profile",
                aggregate_id=str(profile["id"]),
                aggregate_version=profile["version"] + 1,
                payload_schema_version=1,
                payload={
                    "profile_id": str(profile["id"]),
                    "config_version_no": next_version,
                    "state": "active" if config.enabled else "disabled",
                },
                created_at=now,
            )
        )
        return ModelConfigSnapshotRecord(
            config_id,
            profile["id"],
            next_version,
            config.endpoint_id,
            credential["id"],
            credential["active_version_no"],
            draft["capability_snapshot_id"],
            payload,
            digest,
        )

    async def get_active_snapshot(self, profile_id: UUID) -> ModelConfigSnapshotRecord | None:
        statement = (
            select(
                model_config_versions,
                model_profiles.c.logical_role,
                model_credentials.c.active_version_no.label("credential_active_version_no"),
            )
            .join(
                model_profiles,
                (model_profiles.c.id == model_config_versions.c.profile_id)
                & (model_profiles.c.active_config_version_no == model_config_versions.c.version_no),
            )
            .join(
                model_credentials,
                (model_credentials.c.id == model_config_versions.c.credential_id)
                & (model_credentials.c.profile_id == model_config_versions.c.profile_id)
                & (model_credentials.c.status == "active")
                & (model_credentials.c.active_version_no.is_not(None)),
            )
        )
        row = (
            (await self._session.execute(statement.where(model_profiles.c.id == profile_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ModelConfigSnapshotRecord(
            row["id"],
            row["profile_id"],
            row["version_no"],
            row["endpoint_id"],
            row["credential_id"],
            row["credential_active_version_no"],
            row["capability_snapshot_id"],
            _payload_from_row(row),
            row["config_sha256"],
        )

    async def load_credential_envelope(
        self, *, profile_id: UUID, credential_version_no: int
    ) -> tuple[UUID, CredentialEnvelope] | None:
        row = (
            (
                await self._session.execute(
                    text(
                        "SELECT * FROM get_model_credential_version("
                        ":profile_id, :credential_version_no)"
                    ).bindparams(
                        profile_id=profile_id,
                        credential_version_no=credential_version_no,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return (
            row["credential_id"],
            CredentialEnvelope(
                ciphertext=row["ciphertext"],
                nonce=row["nonce"],
                key_version=row["key_version"],
                aad_schema_version=row["aad_schema_version"],
                secret_fingerprint=row["secret_fingerprint"],
                algorithm=row["algorithm"],
            ),
        )

    async def create_key_launch(  # noqa: PLR0913 - immutable launch contract
        self,
        *,
        launch_id: UUID,
        token_hash: bytes,
        admin_id: int,
        profile_id: UUID,
        action: str,
        deployment_version: int,
        expected_credential_version: int,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        if len(token_hash) != 32 or expires_at <= now or expires_at - now > timedelta(minutes=5):
            raise ModelRepositoryError("key launch lifetime is invalid")
        await self._session.execute(
            insert(model_key_launch_sessions).values(
                id=launch_id,
                token_hash=token_hash,
                admin_telegram_user_id=admin_id,
                profile_id=profile_id,
                allowed_action=action,
                deployment_version=deployment_version,
                expected_credential_version=expected_credential_version,
                created_at=now,
                expires_at=expires_at,
            )
        )

    async def claim_key_launch(  # noqa: PLR0913 - authenticated one-time claim
        self,
        *,
        token_hash: bytes,
        admin_id: int,
        logical_role: LogicalRole,
        action: str,
        deployment_version: int,
        now: datetime,
    ) -> ClaimedKeyLaunchRecord | None:
        statement = (
            select(
                model_key_launch_sessions,
                model_profiles.c.logical_role,
                model_credentials.c.id.label("credential_id"),
                model_credentials.c.status.label("credential_status"),
                model_credentials.c.version.label("credential_version"),
            )
            .join(model_profiles, model_profiles.c.id == model_key_launch_sessions.c.profile_id)
            .join(model_credentials, model_credentials.c.profile_id == model_profiles.c.id)
            .where(
                model_key_launch_sessions.c.token_hash == token_hash,
                model_key_launch_sessions.c.admin_telegram_user_id == admin_id,
                model_key_launch_sessions.c.allowed_action == action,
                model_key_launch_sessions.c.deployment_version == deployment_version,
                model_key_launch_sessions.c.consumed_at.is_(None),
                model_key_launch_sessions.c.expires_at > now,
                model_profiles.c.logical_role == logical_role.value,
                model_credentials.c.version
                == model_key_launch_sessions.c.expected_credential_version,
            )
            .with_for_update()
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        if (action == "set" and row["credential_status"] == "active") or (
            action == "replace" and row["credential_status"] != "active"
        ):
            return None
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(model_key_launch_sessions)
                .where(
                    model_key_launch_sessions.c.id == row["id"],
                    model_key_launch_sessions.c.consumed_at.is_(None),
                )
                .values(
                    consumed_at=now,
                    attempt_count=model_key_launch_sessions.c.attempt_count + 1,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        return ClaimedKeyLaunchRecord(
            row["profile_id"],
            row["logical_role"],
            row["credential_id"],
            row["credential_status"],
            row["credential_version"],
        )

    async def allow_key_attempt(
        self,
        *,
        principal_hash: bytes,
        now: datetime,
        limit: int = 5,
        window: timedelta = timedelta(minutes=15),
        block: timedelta = timedelta(minutes=15),
    ) -> bool:
        if len(principal_hash) != 32 or limit < 1:
            raise ModelRepositoryError("key rate-limit input is invalid")
        row = (
            (
                await self._session.execute(
                    select(model_key_rate_limits)
                    .where(model_key_rate_limits.c.principal_hash == principal_hash)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            await self._session.execute(
                insert(model_key_rate_limits).values(
                    principal_hash=principal_hash,
                    window_started_at=now,
                    attempt_count=1,
                )
            )
            return True
        if row["blocked_until"] is not None and row["blocked_until"] > now:
            return False
        if row["window_started_at"] + window <= now:
            await self._session.execute(
                update(model_key_rate_limits)
                .where(model_key_rate_limits.c.principal_hash == principal_hash)
                .values(
                    window_started_at=now,
                    attempt_count=1,
                    blocked_until=None,
                    version=model_key_rate_limits.c.version + 1,
                )
            )
            return True
        allowed = cast(int, row["attempt_count"]) < limit
        await self._session.execute(
            update(model_key_rate_limits)
            .where(model_key_rate_limits.c.principal_hash == principal_hash)
            .values(
                attempt_count=model_key_rate_limits.c.attempt_count + 1,
                blocked_until=None if allowed else now + block,
                version=model_key_rate_limits.c.version + 1,
            )
        )
        return allowed

    async def audit_credential_mutation(  # noqa: PLR0913 - explicit audit contract
        self,
        *,
        admin_id: int,
        logical_role: LogicalRole,
        action: str,
        result: str,
        credential_cas_version: int,
        request_id: UUID,
        now: datetime,
    ) -> None:
        if action not in {"set", "replace", "delete"} or result not in {
            "success",
            "rejected",
            "failed",
        }:
            raise ModelRepositoryError("credential audit input is invalid")
        await self._session.execute(
            insert(audit_log).values(
                occurred_at=now,
                actor_type="admin",
                actor_ref=str(admin_id),
                action=f"model_credential_{action}",
                target_type="model_profile",
                target_id=logical_role.value,
                result=result,
                request_id=request_id,
                metadata_schema_version=1,
                metadata={"credential_cas_version": credential_cas_version},
            )
        )


def _control_draft(raw: Mapping[str, object] | RowMapping) -> ModelControlDraftRecord:
    temperature = raw["temperature"]
    return ModelControlDraftRecord(
        session_id=cast(UUID | None, raw.get("session_id")),
        draft_id=cast(UUID, raw["id"]),
        profile_id=cast(UUID, raw["profile_id"]),
        logical_role=str(raw["logical_role"]),
        profile_kind=str(raw["profile_kind"]),
        credential_id=cast(UUID, raw["credential_id"]),
        expected_profile_version=cast(int, raw["expected_profile_version"]),
        draft_version=cast(int, raw["draft_version"]),
        state=str(raw["state"]),
        pending_field=cast(str | None, raw.get("pending_field")),
        endpoint_id=cast(UUID | None, raw["endpoint_id"]),
        protocol=cast(str | None, raw["protocol"]),
        model_name=cast(str | None, raw["model_name"]),
        temperature=None if temperature is None else float(cast(Any, temperature)),
        max_output_tokens=cast(int | None, raw["max_output_tokens"]),
        timeout_seconds=cast(int | None, raw["timeout_seconds"]),
        enabled=cast(bool | None, raw["enabled"]),
        protocol_options=dict(cast(Mapping[str, object], raw["protocol_options"])),
        capability_snapshot_id=cast(UUID | None, raw["capability_snapshot_id"]),
        expires_at=cast(datetime, raw["expires_at"]),
    )


def _config_from_row(
    profile_id: UUID,
    logical_role: LogicalRole,
    raw: Mapping[str, object] | RowMapping,
) -> CanonicalModelConfig:
    return CanonicalModelConfig(
        profile_id=profile_id,
        logical_role=logical_role,
        endpoint_id=cast(UUID, raw["endpoint_id"]),
        credential_id=cast(UUID, raw["credential_id"]),
        protocol=ModelProtocol(str(raw["protocol"])),
        model_name=str(raw["model_name"]),
        temperature=None if raw["temperature"] is None else float(cast(Any, raw["temperature"])),
        max_output_tokens=cast(int | None, raw["max_output_tokens"]),
        timeout_seconds=cast(int, raw["timeout_seconds"]),
        enabled=cast(bool, raw["enabled"]),
        protocol_options=cast(Mapping[str, object], raw.get("protocol_options", {})),
    )


def _payload_from_row(raw: Mapping[str, object] | RowMapping) -> dict[str, Any]:
    return CanonicalModelConfig(
        profile_id=cast(UUID, raw["profile_id"]),
        logical_role=LogicalRole(str(raw["logical_role"])),
        endpoint_id=cast(UUID, raw["endpoint_id"]),
        credential_id=cast(UUID, raw["credential_id"]),
        protocol=ModelProtocol(str(raw["protocol"])),
        model_name=str(raw["model_name"]),
        temperature=None if raw["temperature"] is None else float(cast(Any, raw["temperature"])),
        max_output_tokens=cast(int | None, raw["max_output_tokens"]),
        timeout_seconds=cast(int, raw["timeout_seconds"]),
        enabled=cast(bool, raw["enabled"]),
        protocol_options=cast(Mapping[str, object], raw["protocol_options"]),
    ).canonical_payload()


def _capabilities_from_row(
    config: CanonicalModelConfig,
    raw: Mapping[str, object] | RowMapping,
) -> ModelCapabilities:
    return ModelCapabilities(
        profile_kind=profile_kind_for(config.logical_role),
        supported_protocols=frozenset({ModelProtocol(str(raw["protocol"]))}),
        supports_text=cast(bool, raw["supports_text"]),
        supports_temperature=cast(bool, raw["supports_temperature"]),
        supports_structured_output=cast(bool, raw["supports_structured_output"]),
        supports_streaming=cast(bool, raw["supports_stream"]),
        supports_images=cast(bool, raw["supports_image"]),
        max_context_tokens=cast(int, raw["max_context_tokens"]),
        max_output_tokens_limit=cast(int | None, raw["max_output_tokens_limit"]),
        supported_input_roles=frozenset(cast(list[str], raw["supported_input_roles"])),
        chat_token_limit_field=cast(str | None, raw["chat_token_limit_field"]),
        embedding_dimensions=frozenset(cast(list[int], raw["embedding_dimensions"])),
        supports_reasoning_effort=cast(bool, raw["supports_reasoning_effort"]),
    )
