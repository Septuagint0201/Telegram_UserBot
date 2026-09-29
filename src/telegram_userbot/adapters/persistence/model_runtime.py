"""Exact model-run snapshots and transactional M5 context preparation."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import RowMapping, and_, exists, false, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import ContextRepository
from telegram_userbot.adapters.persistence.model_snapshots import capability_snapshot_digest
from telegram_userbot.adapters.persistence.schema import (
    context_manifest_item_reasons,
    context_manifest_items,
    context_manifest_omissions,
    context_manifests,
    context_policies,
    context_policy_versions,
    conversation_turns,
    conversations,
    media_objects,
    memories,
    memory_versions,
    message_media,
    message_revisions,
    messages,
    model_capability_snapshots,
    model_config_versions,
    model_endpoints,
    model_profiles,
    model_runs,
    prompt_versions,
    retrieval_policies,
    retrieval_policy_versions,
    summaries,
    summary_versions,
    turn_messages,
)
from telegram_userbot.domain.context import (
    Candidate,
    ContextCapabilities,
    ContextLayer,
    ContextManifest,
    ContextPolicy,
    ContextSource,
    ManifestItem,
    TrustLevel,
    build_context,
    calculate_budget,
    estimate_utf8_bytes_v1,
    rebuild_context,
    render_data_boundary,
    select_structured,
)
from telegram_userbot.domain.model_config import (
    GENERATION_ROLES,
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    profile_kind_for,
    validate_activation,
)
from telegram_userbot.domain.shared.hashing import stable_json_bytes
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding, CredentialEnvelope

MODEL_CONTEXT_BUILDER_VERSION = "production-context-v1"
MODEL_INPUT_FINGERPRINT_VERSION = "canonical-model-input-hmac-v1"
MAX_RECENT_MESSAGES = 24
MAX_CONTEXT_MEMORIES = 64
MAX_CONTEXT_SUMMARIES = 2


class ModelRuntimeSnapshotError(RuntimeError):
    """Stable, content-free failure while freezing a provider request."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _background_runtime_unavailable() -> NoReturn:
    """Reject background generation until its dedicated pipeline is implemented.

    Memory and proactive manifests have different ownership and source contracts
    from the reactive ``context_manifests`` handled by this repository.  Keeping
    this boundary explicit prevents a missing optional module from becoming an
    uncaught import error or, worse, silently reusing the reactive selector.
    """

    raise ModelRuntimeSnapshotError("MODEL_BACKGROUND_RUNTIME_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class RuntimeEndpointSnapshot:
    endpoint_id: UUID
    base_url: str
    network_policy_id: UUID
    network_policy_version: int
    network_category: str


@dataclass(frozen=True, slots=True)
class RuntimeImageSnapshot:
    object_id: UUID
    storage_key: str
    sha256: bytes
    mime_type: str
    byte_size: int

    def __post_init__(self) -> None:
        if (
            not self.storage_key
            or len(self.sha256) != 32
            or self.mime_type not in {"image/jpeg", "image/png", "image/webp"}
            or self.byte_size <= 0
        ):
            raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")


@dataclass(frozen=True, slots=True)
class RuntimeProactiveOutputScope:
    """Immutable, content-free bounds used to validate a proactive decision."""

    candidate_id: UUID
    occurrence_ids: frozenset[UUID]
    window_end_at: datetime
    timezone_name: str
    absolute_no_send_start_local: time
    absolute_no_send_end_local: time

    def __post_init__(self) -> None:
        if (
            not self.occurrence_ids
            or not self.timezone_name
            or self.window_end_at.tzinfo is None
            or self.window_end_at.utcoffset() is None
        ):
            raise ModelRuntimeSnapshotError("MODEL_PROACTIVE_SCOPE_INVALID")


@dataclass(frozen=True, slots=True)
class PreparedModelGeneration:
    run_id: UUID
    account_id: UUID
    conversation_id: UUID
    turn_id: UUID | None
    purpose: str
    config: CanonicalModelConfig
    config_sha256: bytes
    capabilities: ModelCapabilities
    capability_snapshot_sha256: bytes
    endpoint: RuntimeEndpointSnapshot
    credential_binding: CredentialBinding
    credential_envelope: CredentialEnvelope
    context_manifest_id: UUID
    context_manifest_sha256: bytes
    orchestration_claim_fingerprint: bytes
    canonical_input_fingerprint: bytes
    ordered_sources: tuple[ContextSource, ...]
    images: tuple[RuntimeImageSnapshot, ...]
    memory_job_id: UUID | None = None
    proactive_job_id: UUID | None = None
    output_schema_version: int = 1
    proactive_output_scope: RuntimeProactiveOutputScope | None = None


def canonical_config_from_row(row: Mapping[str, object]) -> CanonicalModelConfig:
    return CanonicalModelConfig(
        profile_id=cast(UUID, row["model_profile_id"]),
        logical_role=LogicalRole(str(row["logical_role"])),
        endpoint_id=cast(UUID, row["endpoint_id"]),
        credential_id=cast(UUID, row["credential_id"]),
        protocol=ModelProtocol(str(row["protocol"])),
        model_name=str(row["model_name"]),
        temperature=(None if row["temperature"] is None else float(cast(Any, row["temperature"]))),
        max_output_tokens=cast(int | None, row["max_output_tokens"]),
        timeout_seconds=cast(int, row["timeout_seconds"]),
        enabled=cast(bool, row["enabled"]),
        protocol_options=cast(Mapping[str, object], row["protocol_options"]),
    )


def model_capabilities_from_row(
    config: CanonicalModelConfig, row: Mapping[str, object]
) -> ModelCapabilities:
    return ModelCapabilities(
        profile_kind=profile_kind_for(config.logical_role),
        supported_protocols=frozenset({ModelProtocol(str(row["capability_protocol"]))}),
        supports_text=cast(bool, row["supports_text"]),
        supports_temperature=cast(bool, row["supports_temperature"]),
        supports_structured_output=cast(bool, row["supports_structured_output"]),
        supports_streaming=cast(bool, row["supports_stream"]),
        supports_images=cast(bool, row["supports_image"]),
        max_context_tokens=cast(int, row["max_context_tokens"]),
        max_output_tokens_limit=cast(int | None, row["max_output_tokens_limit"]),
        supported_input_roles=frozenset(cast(Sequence[str], row["supported_input_roles"])),
        chat_token_limit_field=cast(str | None, row["chat_token_limit_field"]),
        embedding_dimensions=frozenset(cast(Sequence[int], row["embedding_dimensions"])),
        supports_reasoning_effort=cast(bool, row["supports_reasoning_effort"]),
        max_images_per_request=cast(int, row["max_images_per_request"]),
        max_image_bytes_per_request=cast(int, row["max_image_bytes_per_request"]),
        auto_image_tokens=cast(int, row["auto_image_tokens"]),
        messages_auto_detail_equivalent=cast(bool, row["messages_auto_detail_equivalent"]),
    )


def capability_digest_from_row(row: Mapping[str, object]) -> bytes:
    try:
        return capability_snapshot_digest(
            row,
            aliases={
                "endpoint_id": "capability_endpoint_id",
                "protocol": "capability_protocol",
                "model_name": "capability_model_name",
            },
        )
    except ValueError:
        raise ModelRuntimeSnapshotError("MODEL_CAPABILITY_SNAPSHOT_INVALID") from None


def model_config_digest(config: CanonicalModelConfig) -> bytes:
    return hashlib.sha256(stable_json_bytes(config.canonical_payload())).digest()


async def load_runtime_credential(
    session: AsyncSession,
    *,
    run_id: UUID,
    profile_id: UUID,
    credential_version_id: UUID,
) -> Mapping[str, object]:
    """Read one undeleted credential bound to the durable run through the accessor."""

    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM get_model_credential_version_by_id("
                    ":run_id, :profile_id, :credential_version_id)"
                ),
                {
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "credential_version_id": credential_version_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ModelRuntimeSnapshotError("MODEL_CREDENTIAL_UNAVAILABLE")
    return {
        "credential_version_id_from_accessor": row["id"],
        "credential_id_from_accessor": row["credential_id"],
        "credential_profile_id": row["profile_id"],
        "credential_version_no": row["version_no"],
        "credential_algorithm": row["algorithm"],
        "credential_key_version": row["key_version"],
        "credential_aad_schema_version": row["aad_schema_version"],
        "credential_nonce": row["nonce"],
        "credential_ciphertext": row["ciphertext"],
        "credential_secret_fingerprint": row["secret_fingerprint"],
        "credential_destroyed_at": None,
    }


def canonical_input_fingerprint(  # noqa: PLR0913 - every immutable snapshot is keyed
    *,
    key: SensitiveValue[bytes],
    run_id: UUID,
    purpose: str,
    config_sha256: bytes,
    capability_sha256: bytes,
    manifest_sha256: bytes,
    sources: Sequence[ContextSource],
    images: Sequence[RuntimeImageSnapshot],
) -> bytes:
    """Key the complete canonical input identity without persisting its content."""

    raw_key = key.reveal_for_use()
    if not isinstance(raw_key, bytes) or len(raw_key) < 32:
        raise ModelRuntimeSnapshotError("MODEL_INPUT_HMAC_KEY_INVALID")
    image_by_id = {image.object_id: image for image in images}
    if len(image_by_id) != len(images):
        raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
    used_images: set[UUID] = set()
    source_payload = []
    for ordinal, source in enumerate(sources, 1):
        rendered = render_data_boundary(source)
        item: dict[str, object] = {
            "ordinal": ordinal,
            "role": source.canonical_role,
            "kind": "image" if source.image_detail is not None else "text",
            "source_type": source.source_type,
            "source_id": str(source.candidate.source_id),
            "source_revision": source.candidate.source_revision,
            "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "image_detail": source.image_detail,
            "image_tokens": source.image_tokens,
        }
        if source.image_detail is not None:
            image = image_by_id.get(source.candidate.source_id)
            if image is None or source.candidate.source_revision != f"sha256-{image.sha256.hex()}":
                raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
            used_images.add(image.object_id)
            item["image_sha256"] = image.sha256.hex()
            item["image_mime"] = image.mime_type
            item["image_byte_size"] = image.byte_size
        source_payload.append(item)
    if used_images != set(image_by_id):
        raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
    payload = {
        "schema": MODEL_INPUT_FINGERPRINT_VERSION,
        "run_id": str(run_id),
        "purpose": purpose,
        "config_sha256": config_sha256.hex(),
        "capability_sha256": capability_sha256.hex(),
        "manifest_sha256": manifest_sha256.hex(),
        "messages": source_payload,
    }
    return hmac.new(
        raw_key,
        stable_json_bytes(cast(Any, payload)),
        hashlib.sha256,
    ).digest()


def orchestration_claim_fingerprint(
    membership: Sequence[tuple[UUID, int]],
) -> bytes:
    """Reproduce the sealed M4 turn-membership claim identity exactly."""

    if not membership or any(
        not isinstance(message_id, UUID)
        or type(revision_no) is not int
        or not 1 <= revision_no <= 0xFFFFFFFF
        for message_id, revision_no in membership
    ):
        raise ModelRuntimeSnapshotError("MODEL_TURN_MEMBERSHIP_INVALID")
    return hashlib.sha256(
        b"m4-turn-input-v1\0"
        + b"\0".join(
            message_id.bytes + revision_no.to_bytes(4, "big")
            for message_id, revision_no in membership
        )
    ).digest()


def background_orchestration_claim_fingerprint(
    *, owner_kind: str, owner_id: UUID, purpose: str, generation_no: int, manifest_sha256: bytes
) -> bytes:
    """Bind a background request to one owner, purpose, generation, and manifest."""

    if (
        owner_kind not in {"memory_job", "proactive_job"}
        or not purpose
        or type(generation_no) is not int
        or generation_no <= 0
        or not isinstance(manifest_sha256, bytes)
        or len(manifest_sha256) != 32
    ):
        raise ModelRuntimeSnapshotError("MODEL_BACKGROUND_CLAIM_INVALID")
    return hashlib.sha256(
        stable_json_bytes(
            {
                "schema": "background-model-claim-v1",
                "owner_kind": owner_kind,
                "owner_id": str(owner_id),
                "purpose": purpose,
                "generation_no": generation_no,
                "manifest_sha256": manifest_sha256.hex(),
            }
        )
    ).digest()


def _prepared_generation(  # noqa: PLR0913 - immutable snapshot fields stay explicit
    *,
    row: RowMapping,
    run_id: UUID,
    config: CanonicalModelConfig,
    capabilities: ModelCapabilities,
    manifest_id: UUID,
    manifest_sha256: bytes,
    orchestration_fingerprint: bytes,
    canonical_fingerprint: bytes,
    ordered_sources: tuple[ContextSource, ...],
    images: tuple[RuntimeImageSnapshot, ...],
) -> PreparedModelGeneration:
    envelope = CredentialEnvelope(
        ciphertext=cast(bytes, row["credential_ciphertext"]),
        nonce=cast(bytes, row["credential_nonce"]),
        key_version=cast(int, row["credential_key_version"]),
        aad_schema_version=cast(int, row["credential_aad_schema_version"]),
        secret_fingerprint=cast(bytes, row["credential_secret_fingerprint"]),
        algorithm=str(row["credential_algorithm"]),
    )
    return PreparedModelGeneration(
        run_id,
        cast(UUID, row["account_id"]),
        cast(UUID, row["conversation_id"]),
        cast(UUID, row["turn_id"]),
        str(row["purpose"]),
        config,
        cast(bytes, row["config_sha256"]),
        capabilities,
        cast(bytes, row["run_capability_sha256"]),
        RuntimeEndpointSnapshot(
            cast(UUID, row["endpoint_id"]),
            str(row["base_url"]),
            cast(UUID, row["network_policy_id"]),
            cast(int, row["network_policy_version"]),
            str(row["network_category"]),
        ),
        CredentialBinding(
            LogicalRole(str(row["logical_role"])),
            cast(UUID, row["model_profile_id"]),
            cast(UUID, row["credential_id"]),
            cast(int, row["credential_version_no"]),
        ),
        envelope,
        manifest_id,
        manifest_sha256,
        orchestration_fingerprint,
        canonical_fingerprint,
        ordered_sources,
        images,
        output_schema_version=cast(int, row["output_schema_version"]),
    )


def _text(row: RowMapping) -> str | None:
    value = row["text_content"] if row["text_content"] is not None else row["caption"]
    return cast(str | None, value)


def _message_source(row: RowMapping, *, layer: ContextLayer) -> ContextSource:
    content = _text(row)
    if content is None or hashlib.sha256(content.encode()).digest() != row["content_sha256"]:
        raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
    direction = str(row["direction"])
    source = str(row["source"])
    if direction == "incoming":
        role = "user"
        actor = "contact"
        trust = TrustLevel.UNTRUSTED_USER
    else:
        role = "assistant"
        actor = "main_ai" if source in {"ai", "copilot_approved", "proactive_ai"} else "owner"
        trust = (
            TrustLevel.MODEL_GENERATED_HISTORY if actor == "main_ai" else TrustLevel.TRUSTED_HISTORY
        )
    return ContextSource(
        Candidate(
            cast(UUID, row["revision_id"]),
            f"revision-{row['revision_no']}",
            f"message:{row['message_id']}",
            layer,
            cast(datetime | None, row["telegram_created_at"]),
            estimate_utf8_bytes_v1(content),
        ),
        role,
        actor,
        trust,
        SensitiveValue(content),
        "message_revision",
    )


def memory_context_layer(memory_type: str) -> ContextLayer:
    if memory_type == "identity":
        return ContextLayer.IDENTITY
    if memory_type == "style":
        return ContextLayer.PERSONALITY
    if memory_type == "relationship":
        return ContextLayer.RELATIONSHIP_TIME
    return ContextLayer.STRUCTURED_MEMORY


class ModelRuntimeRepository:
    """Freeze one run and bind its immutable manifest before provider I/O."""

    def __init__(self, session: AsyncSession, *, new_uuid: Callable[[], UUID]) -> None:
        self._session = session
        self._new_uuid = new_uuid

    async def prepare_or_load_generation(
        self,
        *,
        run_id: UUID,
        expected_logical_role: LogicalRole = LogicalRole.MAIN_AI,
        input_hmac_key: SensitiveValue[bytes],
        now: datetime,
    ) -> PreparedModelGeneration:
        """Create one durable request, or reconstruct the already-bound request.

        The row lock makes the branch atomic.  The load branch is intentionally
        read-only: it uses the manifest and exact version references already
        attached to the run and never performs context selection again.
        """

        if expected_logical_role is not LogicalRole.MAIN_AI or await self._is_background_run(
            run_id
        ):
            _background_runtime_unavailable()
        row = await self._load_exact_run(
            run_id=run_id,
            expected_logical_role=expected_logical_role,
            now=now,
            prepared=None,
        )
        if row["context_manifest_id"] is None:
            return await self._prepare_generation_from_row(
                row=row,
                run_id=run_id,
                input_hmac_key=input_hmac_key,
                now=now,
            )
        return await self._load_prepared_generation_from_row(
            row=row,
            run_id=run_id,
            input_hmac_key=input_hmac_key,
            now=now,
        )

    async def prepare_generation(
        self,
        *,
        run_id: UUID,
        expected_logical_role: LogicalRole = LogicalRole.MAIN_AI,
        input_hmac_key: SensitiveValue[bytes],
        now: datetime,
    ) -> PreparedModelGeneration:
        if expected_logical_role is not LogicalRole.MAIN_AI or await self._is_background_run(
            run_id
        ):
            _background_runtime_unavailable()
        row = await self._load_exact_run(
            run_id=run_id,
            expected_logical_role=expected_logical_role,
            now=now,
            prepared=False,
        )
        return await self._prepare_generation_from_row(
            row=row,
            run_id=run_id,
            input_hmac_key=input_hmac_key,
            now=now,
        )

    async def load_prepared_generation(
        self,
        *,
        run_id: UUID,
        expected_logical_role: LogicalRole = LogicalRole.MAIN_AI,
        input_hmac_key: SensitiveValue[bytes],
        now: datetime,
    ) -> PreparedModelGeneration:
        """Rebuild an existing canonical request without changing durable state."""

        if expected_logical_role is not LogicalRole.MAIN_AI or await self._is_background_run(
            run_id
        ):
            _background_runtime_unavailable()
        row = await self._load_exact_run(
            run_id=run_id,
            expected_logical_role=expected_logical_role,
            now=now,
            prepared=True,
        )
        return await self._load_prepared_generation_from_row(
            row=row,
            run_id=run_id,
            input_hmac_key=input_hmac_key,
            now=now,
        )

    async def _is_background_run(self, run_id: UUID) -> bool:
        owner = (
            await self._session.execute(
                select(model_runs.c.memory_job_id, model_runs.c.proactive_job_id).where(
                    model_runs.c.id == run_id
                )
            )
        ).one_or_none()
        return owner is not None and (
            owner.memory_job_id is not None or owner.proactive_job_id is not None
        )

    async def _prepare_generation_from_row(
        self,
        *,
        row: RowMapping,
        run_id: UUID,
        input_hmac_key: SensitiveValue[bytes],
        now: datetime,
    ) -> PreparedModelGeneration:
        config = canonical_config_from_row(cast(Mapping[str, object], row))
        capabilities = model_capabilities_from_row(config, cast(Mapping[str, object], row))
        self._validate_exact_snapshot(row, config, capabilities, now=now)
        claim_fingerprint = await self._load_orchestration_claim_fingerprint(row)
        stored_claim = row["orchestration_claim_fingerprint"]
        stored_input = row["input_fingerprint"]
        if (
            not isinstance(stored_claim, bytes)
            or not isinstance(stored_input, bytes)
            or not hmac.compare_digest(stored_claim, claim_fingerprint)
            or not hmac.compare_digest(stored_input, claim_fingerprint)
        ):
            raise ModelRuntimeSnapshotError("MODEL_TURN_MEMBERSHIP_INVALID")
        policy_id, policy = await self._load_context_policy(row)
        retrieval_id, retrieval_version = await self._load_retrieval_policy()
        prompt = await self._load_prompt(row)
        sources, images = await self._load_sources(row, prompt, policy, capabilities, now=now)
        budget = calculate_budget(
            policy,
            ContextCapabilities(
                max_context_tokens=capabilities.max_context_tokens,
                max_output_tokens=cast(int, config.max_output_tokens),
                supports_images=capabilities.supports_images,
                max_images_per_request=capabilities.max_images_per_request,
                auto_image_tokens=capabilities.auto_image_tokens,
            ),
            required_image_count=len(images),
        )
        manifest_id = self._new_uuid()
        built = build_context(
            manifest_id=manifest_id,
            purpose=str(row["purpose"]),
            logical_role=str(row["logical_role"]),
            sources=sources,
            budget=budget,
            builder_version=MODEL_CONTEXT_BUILDER_VERSION,
            prompt_version=str(row["prompt_version"]),
            prompt_bundle_sha256=bytes(row["prompt_bundle_sha256"]).hex(),
            context_policy_version=policy.version,
            retrieval_policy_version=retrieval_version,
            capability_snapshot_sha256=bytes(row["run_capability_sha256"]).hex(),
            memory_freshness="fresh",
        )
        await ContextRepository(self._session).save_manifest(
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            turn_id=cast(UUID, row["turn_id"]),
            background_job_id=None,
            context_policy_version_id=policy_id,
            retrieval_policy_version_id=retrieval_id,
            prompt_bundle_sha256=cast(bytes, row["prompt_bundle_sha256"]),
            capability_snapshot_sha256=cast(bytes, row["run_capability_sha256"]),
            manifest=built.manifest,
            created_at=now,
        )
        fingerprint = canonical_input_fingerprint(
            key=input_hmac_key,
            run_id=run_id,
            purpose=str(row["purpose"]),
            config_sha256=cast(bytes, row["config_sha256"]),
            capability_sha256=cast(bytes, row["run_capability_sha256"]),
            manifest_sha256=bytes.fromhex(built.manifest.manifest_sha256),
            sources=built.ordered_sources,
            images=images,
        )
        bound = await self._session.execute(
            update(model_runs)
            .where(
                model_runs.c.id == run_id,
                model_runs.c.state == "running",
                model_runs.c.cancel_requested_at.is_(None),
                model_runs.c.context_manifest_id.is_(None),
                model_runs.c.config_version_id == row["config_version_id"],
                model_runs.c.credential_version_id == row["credential_version_id"],
                model_runs.c.account_control_version_snapshot
                == row["account_control_version_snapshot"],
                model_runs.c.mode_version_snapshot == row["mode_version_snapshot"],
                model_runs.c.content_revision_snapshot == row["content_revision_snapshot"],
                model_runs.c.orchestration_claim_fingerprint == claim_fingerprint,
                model_runs.c.input_fingerprint == claim_fingerprint,
            )
            .values(context_manifest_id=manifest_id, input_fingerprint=fingerprint)
        )
        if getattr(bound, "rowcount", 0) != 1:
            raise ModelRuntimeSnapshotError("MODEL_RUN_CONTEXT_BIND_CONFLICT")
        return _prepared_generation(
            row=row,
            run_id=run_id,
            config=config,
            capabilities=capabilities,
            manifest_id=manifest_id,
            manifest_sha256=bytes.fromhex(built.manifest.manifest_sha256),
            orchestration_fingerprint=claim_fingerprint,
            canonical_fingerprint=fingerprint,
            ordered_sources=built.ordered_sources,
            images=images,
        )

    async def _load_prepared_generation_from_row(
        self,
        *,
        row: RowMapping,
        run_id: UUID,
        input_hmac_key: SensitiveValue[bytes],
        now: datetime,
    ) -> PreparedModelGeneration:
        config = canonical_config_from_row(cast(Mapping[str, object], row))
        capabilities = model_capabilities_from_row(config, cast(Mapping[str, object], row))
        self._validate_exact_snapshot(row, config, capabilities, now=now)
        claim_fingerprint = await self._load_orchestration_claim_fingerprint(row)
        stored_claim = row["orchestration_claim_fingerprint"]
        if not isinstance(stored_claim, bytes) or not hmac.compare_digest(
            stored_claim, claim_fingerprint
        ):
            raise ModelRuntimeSnapshotError("MODEL_TURN_MEMBERSHIP_INVALID")
        manifest, item_rows = await self._load_bound_manifest(row)
        candidate_sources, images = await self._load_exact_manifest_sources(
            row=row,
            manifest=manifest,
            item_rows=item_rows,
            now=now,
        )
        try:
            rebuilt = rebuild_context(manifest, candidate_sources)
        except TypeError, ValueError:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID") from None
        manifest_sha256 = bytes.fromhex(rebuilt.manifest.manifest_sha256)
        fingerprint = canonical_input_fingerprint(
            key=input_hmac_key,
            run_id=run_id,
            purpose=str(row["purpose"]),
            config_sha256=cast(bytes, row["config_sha256"]),
            capability_sha256=cast(bytes, row["run_capability_sha256"]),
            manifest_sha256=manifest_sha256,
            sources=rebuilt.ordered_sources,
            images=images,
        )
        stored_fingerprint = row["input_fingerprint"]
        if (
            not isinstance(stored_fingerprint, bytes)
            or len(stored_fingerprint) != 32
            or not hmac.compare_digest(stored_fingerprint, fingerprint)
        ):
            raise ModelRuntimeSnapshotError("MODEL_INPUT_FINGERPRINT_MISMATCH")
        return _prepared_generation(
            row=row,
            run_id=run_id,
            config=config,
            capabilities=capabilities,
            manifest_id=manifest.id,
            manifest_sha256=manifest_sha256,
            orchestration_fingerprint=claim_fingerprint,
            canonical_fingerprint=fingerprint,
            ordered_sources=rebuilt.ordered_sources,
            images=images,
        )

    async def _load_bound_manifest(
        self,
        row: RowMapping,
    ) -> tuple[ContextManifest, tuple[RowMapping, ...]]:
        manifest_id = cast(UUID, row["context_manifest_id"])
        manifest_row = (
            (
                await self._session.execute(
                    select(context_manifests).where(context_manifests.c.id == manifest_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if manifest_row is None or any(
            manifest_row[name] != expected
            for name, expected in (
                ("account_id", row["account_id"]),
                ("conversation_id", row["conversation_id"]),
                ("owner_kind", "turn"),
                ("turn_id", row["turn_id"]),
                ("background_job_id", None),
                ("purpose", row["purpose"]),
                ("logical_role", row["logical_role"]),
                ("builder_version", MODEL_CONTEXT_BUILDER_VERSION),
                ("prompt_version", row["prompt_version"]),
                ("prompt_bundle_sha256", row["prompt_bundle_sha256"]),
                ("capability_snapshot_sha256", row["run_capability_sha256"]),
            )
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID")
        item_rows = tuple(
            (
                await self._session.execute(
                    select(context_manifest_items)
                    .where(
                        context_manifest_items.c.manifest_id == manifest_id,
                        context_manifest_items.c.account_id == row["account_id"],
                    )
                    .order_by(context_manifest_items.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        item_ids = tuple(cast(int, item["id"]) for item in item_rows)
        reason_rows: tuple[RowMapping, ...] = ()
        if item_ids:
            reason_rows = tuple(
                (
                    await self._session.execute(
                        select(context_manifest_item_reasons)
                        .where(context_manifest_item_reasons.c.manifest_item_id.in_(item_ids))
                        .order_by(
                            context_manifest_item_reasons.c.manifest_item_id,
                            context_manifest_item_reasons.c.reason_ordinal,
                        )
                    )
                )
                .mappings()
                .all()
            )
        reasons: dict[int, list[str]] = {item_id: [] for item_id in item_ids}
        for reason in reason_rows:
            reasons[cast(int, reason["manifest_item_id"])].append(str(reason["reason_code"]))
        omission_rows = tuple(
            (
                await self._session.execute(
                    select(context_manifest_omissions)
                    .where(context_manifest_omissions.c.manifest_id == manifest_id)
                    .order_by(context_manifest_omissions.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        try:
            items = tuple(
                ManifestItem(
                    ordinal=cast(int, item["ordinal"]),
                    layer=str(item["layer"]),
                    canonical_role=str(item["canonical_role"]),
                    source_actor=str(item["source_actor"]),
                    source_type=str(item["source_type"]),
                    source_id=str(item["source_id"]),
                    source_revision=str(item["source_revision"]),
                    trust_level=str(item["trust_level"]),
                    token_estimate=cast(int, item["token_estimate"]),
                    estimated_image_tokens=cast(int, item["estimated_image_tokens"]),
                    content_sha256=bytes(item["content_sha256"]).hex(),
                    rendered_part_sha256=bytes(item["rendered_part_sha256"]).hex(),
                    reasons=tuple(reasons[cast(int, item["id"])]),
                    rank_position=cast(int | None, item["rank_position"]),
                    base_score=(None if item["base_score"] is None else float(item["base_score"])),
                    final_score=(
                        None if item["final_score"] is None else float(item["final_score"])
                    ),
                    image_detail=cast(str | None, item["image_detail"]),
                )
                for item in item_rows
            )
            omissions = tuple(f"{item['layer']}:{item['reason_code']}" for item in omission_rows)
            manifest = ContextManifest(
                id=manifest_id,
                purpose=str(manifest_row["purpose"]),
                logical_role=str(manifest_row["logical_role"]),
                builder_version=str(manifest_row["builder_version"]),
                prompt_version=str(manifest_row["prompt_version"]),
                prompt_bundle_sha256=bytes(manifest_row["prompt_bundle_sha256"]).hex(),
                context_policy_version=str(manifest_row["token_policy_version"]),
                retrieval_policy_version=str(manifest_row["retrieval_policy_version"]),
                token_estimator_version=str(manifest_row["token_estimator_version"]),
                capability_snapshot_sha256=bytes(manifest_row["capability_snapshot_sha256"]).hex(),
                memory_freshness=str(manifest_row["memory_freshness"]),
                effective_input_budget=cast(int, manifest_row["effective_input_budget"]),
                safety_reserve_tokens=cast(int, manifest_row["safety_reserve_tokens"]),
                estimated_instruction_tokens=cast(
                    int, manifest_row["estimated_instruction_tokens"]
                ),
                estimated_text_tokens=cast(int, manifest_row["estimated_text_tokens"]),
                estimated_image_tokens=cast(int, manifest_row["estimated_image_tokens"]),
                estimated_structural_tokens=cast(int, manifest_row["estimated_structural_tokens"]),
                items=items,
                omissions=omissions,
                source_revision_vector_sha256=bytes(
                    manifest_row["source_revision_vector_sha256"]
                ).hex(),
                manifest_sha256=bytes(manifest_row["manifest_sha256"]).hex(),
            )
        except KeyError, TypeError, ValueError, OverflowError:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID") from None
        if (
            manifest_row["input_token_estimate"] != manifest.input_token_estimate
            or manifest_row["image_count"]
            != sum(item.image_detail is not None for item in manifest.items)
            or manifest_row["omission_count"] != len(manifest.omissions)
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID")
        return manifest, item_rows

    async def _load_exact_manifest_sources(
        self,
        *,
        row: RowMapping,
        manifest: ContextManifest,
        item_rows: tuple[RowMapping, ...],
        now: datetime,
    ) -> tuple[tuple[ContextSource, ...], tuple[RuntimeImageSnapshot, ...]]:
        if len(manifest.items) != len(item_rows):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID")
        sources: list[ContextSource] = []
        images: list[RuntimeImageSnapshot] = []
        for item, item_row in zip(manifest.items, item_rows, strict=True):
            content, image = await self._load_exact_source(
                run=row,
                item=item,
                item_row=item_row,
                now=now,
            )
            selection_reasons = tuple(
                reason for reason in item.reasons if not reason.startswith("budget_borrowed_from:")
            )
            try:
                source = ContextSource(
                    Candidate(
                        source_id=UUID(item.source_id),
                        source_revision=item.source_revision,
                        source_root=f"manifest:{item.source_type}:{item.source_id}",
                        layer=ContextLayer(item.layer),
                        occurred_at=None,
                        token_estimate=item.token_estimate,
                        base_score=item.base_score,
                        final_score=item.final_score,
                        rank_position=item.rank_position,
                    ),
                    canonical_role=item.canonical_role,
                    source_actor=item.source_actor,
                    trust_level=TrustLevel(item.trust_level),
                    content=SensitiveValue(content),
                    source_type=item.source_type,
                    selection_reasons=selection_reasons,
                    image_detail=item.image_detail,
                    image_tokens=item.estimated_image_tokens,
                )
            except TypeError, ValueError:
                raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID") from None
            sources.append(source)
            if image is not None:
                images.append(image)
        if len({image.object_id for image in images}) != len(images):
            raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
        return tuple(sources), tuple(images)

    async def _load_exact_source(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        item_row: RowMapping,
        now: datetime,
    ) -> tuple[str, RuntimeImageSnapshot | None]:
        try:
            source_id = UUID(item.source_id)
        except AttributeError, TypeError, ValueError:
            # Persisted manifests are untrusted at this boundary; never expose
            # the parser's implementation-specific exception to the caller.
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID") from None
        typed_column = {
            "trusted_instruction": "prompt_version_id",
            "message_revision": "message_revision_id",
            "media_object": "media_object_id",
            "memory_version": "memory_version_id",
            "summary_version": "summary_version_id",
        }.get(item.source_type)
        if typed_column is None or item_row[typed_column] != source_id:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID")
        if item.source_type == "trusted_instruction":
            content = await self._load_exact_prompt(run=run, item=item, source_id=source_id)
            return content, None
        if item.source_type == "message_revision":
            content = await self._load_exact_message(run=run, item=item, source_id=source_id)
            return content, None
        if item.source_type == "media_object":
            return await self._load_exact_image(
                run=run,
                item=item,
                source_id=source_id,
                now=now,
            )
        if item.source_type == "memory_version":
            content = await self._load_exact_memory(run=run, item=item, source_id=source_id)
            return content, None
        if item.source_type == "summary_version":
            content = await self._load_exact_summary(run=run, item=item, source_id=source_id)
            return content, None
        raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SNAPSHOT_INVALID")

    async def _load_exact_prompt(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        source_id: UUID,
    ) -> str:
        source = (
            (
                await self._session.execute(
                    select(prompt_versions).where(
                        prompt_versions.c.id == source_id,
                        prompt_versions.c.logical_role == run["logical_role"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        content = str(source["template_body"])
        if (
            item.source_revision != f"version-{source['version_no']}"
            or source["template_sha256"] != bytes.fromhex(item.content_sha256)
            or run["prompt_bundle_sha256"] != source["template_sha256"]
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        self._validate_source_content(item, content)
        return content

    async def _load_exact_message(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        source_id: UUID,
    ) -> str:
        source = (
            (
                await self._session.execute(
                    select(
                        message_revisions.c.revision_no,
                        message_revisions.c.text_content,
                        message_revisions.c.caption,
                        message_revisions.c.content_sha256,
                        message_revisions.c.redacted_at,
                        messages.c.current_revision_no,
                        messages.c.deleted_at,
                        messages.c.is_tombstone,
                    )
                    .join(
                        messages,
                        and_(
                            messages.c.id == message_revisions.c.message_id,
                            messages.c.account_id == message_revisions.c.account_id,
                        ),
                    )
                    .where(
                        message_revisions.c.id == source_id,
                        message_revisions.c.account_id == run["account_id"],
                        messages.c.conversation_id == run["conversation_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        content = source["text_content"] or source["caption"]
        if (
            not isinstance(content, str)
            or source["redacted_at"] is not None
            or source["deleted_at"] is not None
            or source["is_tombstone"] is not False
            or source["current_revision_no"] != source["revision_no"]
            or item.source_revision != f"revision-{source['revision_no']}"
            or source["content_sha256"] != bytes.fromhex(item.content_sha256)
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        self._validate_source_content(item, content)
        return content

    async def _load_exact_image(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        source_id: UUID,
        now: datetime,
    ) -> tuple[str, RuntimeImageSnapshot]:
        source = (
            (
                await self._session.execute(
                    select(media_objects).where(
                        media_objects.c.id == source_id,
                        media_objects.c.account_id == run["account_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
        linked_ids = tuple(
            candidate
            for candidate in (source_id, cast(UUID | None, source["parent_object_id"]))
            if candidate is not None
        )
        active_link = await self._session.scalar(
            select(
                exists()
                .where(
                    message_media.c.account_id == run["account_id"],
                    message_media.c.media_object_id.in_(linked_ids),
                    message_revisions.c.id == message_media.c.message_revision_id,
                    message_revisions.c.account_id == message_media.c.account_id,
                    messages.c.id == message_revisions.c.message_id,
                    messages.c.account_id == message_revisions.c.account_id,
                    messages.c.conversation_id == run["conversation_id"],
                    messages.c.current_revision_no == message_revisions.c.revision_no,
                    messages.c.deleted_at.is_(None),
                    messages.c.is_tombstone.is_(False),
                    message_revisions.c.redacted_at.is_(None),
                    turn_messages.c.turn_id == run["turn_id"],
                    turn_messages.c.account_id == message_revisions.c.account_id,
                    turn_messages.c.message_id == message_revisions.c.message_id,
                    turn_messages.c.message_revision_no == message_revisions.c.revision_no,
                )
                .select_from(
                    message_media.join(
                        message_revisions,
                        and_(
                            message_revisions.c.id == message_media.c.message_revision_id,
                            message_revisions.c.account_id == message_media.c.account_id,
                        ),
                    )
                    .join(
                        messages,
                        and_(
                            messages.c.id == message_revisions.c.message_id,
                            messages.c.account_id == message_revisions.c.account_id,
                        ),
                    )
                    .join(
                        turn_messages,
                        and_(
                            turn_messages.c.message_id == message_revisions.c.message_id,
                            turn_messages.c.account_id == message_revisions.c.account_id,
                            turn_messages.c.message_revision_no == message_revisions.c.revision_no,
                        ),
                    )
                )
            )
        )
        digest = source["sha256"]
        storage_key = source["storage_key"]
        mime_type = source["validated_mime"]
        byte_size = source["byte_size"]
        expires_at = source["expires_at"]
        if (
            active_link is not True
            or source["object_kind"] != "provider_copy"
            or source["status"] != "ready"
            or not isinstance(storage_key, str)
            or not isinstance(digest, bytes)
            or not isinstance(mime_type, str)
            or type(byte_size) is not int
            or (expires_at is not None and expires_at <= now)
            or item.source_revision != f"sha256-{digest.hex()}"
        ):
            raise ModelRuntimeSnapshotError("MODEL_IMAGE_SNAPSHOT_INVALID")
        content = (
            f"[IMAGE media_object_id={source_id} sha256={digest.hex()} "
            f"mime={mime_type} width={source['width'] or ''} "
            f"height={source['height'] or ''} detail=auto]"
        )
        self._validate_source_content(item, content)
        return content, RuntimeImageSnapshot(
            source_id,
            storage_key,
            digest,
            mime_type,
            byte_size,
        )

    async def _load_exact_memory(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        source_id: UUID,
    ) -> str:
        source = (
            (
                await self._session.execute(
                    select(
                        memory_versions.c.version_no,
                        memory_versions.c.rendered_text,
                        memory_versions.c.redacted_at,
                        memories.c.current_version_no,
                        memories.c.status,
                    )
                    .join(
                        memories,
                        and_(
                            memories.c.id == memory_versions.c.memory_id,
                            memories.c.account_id == memory_versions.c.account_id,
                        ),
                    )
                    .where(
                        memory_versions.c.id == source_id,
                        memory_versions.c.account_id == run["account_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        content = source["rendered_text"]
        if (
            not isinstance(content, str)
            or source["redacted_at"] is not None
            or source["status"] != "active"
            or source["current_version_no"] != source["version_no"]
            or item.source_revision != f"version-{source['version_no']}"
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        self._validate_source_content(item, content)
        return content

    async def _load_exact_summary(
        self,
        *,
        run: RowMapping,
        item: ManifestItem,
        source_id: UUID,
    ) -> str:
        source = (
            (
                await self._session.execute(
                    select(
                        summary_versions.c.version_no,
                        summary_versions.c.content_text,
                        summary_versions.c.content_sha256,
                        summary_versions.c.invalidation_state,
                        summary_versions.c.redacted_at,
                        summaries.c.current_version_no,
                        summaries.c.status,
                    )
                    .join(
                        summaries,
                        and_(
                            summaries.c.id == summary_versions.c.summary_id,
                            summaries.c.account_id == summary_versions.c.account_id,
                        ),
                    )
                    .where(
                        summary_versions.c.id == source_id,
                        summary_versions.c.account_id == run["account_id"],
                        summaries.c.conversation_id == run["conversation_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        content = source["content_text"]
        if (
            not isinstance(content, str)
            or source["redacted_at"] is not None
            or source["invalidation_state"] != "active"
            or source["status"] != "active"
            or source["current_version_no"] != source["version_no"]
            or item.source_revision != f"version-{source['version_no']}"
            or source["content_sha256"] != bytes.fromhex(item.content_sha256)
        ):
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")
        self._validate_source_content(item, content)
        return content

    @staticmethod
    def _validate_source_content(item: ManifestItem, content: str) -> None:
        if not content or hashlib.sha256(content.encode()).hexdigest() != item.content_sha256:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_SOURCE_INVALID")

    async def _load_orchestration_claim_fingerprint(self, row: RowMapping) -> bytes:
        membership = tuple(
            (
                cast(UUID, item.message_id),
                cast(int, item.message_revision_no),
            )
            for item in (
                await self._session.execute(
                    select(
                        turn_messages.c.message_id,
                        turn_messages.c.message_revision_no,
                    )
                    .where(
                        turn_messages.c.turn_id == row["turn_id"],
                        turn_messages.c.account_id == row["account_id"],
                    )
                    .order_by(turn_messages.c.ordinal)
                )
            )
        )
        return orchestration_claim_fingerprint(membership)

    async def _load_exact_run(
        self,
        *,
        run_id: UUID,
        expected_logical_role: LogicalRole,
        now: datetime,
        prepared: bool | None,
    ) -> RowMapping:
        if expected_logical_role not in GENERATION_ROLES:
            raise ModelRuntimeSnapshotError("MODEL_GENERATION_ROLE_INVALID")
        config = model_config_versions.alias("runtime_config")
        capability = model_capability_snapshots.alias("runtime_capability")
        statement = (
            select(
                *model_runs.c,
                model_runs.c.capability_snapshot_sha256.label("run_capability_sha256"),
                config.c.endpoint_id,
                config.c.credential_id,
                config.c.protocol,
                config.c.model_name,
                config.c.temperature,
                config.c.max_output_tokens,
                config.c.timeout_seconds,
                config.c.enabled,
                config.c.protocol_options,
                config.c.config_sha256,
                capability.c.endpoint_id.label("capability_endpoint_id"),
                capability.c.protocol.label("capability_protocol"),
                capability.c.model_name.label("capability_model_name"),
                capability.c.supports_text,
                capability.c.supports_temperature,
                capability.c.supports_reasoning_effort,
                capability.c.supports_image,
                capability.c.supports_stream,
                capability.c.supports_structured_output,
                capability.c.chat_token_limit_field,
                capability.c.max_context_tokens,
                capability.c.max_output_tokens_limit,
                capability.c.max_images_per_request,
                capability.c.max_image_bytes_per_request,
                capability.c.auto_image_tokens,
                capability.c.messages_auto_detail_equivalent,
                capability.c.supported_input_roles,
                capability.c.embedding_dimensions,
                capability.c.metadata_schema_version,
                capability.c.metadata,
                capability.c.status.label("capability_status"),
                capability.c.observed_at,
                capability.c.expires_at.label("capability_expires_at"),
                model_endpoints.c.base_url,
                model_endpoints.c.canonical_sha256.label("endpoint_canonical_sha256"),
                model_endpoints.c.network_policy_id,
                model_endpoints.c.network_policy_version,
                model_endpoints.c.network_category,
                conversation_turns.c.state.label("turn_state"),
            )
            .join(model_profiles, model_profiles.c.id == model_runs.c.model_profile_id)
            .join(config, config.c.id == model_runs.c.config_version_id)
            .join(
                capability,
                capability.c.id == config.c.capability_snapshot_id,
            )
            .join(model_endpoints, model_endpoints.c.id == config.c.endpoint_id)
            .join(conversation_turns, conversation_turns.c.id == model_runs.c.turn_id)
            .where(
                model_runs.c.id == run_id,
                model_runs.c.state == "running",
                model_runs.c.cancel_requested_at.is_(None),
                model_runs.c.logical_role == expected_logical_role.value,
                model_profiles.c.logical_role == model_runs.c.logical_role,
                config.c.profile_id == model_runs.c.model_profile_id,
                capability.c.endpoint_id == config.c.endpoint_id,
                capability.c.protocol == config.c.protocol,
                capability.c.model_name == config.c.model_name,
                conversation_turns.c.state == "generating",
            )
            .with_for_update(of=model_runs)
        )
        if prepared is True:
            statement = statement.where(
                model_runs.c.context_manifest_id.is_not(None),
            )
        elif prepared is False:
            statement = statement.where(
                model_runs.c.context_manifest_id.is_(None),
                capability.c.expires_at > now,
            )
        persisted = (await self._session.execute(statement)).mappings().one_or_none()
        if persisted is None:
            raise ModelRuntimeSnapshotError("MODEL_RUN_SNAPSHOT_UNAVAILABLE")
        credential = await load_runtime_credential(
            self._session,
            run_id=run_id,
            profile_id=cast(UUID, persisted["model_profile_id"]),
            credential_version_id=cast(UUID, persisted["credential_version_id"]),
        )
        row = dict(persisted)
        row.update(credential)
        if (
            row["credential_version_id_from_accessor"] != row["credential_version_id"]
            or row["credential_profile_id"] != row["model_profile_id"]
            or row["credential_id_from_accessor"] != row["credential_id"]
        ):
            raise ModelRuntimeSnapshotError("MODEL_RUN_SNAPSHOT_INVALID")
        has_manifest = row["context_manifest_id"] is not None
        if prepared is None and not has_manifest and row["capability_expires_at"] <= now:
            raise ModelRuntimeSnapshotError("MODEL_RUN_SNAPSHOT_INVALID")
        return cast(RowMapping, row)

    @staticmethod
    def _validate_exact_snapshot(
        row: RowMapping,
        config: CanonicalModelConfig,
        capabilities: ModelCapabilities,
        *,
        now: datetime,
    ) -> None:
        del now
        if (
            not config.enabled
            or row["capability_status"] != "valid"
            or row["credential_destroyed_at"] is not None
            or any(
                row[name] is None
                for name in (
                    "credential_nonce",
                    "credential_ciphertext",
                    "credential_secret_fingerprint",
                )
            )
            or hashlib.sha256(str(row["base_url"]).encode()).digest()
            != row["endpoint_canonical_sha256"]
            or model_config_digest(config) != row["config_sha256"]
            or capability_digest_from_row(cast(Mapping[str, object], row))
            != row["run_capability_sha256"]
        ):
            raise ModelRuntimeSnapshotError("MODEL_RUN_SNAPSHOT_INVALID")
        validate_activation(config, capabilities)

    async def _load_context_policy(self, row: RowMapping) -> tuple[UUID, ContextPolicy]:
        policy = (
            (
                await self._session.execute(
                    select(context_policy_versions)
                    .join(
                        context_policies,
                        and_(
                            context_policies.c.id == context_policy_versions.c.policy_id,
                            context_policies.c.active_version_id == context_policy_versions.c.id,
                        ),
                    )
                    .where(
                        context_policies.c.logical_role == row["logical_role"],
                        context_policies.c.purpose == row["purpose"],
                        context_policy_versions.c.status == "active",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if policy is None:
            raise ModelRuntimeSnapshotError("MODEL_CONTEXT_POLICY_UNAVAILABLE")
        version = f"context-v{policy['version_no']}"
        return cast(UUID, policy["id"]), ContextPolicy(
            version=version,
            max_input_tokens=cast(int, policy["max_input_tokens"]),
            safety_reserve_basis_points=cast(int, policy["safety_reserve_basis_points"]),
            minimum_safety_reserve_tokens=cast(int, policy["minimum_safety_reserve_tokens"]),
            current_budget_basis_points=cast(int, policy["current_budget_basis_points"]),
            recent_budget_basis_points=cast(int, policy["recent_budget_basis_points"]),
            profile_budget_basis_points=cast(int, policy["profile_budget_basis_points"]),
            structured_budget_basis_points=cast(int, policy["structured_budget_basis_points"]),
            semantic_budget_basis_points=cast(int, policy["semantic_budget_basis_points"]),
            summary_budget_basis_points=cast(int, policy["summary_budget_basis_points"]),
            structured_limit=cast(int, policy["structured_limit"]),
            semantic_limit=cast(int, policy["semantic_limit"]),
            ann_candidate_limit=cast(int, policy["ann_candidate_limit"]),
            current_image_limit=cast(int, policy["current_image_limit"]),
            fallback_auto_image_tokens=cast(int, policy["fallback_auto_image_tokens"]),
        )

    async def _load_retrieval_policy(self) -> tuple[UUID, str]:
        row = (
            (
                await self._session.execute(
                    select(retrieval_policy_versions)
                    .join(
                        retrieval_policies,
                        and_(
                            retrieval_policies.c.id == retrieval_policy_versions.c.policy_id,
                            retrieval_policies.c.active_version_id
                            == retrieval_policy_versions.c.id,
                        ),
                    )
                    .where(retrieval_policy_versions.c.status == "active")
                    .order_by(retrieval_policies.c.policy_name)
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ModelRuntimeSnapshotError("MODEL_RETRIEVAL_POLICY_UNAVAILABLE")
        return cast(UUID, row["id"]), f"retrieval-v{row['version_no']}"

    async def _load_prompt(self, row: RowMapping) -> RowMapping:
        prompt = (
            (
                await self._session.execute(
                    select(prompt_versions).where(
                        prompt_versions.c.logical_role == row["logical_role"],
                        prompt_versions.c.template_sha256 == row["prompt_bundle_sha256"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            prompt is None
            or hashlib.sha256(str(prompt["template_body"]).encode()).digest()
            != prompt["template_sha256"]
            or row["prompt_version"] != f"main-ai-v{prompt['version_no']}"
        ):
            raise ModelRuntimeSnapshotError("MODEL_PROMPT_SNAPSHOT_INVALID")
        return prompt

    async def _load_sources(
        self,
        row: RowMapping,
        prompt: RowMapping,
        policy: ContextPolicy,
        capabilities: ModelCapabilities,
        *,
        now: datetime,
    ) -> tuple[tuple[ContextSource, ...], tuple[RuntimeImageSnapshot, ...]]:
        prompt_text = str(prompt["template_body"])
        instruction = ContextSource(
            Candidate(
                cast(UUID, prompt["id"]),
                f"version-{prompt['version_no']}",
                f"prompt:{prompt['id']}",
                ContextLayer.INSTRUCTION,
                None,
                estimate_utf8_bytes_v1(prompt_text),
            ),
            "system",
            "packaged_prompt",
            TrustLevel.SYSTEM,
            SensitiveValue(prompt_text),
            "trusted_instruction",
        )
        current_rows = await self._message_rows(
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            turn_id=cast(UUID, row["turn_id"]),
            current=True,
        )
        current = tuple(_message_source(item, layer=ContextLayer.CURRENT) for item in current_rows)
        current_ids = tuple(cast(UUID, item["message_id"]) for item in current_rows)
        recent_rows = await self._message_rows(
            account_id=cast(UUID, row["account_id"]),
            conversation_id=cast(UUID, row["conversation_id"]),
            turn_id=cast(UUID, row["turn_id"]),
            current=False,
            excluded_message_ids=current_ids,
        )
        recent = tuple(_message_source(item, layer=ContextLayer.RECENT) for item in recent_rows)
        memory = await self._memory_sources(row, policy)
        summary = await self._summary_sources(row)
        image_sources, images = await self._image_sources(
            row,
            capabilities=capabilities,
            image_limit=min(policy.current_image_limit, capabilities.max_images_per_request),
            now=now,
        )
        return (instruction, *memory, *summary, *recent, *current, *image_sources), images

    async def _message_rows(
        self,
        *,
        account_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        current: bool,
        excluded_message_ids: tuple[UUID, ...] = (),
    ) -> tuple[RowMapping, ...]:
        if current:
            statement = (
                select(
                    messages.c.id.label("message_id"),
                    messages.c.direction,
                    messages.c.role,
                    messages.c.source,
                    messages.c.telegram_created_at,
                    message_revisions.c.id.label("revision_id"),
                    message_revisions.c.revision_no,
                    message_revisions.c.text_content,
                    message_revisions.c.caption,
                    message_revisions.c.content_sha256,
                )
                .join(
                    turn_messages,
                    and_(
                        turn_messages.c.message_id == messages.c.id,
                        turn_messages.c.account_id == messages.c.account_id,
                    ),
                )
                .join(
                    message_revisions,
                    and_(
                        message_revisions.c.message_id == turn_messages.c.message_id,
                        message_revisions.c.account_id == turn_messages.c.account_id,
                        message_revisions.c.revision_no == turn_messages.c.message_revision_no,
                    ),
                )
                .where(
                    turn_messages.c.turn_id == turn_id,
                    messages.c.account_id == account_id,
                    messages.c.conversation_id == conversation_id,
                    messages.c.current_revision_no == message_revisions.c.revision_no,
                    messages.c.deleted_at.is_(None),
                    messages.c.is_tombstone.is_(False),
                    message_revisions.c.redacted_at.is_(None),
                    or_(
                        message_revisions.c.text_content.is_not(None),
                        message_revisions.c.caption.is_not(None),
                    ),
                )
                .order_by(turn_messages.c.ordinal)
            )
        else:
            statement = (
                select(
                    messages.c.id.label("message_id"),
                    messages.c.direction,
                    messages.c.role,
                    messages.c.source,
                    messages.c.telegram_created_at,
                    message_revisions.c.id.label("revision_id"),
                    message_revisions.c.revision_no,
                    message_revisions.c.text_content,
                    message_revisions.c.caption,
                    message_revisions.c.content_sha256,
                )
                .join(
                    message_revisions,
                    and_(
                        message_revisions.c.message_id == messages.c.id,
                        message_revisions.c.account_id == messages.c.account_id,
                        message_revisions.c.revision_no == messages.c.current_revision_no,
                    ),
                )
                .where(
                    messages.c.account_id == account_id,
                    messages.c.conversation_id == conversation_id,
                    messages.c.deleted_at.is_(None),
                    messages.c.is_tombstone.is_(False),
                    message_revisions.c.redacted_at.is_(None),
                    or_(
                        message_revisions.c.text_content.is_not(None),
                        message_revisions.c.caption.is_not(None),
                    ),
                )
                .order_by(messages.c.telegram_created_at.desc(), messages.c.id.desc())
                .limit(MAX_RECENT_MESSAGES)
            )
            if excluded_message_ids:
                statement = statement.where(messages.c.id.not_in(excluded_message_ids))
        return tuple((await self._session.execute(statement)).mappings())

    async def _memory_sources(
        self, row: RowMapping, policy: ContextPolicy
    ) -> tuple[ContextSource, ...]:
        contact_id = await self._session.scalar(
            select(conversations.c.contact_id).where(
                conversations.c.id == row["conversation_id"],
                conversations.c.account_id == row["account_id"],
            )
        )
        statement = (
            select(
                memories.c.id.label("memory_id"),
                memories.c.memory_type,
                memory_versions.c.id.label("version_id"),
                memory_versions.c.version_no,
                memory_versions.c.rendered_text,
                memory_versions.c.importance,
                memory_versions.c.confidence,
                memory_versions.c.observed_at,
                memory_versions.c.created_at,
            )
            .join(
                memory_versions,
                and_(
                    memory_versions.c.memory_id == memories.c.id,
                    memory_versions.c.account_id == memories.c.account_id,
                    memory_versions.c.version_no == memories.c.current_version_no,
                ),
            )
            .where(
                memories.c.account_id == row["account_id"],
                memories.c.status == "active",
                memory_versions.c.redacted_at.is_(None),
                memory_versions.c.rendered_text.is_not(None),
                or_(
                    memories.c.conversation_id == row["conversation_id"],
                    memories.c.contact_id == contact_id if contact_id is not None else false(),
                    and_(memories.c.contact_id.is_(None), memories.c.conversation_id.is_(None)),
                ),
            )
            .order_by(
                memory_versions.c.importance.desc(),
                memory_versions.c.confidence.desc(),
                memory_versions.c.created_at.desc(),
                memory_versions.c.id,
            )
            .limit(MAX_CONTEXT_MEMORIES)
        )
        rows = tuple((await self._session.execute(statement)).mappings())
        sources: list[ContextSource] = []
        structured_candidates: list[Candidate] = []
        by_id: dict[UUID, RowMapping] = {}
        for item in rows:
            text = cast(str, item["rendered_text"])
            layer = memory_context_layer(str(item["memory_type"]))
            source_id = cast(UUID, item["version_id"])
            candidate = Candidate(
                source_id,
                f"version-{item['version_no']}",
                f"memory:{item['memory_id']}",
                layer,
                cast(datetime | None, item["observed_at"] or item["created_at"]),
                estimate_utf8_bytes_v1(text),
                importance=float(item["importance"]),
                confidence=float(item["confidence"]),
                freshness=1.0,
                source_quality=1.0,
                semantic_key=str(item["memory_id"]),
            )
            if layer is ContextLayer.STRUCTURED_MEMORY:
                structured_candidates.append(candidate)
                by_id[source_id] = item
                continue
            sources.append(
                ContextSource(
                    candidate,
                    "user",
                    "accepted_memory",
                    TrustLevel.TRUSTED_DERIVED,
                    SensitiveValue(text),
                    "memory_version",
                )
            )
        for candidate in select_structured(
            tuple(structured_candidates), limit=policy.structured_limit
        ):
            item = by_id[candidate.source_id]
            sources.append(
                ContextSource(
                    candidate,
                    "user",
                    "accepted_memory",
                    TrustLevel.TRUSTED_DERIVED,
                    SensitiveValue(cast(str, item["rendered_text"])),
                    "memory_version",
                )
            )
        return tuple(sources)

    async def _summary_sources(self, row: RowMapping) -> tuple[ContextSource, ...]:
        statement = (
            select(
                summaries.c.id.label("summary_id"),
                summary_versions.c.id.label("version_id"),
                summary_versions.c.version_no,
                summary_versions.c.content_text,
                summary_versions.c.created_at,
            )
            .join(
                summary_versions,
                and_(
                    summary_versions.c.summary_id == summaries.c.id,
                    summary_versions.c.account_id == summaries.c.account_id,
                    summary_versions.c.version_no == summaries.c.current_version_no,
                ),
            )
            .where(
                summaries.c.account_id == row["account_id"],
                summaries.c.conversation_id == row["conversation_id"],
                summaries.c.status == "active",
                summary_versions.c.invalidation_state == "active",
                summary_versions.c.redacted_at.is_(None),
                summary_versions.c.content_text.is_not(None),
            )
            .order_by(summary_versions.c.created_at.desc(), summary_versions.c.id)
            .limit(MAX_CONTEXT_SUMMARIES)
        )
        output = []
        for item in (await self._session.execute(statement)).mappings():
            content = cast(str, item["content_text"])
            output.append(
                ContextSource(
                    Candidate(
                        cast(UUID, item["version_id"]),
                        f"version-{item['version_no']}",
                        f"summary:{item['summary_id']}",
                        ContextLayer.SUMMARY,
                        cast(datetime, item["created_at"]),
                        estimate_utf8_bytes_v1(content),
                    ),
                    "user",
                    "accepted_summary",
                    TrustLevel.TRUSTED_DERIVED,
                    SensitiveValue(content),
                    "summary_version",
                )
            )
        return tuple(output)

    async def _image_sources(
        self,
        row: RowMapping,
        *,
        capabilities: ModelCapabilities,
        image_limit: int,
        now: datetime,
    ) -> tuple[tuple[ContextSource, ...], tuple[RuntimeImageSnapshot, ...]]:
        statement = (
            select(
                media_objects.c.id,
                media_objects.c.storage_key,
                media_objects.c.sha256,
                media_objects.c.validated_mime,
                media_objects.c.byte_size,
                media_objects.c.width,
                media_objects.c.height,
            )
            .join(
                message_media,
                and_(
                    message_media.c.media_object_id == media_objects.c.id,
                    message_media.c.account_id == media_objects.c.account_id,
                ),
            )
            .join(
                message_revisions,
                and_(
                    message_revisions.c.id == message_media.c.message_revision_id,
                    message_revisions.c.account_id == message_media.c.account_id,
                ),
            )
            .join(
                turn_messages,
                and_(
                    turn_messages.c.message_id == message_revisions.c.message_id,
                    turn_messages.c.account_id == message_revisions.c.account_id,
                    turn_messages.c.message_revision_no == message_revisions.c.revision_no,
                ),
            )
            .join(
                messages,
                and_(
                    messages.c.id == message_revisions.c.message_id,
                    messages.c.account_id == message_revisions.c.account_id,
                    messages.c.current_revision_no == message_revisions.c.revision_no,
                ),
            )
            .where(
                turn_messages.c.turn_id == row["turn_id"],
                media_objects.c.account_id == row["account_id"],
                media_objects.c.object_kind == "provider_copy",
                media_objects.c.status == "ready",
                media_objects.c.storage_key.is_not(None),
                media_objects.c.sha256.is_not(None),
                media_objects.c.validated_mime.in_(("image/jpeg", "image/png", "image/webp")),
                media_objects.c.byte_size > 0,
                media_objects.c.byte_size <= capabilities.max_image_bytes_per_request,
                or_(media_objects.c.expires_at.is_(None), media_objects.c.expires_at > now),
                messages.c.deleted_at.is_(None),
                messages.c.is_tombstone.is_(False),
                message_revisions.c.redacted_at.is_(None),
            )
            .order_by(turn_messages.c.ordinal, message_media.c.position, media_objects.c.id)
            .limit(image_limit)
        )
        sources: list[ContextSource] = []
        images: list[RuntimeImageSnapshot] = []
        seen: set[UUID] = set()
        for item in (await self._session.execute(statement)).mappings():
            object_id = cast(UUID, item["id"])
            if object_id in seen:
                continue
            seen.add(object_id)
            digest = cast(bytes, item["sha256"])
            metadata = (
                f"[IMAGE media_object_id={object_id} sha256={digest.hex()} "
                f"mime={item['validated_mime']} width={item['width'] or ''} "
                f"height={item['height'] or ''} detail=auto]"
            )
            sources.append(
                ContextSource(
                    Candidate(
                        object_id,
                        f"sha256-{digest.hex()}",
                        f"media:{object_id}",
                        ContextLayer.CURRENT,
                        None,
                        estimate_utf8_bytes_v1(metadata),
                    ),
                    "user",
                    "contact",
                    TrustLevel.UNTRUSTED_USER,
                    SensitiveValue(metadata),
                    "media_object",
                    image_detail="auto",
                    image_tokens=capabilities.auto_image_tokens,
                )
            )
            images.append(
                RuntimeImageSnapshot(
                    object_id,
                    cast(str, item["storage_key"]),
                    digest,
                    cast(str, item["validated_mime"]),
                    cast(int, item["byte_size"]),
                )
            )
        return tuple(sources), tuple(images)


__all__ = [
    "MODEL_CONTEXT_BUILDER_VERSION",
    "MODEL_INPUT_FINGERPRINT_VERSION",
    "ModelRuntimeRepository",
    "ModelRuntimeSnapshotError",
    "PreparedModelGeneration",
    "RuntimeEndpointSnapshot",
    "RuntimeImageSnapshot",
    "RuntimeProactiveOutputScope",
    "background_orchestration_claim_fingerprint",
    "canonical_config_from_row",
    "canonical_input_fingerprint",
    "capability_digest_from_row",
    "load_runtime_credential",
    "memory_context_layer",
    "model_capabilities_from_row",
    "model_config_digest",
    "orchestration_claim_fingerprint",
]
