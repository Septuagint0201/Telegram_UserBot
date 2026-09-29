"""Immutable model and canonical-source inputs for background memory runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_source_hash import (
    EMPTY_MESSAGE_CONTENT,
    EMPTY_MESSAGE_SHA256,
)
from telegram_userbot.adapters.persistence.model_runtime import (
    RuntimeEndpointSnapshot,
    canonical_config_from_row,
    model_capabilities_from_row,
    model_config_digest,
)
from telegram_userbot.adapters.persistence.model_snapshots import capability_snapshot_digest
from telegram_userbot.domain.memory.models import InputSource, TrustClass
from telegram_userbot.domain.model_config import CanonicalModelConfig, ModelCapabilities
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto.credentials import CredentialBinding, CredentialEnvelope


class MemoryPipelineError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MemoryModelSnapshot:
    config_id: UUID
    credential_version_id: UUID
    config: CanonicalModelConfig
    capabilities: ModelCapabilities
    capability_sha256: bytes
    endpoint: RuntimeEndpointSnapshot
    binding: CredentialBinding
    envelope: CredentialEnvelope = field(repr=False)
    prompt: SensitiveValue[str] = field(repr=False)
    prompt_sha256: bytes


async def load_memory_model(  # noqa: PLR0913 - explicit immutable model selection
    session: AsyncSession,
    *,
    prompt_version: str,
    now: datetime,
    config_id: UUID | None = None,
    credential_version_id: UUID | None = None,
    logical_role: str = "memory_agent",
) -> MemoryModelSnapshot:
    query = (
        select(
            s.model_config_versions,
            s.model_profiles.c.logical_role,
            s.model_profiles.c.id.label("model_profile_id"),
            s.model_credentials.c.active_version_no.label("credential_version_no"),
        )
        .join(s.model_profiles, s.model_profiles.c.id == s.model_config_versions.c.profile_id)
        .join(
            s.model_credentials,
            s.model_credentials.c.id == s.model_config_versions.c.credential_id,
        )
        .where(
            s.model_profiles.c.logical_role == logical_role,
            s.model_profiles.c.state == "active",
            s.model_credentials.c.status == "active",
        )
    )
    query = (
        query.where(s.model_config_versions.c.id == config_id)
        if config_id
        else query.where(
            s.model_config_versions.c.version_no == s.model_profiles.c.active_config_version_no,
        )
    )
    row = (await session.execute(query)).mappings().one_or_none()
    if row is None:
        raise MemoryPipelineError("MEMORY_MODEL_UNAVAILABLE")
    config = canonical_config_from_row(dict(row))
    if not config.enabled or model_config_digest(config) != row["config_sha256"]:
        raise MemoryPipelineError("MEMORY_CONFIG_INVALID")
    cap = (
        (
            await session.execute(
                select(s.model_capability_snapshots).where(
                    s.model_capability_snapshots.c.id == row["capability_snapshot_id"],
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        cap is None
        or cap["status"] != "valid"
        or cap["endpoint_id"] != config.endpoint_id
        or cap["model_name"] != config.model_name
        or cap["protocol"] != config.protocol.value
        or (config_id is None and not cap["observed_at"] <= now < cap["expires_at"])
    ):
        raise MemoryPipelineError("MEMORY_CAPABILITY_INVALID")
    capabilities = model_capabilities_from_row(
        config, {**dict(cap), "capability_protocol": cap["protocol"]}
    )
    if (
        not capabilities.supports_text
        or not {"system", "user"} <= capabilities.supported_input_roles
    ):
        raise MemoryPipelineError("MEMORY_CAPABILITY_INVALID")
    version = s.model_credential_versions
    metadata = select(version.c.id, version.c.version_no).where(
        version.c.profile_id == config.profile_id,
        version.c.credential_id == config.credential_id,
        version.c.destroyed_at.is_(None),
    )
    metadata = (
        metadata.where(version.c.id == credential_version_id)
        if credential_version_id
        else metadata.where(
            version.c.version_no == row["credential_version_no"],
        )
    )
    credential_meta = (await session.execute(metadata)).one_or_none()
    if credential_meta is None:
        raise MemoryPipelineError("MEMORY_CREDENTIAL_UNAVAILABLE")
    credential = (
        (
            await session.execute(
                text("SELECT * FROM get_model_credential_version(:profile, :version)"),
                {"profile": config.profile_id, "version": credential_meta.version_no},
            )
        )
        .mappings()
        .one_or_none()
    )
    if credential is None or credential["credential_id"] != config.credential_id:
        raise MemoryPipelineError("MEMORY_CREDENTIAL_UNAVAILABLE")
    matched = re.fullmatch(r"prompt-v([1-9][0-9]*)", prompt_version)
    if matched is None:
        raise MemoryPipelineError("MEMORY_PROMPT_UNSUPPORTED")
    prompt = (
        (
            await session.execute(
                select(s.prompt_versions).where(
                    s.prompt_versions.c.logical_role == logical_role,
                    s.prompt_versions.c.version_no == int(matched[1]),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        prompt is None
        or hashlib.sha256(prompt["template_body"].encode()).digest() != prompt["template_sha256"]
    ):
        raise MemoryPipelineError("MEMORY_PROMPT_INVALID")
    endpoint = (
        (
            await session.execute(
                select(s.model_endpoints).where(
                    s.model_endpoints.c.id == config.endpoint_id,
                )
            )
        )
        .mappings()
        .one()
    )
    return MemoryModelSnapshot(
        row["id"],
        credential_meta.id,
        config,
        capabilities,
        capability_snapshot_digest(dict(cap)),
        RuntimeEndpointSnapshot(
            config.endpoint_id,
            endpoint["base_url"],
            endpoint["network_policy_id"],
            endpoint["network_policy_version"],
            endpoint["network_category"],
        ),
        CredentialBinding(
            config.logical_role, config.profile_id, config.credential_id, credential_meta.version_no
        ),
        CredentialEnvelope(
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
        ),
        SensitiveValue(prompt["template_body"]),
        prompt["template_sha256"],
    )


def message_source_query(account: UUID, conversation: UUID) -> Any:
    return (
        select(
            s.message_revisions,
            s.messages.c.role.label("message_role"),
            s.messages.c.source.label("message_source"),
        )
        .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
        .where(
            s.messages.c.account_id == account,
            s.messages.c.conversation_id == conversation,
            s.message_revisions.c.account_id == account,
            s.messages.c.deleted_at.is_(None),
            s.messages.c.is_tombstone.is_(False),
            s.messages.c.source_status != "pending",
            s.messages.c.current_revision_no == s.message_revisions.c.revision_no,
            s.message_revisions.c.redacted_at.is_(None),
            or_(
                s.message_revisions.c.body_kind.in_(("text", "caption")),
                and_(
                    s.message_revisions.c.body_kind == "none",
                    exists(
                        select(s.message_media.c.id).where(
                            s.message_media.c.account_id == account,
                            s.message_media.c.message_revision_id == s.message_revisions.c.id,
                            s.message_media.c.media_kind.in_(("photo", "image_document")),
                        )
                    ),
                ),
            ),
        )
    )


def message_source(row: Any) -> InputSource:
    body = row["text_content"] if row["body_kind"] == "text" else row["caption"]
    # Telegram stores the hash of the canonical body envelope, including entities.
    # Keeping that envelope in the model input preserves the original evidence hash.
    body = json.dumps(
        {"kind": row["body_kind"], "text": body, "entities": row["entities"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    trust = (
        TrustClass.USER_STATEMENT
        if row["message_source"] in {"telegram_user", "human"}
        else TrustClass.MODEL_INFERENCE
    )
    digest = row["content_sha256"]
    if row["body_kind"] == "none" and digest is None and body == EMPTY_MESSAGE_CONTENT:
        digest = EMPTY_MESSAGE_SHA256
    try:
        return InputSource(
            row["id"],
            f"revision-{row['revision_no']}",
            body,
            bytes(digest),
            trust=trust,
            visual_only=row["body_kind"] == "none",
        )
    except ValueError, TypeError:
        raise MemoryPipelineError("MEMORY_SOURCE_INVALID") from None


async def select_memory_sources(session: AsyncSession, job: Any) -> tuple[InputSource, ...]:
    """Select current text plus existing formal context and its canonical roots."""
    account, conversation = job["account_id"], job["conversation_id"]
    revisions = s.message_revisions
    rows = (
        (
            await session.execute(
                message_source_query(account, conversation)
                .where(
                    revisions.c.source_event_id.between(
                        job["range_start_event_id"], job["range_end_event_id"]
                    )
                )
                .order_by(revisions.c.source_event_id, revisions.c.id)
                .limit(1001)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 1000:
        raise MemoryPipelineError("MEMORY_INPUT_LIMIT_EXCEEDED")
    sources = [message_source(row) for row in rows]
    memories = (
        (
            await session.execute(
                select(s.memory_versions)
                .join(
                    s.memories,
                    s.memories.c.id == s.memory_versions.c.memory_id,
                )
                .where(
                    s.memories.c.account_id == account,
                    s.memories.c.conversation_id == conversation,
                    s.memories.c.status == "active",
                    s.memories.c.current_version_no == s.memory_versions.c.version_no,
                    s.memory_versions.c.redacted_at.is_(None),
                    s.memory_versions.c.rendered_text.is_not(None),
                )
                .order_by(s.memories.c.updated_at.desc(), s.memories.c.id)
                .limit(16)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    # Formal memories are optional context; every newly scheduled message above
    # remains mandatory. Include a memory only with all its current canonical
    # roots, within a separate byte budget, never a truncated body or root set.
    remaining = 8000
    included = {item.source_id for item in sources}
    for row in memories:
        body = row["rendered_text"]
        if len(body.encode()) + 512 > remaining:
            continue
        roots = (
            (
                await session.execute(
                    message_source_query(account, conversation)
                    .add_columns(s.memory_evidence.c.source_content_sha256.label("evidence_hash"))
                    .join(
                        s.memory_evidence, s.memory_evidence.c.message_revision_id == revisions.c.id
                    )
                    .where(s.memory_evidence.c.memory_version_id == row["id"])
                    .order_by(revisions.c.source_event_id, revisions.c.id)
                    .limit(9)
                )
            )
            .mappings()
            .all()
        )
        expected = await session.scalar(
            select(func.count())
            .select_from(s.memory_evidence)
            .where(s.memory_evidence.c.memory_version_id == row["id"])
        )
        if (
            not roots
            or len(roots) > 8
            or len(roots) != expected
            or any(message_source(item).content_sha256 != item["evidence_hash"] for item in roots)
        ):
            continue
        extra = [message_source(item) for item in roots if item["id"] not in included]
        size = len(body.encode()) + 512 + sum(len(item.content.encode()) + 512 for item in extra)
        if size > remaining:
            continue
        remaining -= size
        sources.extend(extra)
        included.update(item.source_id for item in extra)
        sources.append(
            InputSource(
                row["id"],
                f"version-{row['version_no']}",
                body,
                hashlib.sha256(body.encode()).digest(),
                "memory_version",
                TrustClass.TRUSTED_DERIVED,
            )
        )
    summary = (
        (
            await session.execute(
                select(s.summary_versions)
                .join(
                    s.summaries,
                    s.summaries.c.id == s.summary_versions.c.summary_id,
                )
                .where(
                    s.summaries.c.account_id == account,
                    s.summaries.c.conversation_id == conversation,
                    s.summaries.c.summary_kind == "rolling",
                    s.summaries.c.status == "active",
                    s.summaries.c.current_version_no == s.summary_versions.c.version_no,
                    s.summary_versions.c.invalidation_state == "active",
                    s.summary_versions.c.redacted_at.is_(None),
                )
                .order_by(s.summary_versions.c.created_at.desc(), s.summary_versions.c.id)
                .limit(1)
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if summary is not None and len(summary["content_text"].encode()) <= 4000:
        sources.append(
            InputSource(
                summary["id"],
                f"version-{summary['version_no']}",
                summary["content_text"],
                summary["content_sha256"],
                "summary_version",
                TrustClass.TRUSTED_DERIVED,
            )
        )
    return tuple(sources)


async def reload_memory_sources(session: AsyncSession, manifest: Any) -> tuple[InputSource, ...]:
    items = (
        (
            await session.execute(
                select(s.memory_input_manifest_items)
                .where(
                    s.memory_input_manifest_items.c.manifest_id == manifest["id"],
                )
                .order_by(s.memory_input_manifest_items.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    sources: list[InputSource] = []
    for item in items:
        kind = item["source_type"]
        if kind == "media_object":
            # Reloaded from the complete canonical attachment set by _inputs,
            # then compared with the sealed manifest hash (including order).
            continue
        if kind == "message_revision":
            row = (
                (
                    await session.execute(
                        message_source_query(manifest["account_id"], manifest["conversation_id"])
                        .where(
                            s.message_revisions.c.id == item["message_revision_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise MemoryPipelineError("MEMORY_SOURCE_CHANGED")
            source = message_source(row)
        else:
            if kind == "memory_version":
                version, parent, value = (
                    s.memory_versions,
                    s.memories,
                    s.memory_versions.c.rendered_text,
                )
                parent_column = version.c.memory_id
            elif kind == "summary_version":
                version, parent, value = (
                    s.summary_versions,
                    s.summaries,
                    s.summary_versions.c.content_text,
                )
                parent_column = version.c.summary_id
            else:
                raise MemoryPipelineError("MEMORY_SOURCE_UNSUPPORTED")
            query = (
                select(value, version.c.version_no)
                .join(parent, parent.c.id == parent_column)
                .where(
                    version.c.id == item[f"{kind}_id"],
                    version.c.account_id == manifest["account_id"],
                    parent.c.conversation_id == manifest["conversation_id"],
                    parent.c.status == "active",
                    parent.c.current_version_no == version.c.version_no,
                    version.c.redacted_at.is_(None),
                )
            )
            if kind == "summary_version":
                query = query.where(version.c.invalidation_state == "active")
            result = (await session.execute(query.with_for_update())).one_or_none()
            if result is None or result[0] is None:
                raise MemoryPipelineError("MEMORY_SOURCE_CHANGED")
            source = InputSource(
                item[f"{kind}_id"],
                f"version-{result[1]}",
                result[0],
                hashlib.sha256(result[0].encode()).digest(),
                kind,
                TrustClass.TRUSTED_DERIVED,
            )
        if (
            source.revision != item["source_revision"]
            or source.content_sha256 != item["source_content_sha256"]
            or source.trust.value != item["trust_class"]
            or item["source_redacted"]
            or source.visual_only != item["source_visual_only"]
        ):
            raise MemoryPipelineError("MEMORY_SOURCE_CHANGED")
        sources.append(source)
    return tuple(sources)


async def require_coverage(
    session: AsyncSession, row: Any, sources: tuple[InputSource, ...]
) -> None:
    """Prove that the sealed input still covers every eligible event in its range."""
    pending = await session.scalar(
        select(s.message_events.c.id)
        .where(
            s.message_events.c.account_id == row["account_id"],
            s.message_events.c.conversation_id == row["conversation_id"],
            s.message_events.c.id.between(row["range_start_event_id"], row["range_end_event_id"]),
            s.message_events.c.projected_at.is_(None),
        )
        .limit(1)
    )
    if pending is not None:
        raise MemoryPipelineError("MEMORY_PROJECTION_PENDING", retryable=True)
    revisions = (
        await session.execute(
            select(
                s.message_revisions.c.id,
                s.messages.c.source_status,
            )
            .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
            .where(
                s.messages.c.account_id == row["account_id"],
                s.messages.c.conversation_id == row["conversation_id"],
                s.message_revisions.c.source_event_id.between(
                    row["range_start_event_id"], row["range_end_event_id"]
                ),
                s.messages.c.current_revision_no == s.message_revisions.c.revision_no,
                s.messages.c.deleted_at.is_(None),
                s.messages.c.is_tombstone.is_(False),
                s.message_revisions.c.redacted_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if any(item.source_status == "pending" for item in revisions):
        raise MemoryPipelineError("MEMORY_SOURCE_PENDING", retryable=True)
    expected = (
        (
            await session.execute(
                message_source_query(row["account_id"], row["conversation_id"]).where(
                    s.message_revisions.c.source_event_id.between(
                        row["range_start_event_id"], row["range_end_event_id"]
                    ),
                )
            )
        )
        .mappings()
        .all()
    )
    selected = {item.source_id for item in sources if item.source_type == "message_revision"}
    if any(item["id"] not in selected for item in expected):
        raise MemoryPipelineError("MEMORY_COVERAGE_CHANGED")
