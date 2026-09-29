"""Durable embedding inputs, lease fences, and current-source validation.

Only IDs, hashes and model versions enter the background-job payload. Provider
text and secrets live only in the returned in-memory snapshot.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Text, and_, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.model_runtime import (
    RuntimeEndpointSnapshot,
    canonical_config_from_row,
    model_config_digest,
)
from telegram_userbot.adapters.persistence.model_snapshots import capability_snapshot_digest
from telegram_userbot.adapters.persistence.records import JobRecord
from telegram_userbot.domain.memory.embedding import chunk_text
from telegram_userbot.domain.model_config import CanonicalModelConfig, LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto.credentials import CredentialBinding, CredentialEnvelope


class EmbeddingRuntimeError(RuntimeError):
    """Stable code safe to store in the durable retry journal."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedEmbedding:
    record_id: UUID
    snapshot: dict[str, Any]
    config: CanonicalModelConfig
    endpoint: RuntimeEndpointSnapshot
    binding: CredentialBinding
    envelope: CredentialEnvelope = field(repr=False)
    content: SensitiveValue[str] = field(repr=False)


def embedding_job_id(record_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"telegram-userbot:embedding-record:{record_id}")


def checked_vector(
    vector: tuple[float, ...], *, dimensions: int, normalization: str
) -> list[float]:
    if len(vector) != dimensions or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in vector
    ):
        raise EmbeddingRuntimeError("EMBEDDING_VECTOR_INVALID")
    if normalization == "none":
        return list(vector)
    if normalization != "l2":
        raise EmbeddingRuntimeError("EMBEDDING_NORMALIZATION_UNSUPPORTED")
    # hypot scales its input; squaring large finite values would overflow.
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingRuntimeError("EMBEDDING_VECTOR_INVALID")
    return [value / norm for value in vector]


class EmbeddingRuntimeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stage_target(
        self,
        *,
        account_id: UUID,
        target_kind: str,
        target_id: UUID,
        now: datetime,
        space_id: UUID | None = None,
    ) -> int:
        """Produce deterministic pending chunks in configured spaces, without activation."""
        if target_kind not in {"memory_version", "summary_version", "message_revision"}:
            raise ValueError("embedding target kind is invalid")
        spaces = (
            (
                await self._session.execute(
                    select(s.embedding_spaces)
                    .where(
                        s.embedding_spaces.c.account_id == account_id,
                        s.embedding_spaces.c.state.in_(("building", "active")),
                        s.embedding_spaces.c.chunker_version == "v1",
                        s.embedding_spaces.c.id == space_id if space_id else true(),
                    )
                    .order_by(s.embedding_spaces.c.id)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not spaces:
            return 0
        body = await MemoryRepository(self._session).load_current_embedding_target(
            target_kind=target_kind,
            target_id=target_id,
            account_id=account_id,
        )
        count = 0
        for space in spaces:
            for chunk in chunk_text(body):
                result = await self._session.scalar(
                    insert(s.embedding_records)
                    .values(
                        id=uuid5(space["id"], f"{target_kind}:{target_id}:{chunk.index}"),
                        account_id=account_id,
                        embedding_space_id=space["id"],
                        **{f"{target_kind}_id": target_id},
                        chunk_index=chunk.index,
                        chunker_version="v1",
                        source_sha256=chunk.source_sha256,
                        vector_payload=[],
                        dimensions=space["dimensions"],
                        state="pending",
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                    .returning(s.embedding_records.c.id)
                )
                count += int(result is not None)
        return count

    async def enqueue_pending(self, *, now: datetime, limit: int = 100) -> int:
        """Compensate lost wakeups without resetting terminal retry budgets."""
        if not 1 <= limit <= 1000:
            raise ValueError("embedding compensation limit is invalid")
        rows = (
            (
                await self._session.execute(
                    select(s.embedding_records, s.background_jobs.c.state.label("job_state"))
                    .outerjoin(
                        s.background_jobs,
                        and_(
                            s.background_jobs.c.account_id == s.embedding_records.c.account_id,
                            s.background_jobs.c.job_type == "embedding.compute",
                            s.background_jobs.c.payload["record_id"].astext
                            == s.embedding_records.c.id.cast(Text),
                        ),
                    )
                    .where(
                        s.embedding_records.c.state == "pending",
                        or_(
                            s.background_jobs.c.id.is_(None),
                            s.background_jobs.c.state == "dead_letter",
                        ),
                    )
                    .order_by(s.embedding_records.c.created_at, s.embedding_records.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        count = 0
        for row in rows:
            record_id, account_id = row["id"], row["account_id"]
            target = await self._target_scope(row)
            await self._session.execute(
                select(s.accounts.c.id)
                .where(s.accounts.c.id == account_id)
                .with_for_update(key_share=True)
            )
            if row["job_state"] == "dead_letter":
                await self._session.execute(
                    update(s.embedding_records)
                    .where(
                        s.embedding_records.c.id == record_id,
                        s.embedding_records.c.state == "pending",
                    )
                    .values(state="failed", vector_payload=[])
                )
                continue
            try:
                await self._source(row, conversation_id=str(target["conversation_id"]))
            except EmbeddingRuntimeError:
                # Erasure intent must not block the same compensation transaction
                # that schedules its cleanup. Do not create a new wakeup for it.
                await self._session.execute(
                    update(s.embedding_records)
                    .where(
                        s.embedding_records.c.id == record_id,
                        s.embedding_records.c.state == "pending",
                    )
                    .values(state="invalidated", vector_payload=[], invalidated_at=now)
                )
                continue
            result = await self._session.scalar(
                insert(s.background_jobs)
                .values(
                    id=embedding_job_id(record_id),
                    account_id=account_id,
                    queue_name="worker",
                    job_type="embedding.compute",
                    idempotency_key=hashlib.sha256(b"embedding:" + record_id.bytes).digest(),
                    payload_schema_version=1,
                    payload={
                        "record_id": str(record_id),
                        "conversation_id": str(target["conversation_id"]),
                    },
                    available_at=now,
                    max_attempts=5,
                )
                .on_conflict_do_nothing()
                .returning(s.background_jobs.c.id)
            )
            count += int(result is not None)
        return count

    async def _fenced_payload(self, job: JobRecord, now: datetime) -> dict[str, Any]:
        if job.account_id is None or job.job_type != "embedding.compute":
            raise EmbeddingRuntimeError("EMBEDDING_JOB_SCOPE_INVALID")
        # Same lock order/serialization point as erasure admission. Never keep
        # this transaction open over provider HTTP.
        await self._session.execute(
            select(s.accounts.c.id)
            .where(s.accounts.c.id == job.account_id)
            .with_for_update(key_share=True)
        )
        payload = await self._session.scalar(
            select(s.background_jobs.c.payload)
            .where(
                s.background_jobs.c.id == job.id,
                s.background_jobs.c.account_id == job.account_id,
                s.background_jobs.c.job_type == "embedding.compute",
                s.background_jobs.c.state == "leased",
                s.background_jobs.c.lease_owner == job.lease_owner,
                s.background_jobs.c.fencing_token == job.fencing_token,
                s.background_jobs.c.lease_expires_at > now,
            )
            .with_for_update()
        )
        if payload is None:
            raise EmbeddingRuntimeError("WORKER_JOB_FENCE_LOST", retryable=True)
        return cast(dict[str, Any], payload)

    @staticmethod
    def _record_id(payload: dict[str, Any]) -> UUID:
        if set(payload) not in (
            {"record_id", "conversation_id"},
            {"record_id", "conversation_id", "snapshot"},
        ):
            raise EmbeddingRuntimeError("EMBEDDING_JOB_PAYLOAD_INVALID")
        try:
            value = UUID(payload["record_id"])
        except ValueError, TypeError, AttributeError:
            raise EmbeddingRuntimeError("EMBEDDING_JOB_PAYLOAD_INVALID") from None
        if not value.int:
            raise EmbeddingRuntimeError("EMBEDDING_JOB_PAYLOAD_INVALID")
        return value

    async def _record(self, record_id: UUID, account_id: UUID | None) -> Any:
        return (
            (
                await self._session.execute(
                    select(s.embedding_records)
                    .where(
                        s.embedding_records.c.id == record_id,
                        s.embedding_records.c.account_id == account_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _target_scope(self, row: Any) -> Any:
        kind = next(
            key
            for key in ("memory_version", "summary_version", "message_revision")
            if row[f"{key}_id"] is not None
        )
        if kind == "memory_version":
            scope = (
                select(s.memories.c.conversation_id, s.memories.c.id.label("memory_id"))
                .join(s.memory_versions, s.memory_versions.c.memory_id == s.memories.c.id)
                .where(s.memory_versions.c.id == row["memory_version_id"])
            )
        elif kind == "summary_version":
            scope = (
                select(s.summaries.c.conversation_id)
                .join(s.summary_versions, s.summary_versions.c.summary_id == s.summaries.c.id)
                .where(s.summary_versions.c.id == row["summary_version_id"])
            )
        else:
            scope = (
                select(s.messages.c.conversation_id)
                .join(s.message_revisions, s.message_revisions.c.message_id == s.messages.c.id)
                .where(s.message_revisions.c.id == row["message_revision_id"])
            )
        target = (await self._session.execute(scope)).mappings().one_or_none()
        if target is None:
            raise EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE")
        return target

    async def _source(self, row: Any, *, conversation_id: str) -> str:
        target = await self._target_scope(row)
        if str(target["conversation_id"]) != conversation_id:
            raise EmbeddingRuntimeError("EMBEDDING_JOB_SCOPE_INVALID")
        kind = next(
            key
            for key in ("memory_version", "summary_version", "message_revision")
            if row[f"{key}_id"] is not None
        )
        contact_id = await self._session.scalar(
            select(s.conversations.c.contact_id).where(
                s.conversations.c.id == target["conversation_id"],
                s.conversations.c.account_id == row["account_id"],
            )
        )
        erased = await self._session.scalar(
            select(s.data_erasure_requests.c.id)
            .where(
                s.data_erasure_requests.c.account_id == row["account_id"],
                or_(
                    s.data_erasure_requests.c.scope_type == "account",
                    and_(
                        s.data_erasure_requests.c.scope_type == "contact",
                        s.data_erasure_requests.c.contact_id == contact_id,
                    ),
                    and_(
                        s.data_erasure_requests.c.scope_type == "memory",
                        s.data_erasure_requests.c.memory_id == target.get("memory_id"),
                    ),
                ),
            )
            .limit(1)
        )
        if erased is not None:
            raise EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE")
        try:
            content = await MemoryRepository(self._session).load_current_embedding_target(
                target_kind=kind, target_id=row[f"{kind}_id"], account_id=row["account_id"]
            )
        except ValueError:
            raise EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE") from None
        chunks = chunk_text(content)
        index = cast(int, row["chunk_index"])
        if row["chunker_version"] != "v1" or index >= len(chunks):
            raise EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE")
        if chunks[index].source_sha256 != row["source_sha256"]:
            raise EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE")
        return chunks[index].text

    async def prepare(  # noqa: PLR0912 - each admission check fails closed
        self, job: JobRecord, *, now: datetime
    ) -> PreparedEmbedding | None:
        payload = await self._fenced_payload(job, now)
        record_id = self._record_id(payload)
        if job.id != embedding_job_id(record_id):
            raise EmbeddingRuntimeError("EMBEDDING_JOB_SCOPE_INVALID")
        row = await self._record(record_id, job.account_id)
        if row is None:
            raise EmbeddingRuntimeError("EMBEDDING_JOB_SCOPE_INVALID")
        if row["state"] != "pending":
            return None
        content = await self._source(row, conversation_id=payload["conversation_id"])
        space = (
            (
                await self._session.execute(
                    select(s.embedding_spaces)
                    .where(
                        s.embedding_spaces.c.id == row["embedding_space_id"],
                        s.embedding_spaces.c.account_id == job.account_id,
                        s.embedding_spaces.c.state.in_(("active", "building")),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if space is None or space["chunker_version"] != row["chunker_version"]:
            raise EmbeddingRuntimeError("EMBEDDING_SPACE_UNAVAILABLE")
        config_row = (
            (
                await self._session.execute(
                    select(
                        s.model_config_versions,
                        s.model_profiles.c.logical_role,
                        s.model_profiles.c.id.label("model_profile_id"),
                        s.model_profiles.c.state.label("profile_state"),
                        s.model_credentials.c.active_version_no.label("credential_version_no"),
                        s.model_credentials.c.status.label("credential_status"),
                    )
                    .join(
                        s.model_profiles,
                        s.model_profiles.c.id == s.model_config_versions.c.profile_id,
                    )
                    .join(
                        s.model_credentials,
                        s.model_credentials.c.id == s.model_config_versions.c.credential_id,
                    )
                    .where(s.model_config_versions.c.id == space["config_version_id"])
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            config_row is None
            or config_row["profile_state"] != "active"
            or config_row["credential_status"] != "active"
        ):
            raise EmbeddingRuntimeError("EMBEDDING_MODEL_UNAVAILABLE")
        config = canonical_config_from_row(dict(config_row))
        if (
            config.logical_role is not LogicalRole.EMBEDDING
            or not config.enabled
            or config.profile_id != space["model_profile_id"]
            or config.model_name != space["model_name_snapshot"]
            or model_config_digest(config) != config_row["config_sha256"]
            or row["dimensions"] != space["dimensions"]
            or config.protocol_options["dimensions"] not in (None, space["dimensions"])
        ):
            raise EmbeddingRuntimeError("EMBEDDING_CONFIG_MISMATCH")
        capability = (
            (
                await self._session.execute(
                    select(s.model_capability_snapshots).where(
                        s.model_capability_snapshots.c.id == config_row["capability_snapshot_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            capability is None
            or capability["status"] != "valid"
            or capability["endpoint_id"] != config.endpoint_id
            or capability["protocol"] != config.protocol.value
            or capability["model_name"] != config.model_name
            or space["dimensions"] not in capability["embedding_dimensions"]
        ):
            raise EmbeddingRuntimeError("EMBEDDING_CAPABILITY_INVALID")
        old = payload.get("snapshot")
        if old is not None and not isinstance(old, dict):
            raise EmbeddingRuntimeError("EMBEDDING_SNAPSHOT_INVALID")
        if old is None and not capability["observed_at"] <= now < capability["expires_at"]:
            raise EmbeddingRuntimeError("EMBEDDING_CAPABILITY_EXPIRED")
        credential_version = (
            old.get("credential_version_no")
            if old is not None
            else config_row["credential_version_no"]
        )
        if type(credential_version) is not int or credential_version <= 0:
            raise EmbeddingRuntimeError("EMBEDDING_CREDENTIAL_UNAVAILABLE")
        snapshot = {
            "schema_version": 1,
            "conversation_id": payload["conversation_id"],
            "space_id": str(space["id"]),
            "space_generation": space["generation"],
            "distance_metric": space["distance_metric"],
            "model_profile_id": str(space["model_profile_id"]),
            "model_name": space["model_name_snapshot"],
            "config_id": str(config_row["id"]),
            "credential_version_no": credential_version,
            "source_sha256": bytes(row["source_sha256"]).hex(),
            "chunk_index": row["chunk_index"],
            "chunker_version": row["chunker_version"],
            "dimensions": row["dimensions"],
            "normalization": space["normalization"],
            "capability_sha256": capability_snapshot_digest(dict(capability)).hex(),
            "targets": {
                key: str(row[key])
                for key in ("memory_version_id", "summary_version_id", "message_revision_id")
                if row[key] is not None
            },
        }
        if old is not None and old != snapshot:
            raise EmbeddingRuntimeError("EMBEDDING_SNAPSHOT_CHANGED")
        credential = (
            (
                await self._session.execute(
                    text("SELECT * FROM get_model_credential_version(:profile, :version)"),
                    {"profile": config.profile_id, "version": credential_version},
                )
            )
            .mappings()
            .one_or_none()
        )
        if credential is None or credential["credential_id"] != config.credential_id:
            raise EmbeddingRuntimeError("EMBEDDING_CREDENTIAL_UNAVAILABLE")
        envelope = CredentialEnvelope(
            **{
                key: credential[key]
                for key in (
                    "ciphertext",
                    "nonce",
                    "key_version",
                    "aad_schema_version",
                    "secret_fingerprint",
                    "algorithm",
                )
            }
        )
        endpoint_row = (
            (
                await self._session.execute(
                    select(s.model_endpoints).where(
                        s.model_endpoints.c.id == config.endpoint_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        endpoint = RuntimeEndpointSnapshot(
            **{
                key: endpoint_row[key]
                for key in (
                    "base_url",
                    "network_policy_id",
                    "network_policy_version",
                    "network_category",
                )
            },
            endpoint_id=config.endpoint_id,
        )
        if old is None:
            await self._session.execute(
                update(s.background_jobs)
                .where(
                    s.background_jobs.c.id == job.id,
                )
                .values(payload={**payload, "snapshot": snapshot})
            )
        return PreparedEmbedding(
            record_id,
            snapshot,
            config,
            endpoint,
            CredentialBinding(
                LogicalRole.EMBEDDING, config.profile_id, config.credential_id, credential_version
            ),
            envelope,
            SensitiveValue(content),
        )

    async def complete(
        self,
        job: JobRecord,
        prepared: PreparedEmbedding,
        vector: tuple[float, ...],
        *,
        now: datetime,
    ) -> None:
        # Reload both the sealed contract and canonical source after HTTP. This
        # also rejects a destroyed credential or disabled model before publishing.
        current = await self.prepare(job, now=now)
        if current is None:
            return
        if current.snapshot != prepared.snapshot or current.record_id != prepared.record_id:
            raise EmbeddingRuntimeError("EMBEDDING_SNAPSHOT_CHANGED")
        values = checked_vector(
            vector,
            dimensions=current.snapshot["dimensions"],
            normalization=current.snapshot["normalization"],
        )
        await self._session.execute(
            update(s.embedding_records)
            .where(
                s.embedding_records.c.id == current.record_id,
                s.embedding_records.c.account_id == job.account_id,
                s.embedding_records.c.state == "pending",
            )
            .values(vector_payload=values, state="ready")
        )

    async def fail(self, job: JobRecord, *, code: str, now: datetime) -> None:
        payload = await self._fenced_payload(job, now)
        record_id = self._record_id(payload)
        if job.id != embedding_job_id(record_id):
            raise EmbeddingRuntimeError("EMBEDDING_JOB_SCOPE_INVALID")
        invalidated = code == "EMBEDDING_SOURCE_UNAVAILABLE"
        await self._session.execute(
            update(s.embedding_records)
            .where(
                s.embedding_records.c.id == record_id,
                s.embedding_records.c.account_id == job.account_id,
                s.embedding_records.c.state == "pending",
            )
            .values(
                state="invalidated" if invalidated else "failed",
                vector_payload=[],
                invalidated_at=now if invalidated else None,
            )
        )
