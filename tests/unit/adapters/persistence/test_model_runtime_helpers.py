from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, time
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import model_runtime
from telegram_userbot.adapters.persistence.model_runtime import (
    ModelRuntimeSnapshotError,
    RuntimeImageSnapshot,
    RuntimeProactiveOutputScope,
    background_orchestration_claim_fingerprint,
    canonical_config_from_row,
    canonical_input_fingerprint,
    capability_digest_from_row,
    load_runtime_credential,
    memory_context_layer,
    model_capabilities_from_row,
    model_config_digest,
    orchestration_claim_fingerprint,
)
from telegram_userbot.domain.context import (
    Candidate,
    ContextLayer,
    ContextSource,
    ManifestItem,
    TrustLevel,
)
from telegram_userbot.domain.model_config import LogicalRole, ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
PROFILE_ID = UUID("01900000-0000-7000-8000-000000000001")
ENDPOINT_ID = UUID("01900000-0000-7000-8000-000000000002")
CREDENTIAL_ID = UUID("01900000-0000-7000-8000-000000000003")
RUN_ID = UUID("01900000-0000-7000-8000-000000000004")
IMAGE_ID = UUID("01900000-0000-7000-8000-000000000005")


def _config_row() -> dict[str, object]:
    return {
        "model_profile_id": PROFILE_ID,
        "logical_role": "main_ai",
        "endpoint_id": ENDPOINT_ID,
        "credential_id": CREDENTIAL_ID,
        "protocol": "openai_responses",
        "model_name": " model-a ",
        "temperature": "0.25",
        "max_output_tokens": 512,
        "timeout_seconds": 30,
        "enabled": True,
        "protocol_options": {"reasoning_effort": "low"},
    }


def _capability_row() -> dict[str, object]:
    return {
        "endpoint_id": ENDPOINT_ID,
        "protocol": "openai_responses",
        "model_name": "model-a",
        "supports_text": True,
        "supports_temperature": True,
        "supports_reasoning_effort": True,
        "supports_image": True,
        "supports_stream": False,
        "supports_structured_output": True,
        "chat_token_limit_field": None,
        "max_context_tokens": 8192,
        "max_output_tokens_limit": 2048,
        "max_images_per_request": 4,
        "max_image_bytes_per_request": 4_000_000,
        "auto_image_tokens": 256,
        "messages_auto_detail_equivalent": True,
        "supported_input_roles": ("user", "assistant"),
        "embedding_dimensions": (1536,),
        "metadata_schema_version": 1,
        "metadata": {"region": "test", "revision": 1},
        "observed_at": NOW,
        "expires_at": None,
        "capability_protocol": "openai_responses",
    }


def _text_source(content: str = "hello") -> ContextSource:
    return ContextSource(
        Candidate(
            UUID("01900000-0000-7000-8000-000000000010"),
            "revision-1",
            "message:10",
            ContextLayer.CURRENT,
            NOW,
            len(content.encode()),
        ),
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue(content),
        "message_revision",
    )


def _image_source(*, image_id: UUID = IMAGE_ID, sha256: bytes = b"i" * 32) -> ContextSource:
    return ContextSource(
        Candidate(
            image_id,
            f"sha256-{sha256.hex()}",
            f"media:{image_id}",
            ContextLayer.CURRENT,
            NOW,
            1,
        ),
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue("image"),
        "media_object",
        image_detail="auto",
        image_tokens=256,
    )


def _image(*, image_id: UUID = IMAGE_ID, sha256: bytes = b"i" * 32) -> RuntimeImageSnapshot:
    return RuntimeImageSnapshot(
        object_id=image_id,
        storage_key="media/01.png",
        sha256=sha256,
        mime_type="image/png",
        byte_size=100,
    )


@pytest.mark.unit
def test_config_and_capability_rows_become_canonical_runtime_snapshots() -> None:
    config = canonical_config_from_row(_config_row())
    capabilities = model_capabilities_from_row(config, _capability_row())

    assert config.logical_role is LogicalRole.MAIN_AI
    assert config.protocol is ModelProtocol.OPENAI_RESPONSES
    assert config.model_name == "model-a"
    assert config.temperature == 0.25
    assert capabilities.supported_protocols == frozenset({ModelProtocol.OPENAI_RESPONSES})
    assert capabilities.max_context_tokens == 8192
    assert capabilities.supported_input_roles == frozenset({"user", "assistant"})


@pytest.mark.unit
def test_capability_digest_rejects_invalid_aliased_snapshot() -> None:
    row = _capability_row()
    row["capability_endpoint_id"] = row.pop("endpoint_id")
    row["capability_model_name"] = row.pop("model_name")

    digest = capability_digest_from_row(row)
    assert len(digest) == 32

    row["metadata"] = {"unsupported": b"binary"}
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CAPABILITY_SNAPSHOT_INVALID"):
        capability_digest_from_row(row)


@pytest.mark.unit
def test_config_digest_is_stable_and_captures_semantic_changes() -> None:
    config = canonical_config_from_row(_config_row())
    assert model_config_digest(config) == model_config_digest(config)

    changed = _config_row()
    changed["temperature"] = 0.5
    assert model_config_digest(canonical_config_from_row(changed)) != model_config_digest(config)


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"storage_key": ""},
        {"sha256": b"short"},
        {"mime_type": "image/gif"},
        {"byte_size": 0},
    ],
)
def test_image_snapshot_rejects_invalid_durable_media_shape(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "object_id": IMAGE_ID,
        "storage_key": "media/01.png",
        "sha256": b"i" * 32,
        "mime_type": "image/png",
        "byte_size": 100,
    }
    values.update(kwargs)

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_IMAGE_SNAPSHOT_INVALID"):
        RuntimeImageSnapshot(**cast(Any, values))


@pytest.mark.unit
def test_proactive_scope_requires_candidates_and_aware_window() -> None:
    scope = RuntimeProactiveOutputScope(
        candidate_id=UUID("01900000-0000-7000-8000-000000000020"),
        occurrence_ids=frozenset({UUID("01900000-0000-7000-8000-000000000021")}),
        window_end_at=NOW,
        timezone_name="Asia/Shanghai",
        absolute_no_send_start_local=time(23),
        absolute_no_send_end_local=time(7),
    )
    assert scope.timezone_name == "Asia/Shanghai"

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_PROACTIVE_SCOPE_INVALID"):
        RuntimeProactiveOutputScope(
            candidate_id=scope.candidate_id,
            occurrence_ids=frozenset(),
            window_end_at=datetime(2030, 1, 2, 3, 4, 5),  # noqa: DTZ001
            timezone_name="",
            absolute_no_send_start_local=time(23),
            absolute_no_send_end_local=time(7),
        )


@pytest.mark.unit
def test_canonical_input_fingerprint_binds_text_images_and_each_snapshot() -> None:
    text_source = _text_source()
    image_source = _image_source()
    image = _image()
    kwargs = {
        "key": SensitiveValue(b"k" * 32),
        "run_id": RUN_ID,
        "purpose": "conversation_reply",
        "config_sha256": b"c" * 32,
        "capability_sha256": b"p" * 32,
        "manifest_sha256": b"m" * 32,
        "sources": (text_source, image_source),
        "images": (image,),
    }

    fingerprint = canonical_input_fingerprint(
        key=cast(SensitiveValue[bytes], kwargs["key"]),
        run_id=cast(UUID, kwargs["run_id"]),
        purpose=cast(str, kwargs["purpose"]),
        config_sha256=cast(bytes, kwargs["config_sha256"]),
        capability_sha256=cast(bytes, kwargs["capability_sha256"]),
        manifest_sha256=cast(bytes, kwargs["manifest_sha256"]),
        sources=cast(tuple[ContextSource, ...], kwargs["sources"]),
        images=cast(tuple[RuntimeImageSnapshot, ...], kwargs["images"]),
    )
    assert len(fingerprint) == 32
    assert fingerprint == canonical_input_fingerprint(
        key=cast(SensitiveValue[bytes], kwargs["key"]),
        run_id=cast(UUID, kwargs["run_id"]),
        purpose=cast(str, kwargs["purpose"]),
        config_sha256=cast(bytes, kwargs["config_sha256"]),
        capability_sha256=cast(bytes, kwargs["capability_sha256"]),
        manifest_sha256=cast(bytes, kwargs["manifest_sha256"]),
        sources=cast(tuple[ContextSource, ...], kwargs["sources"]),
        images=cast(tuple[RuntimeImageSnapshot, ...], kwargs["images"]),
    )

    changed = dict(kwargs)
    changed["sources"] = (_text_source("changed"), image_source)
    assert (
        canonical_input_fingerprint(
            key=cast(SensitiveValue[bytes], changed["key"]),
            run_id=cast(UUID, changed["run_id"]),
            purpose=cast(str, changed["purpose"]),
            config_sha256=cast(bytes, changed["config_sha256"]),
            capability_sha256=cast(bytes, changed["capability_sha256"]),
            manifest_sha256=cast(bytes, changed["manifest_sha256"]),
            sources=cast(tuple[ContextSource, ...], changed["sources"]),
            images=cast(tuple[RuntimeImageSnapshot, ...], changed["images"]),
        )
        != fingerprint
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "sources", "images"),
    [
        (SensitiveValue(b"k" * 31), (_text_source(),), ()),
        (SensitiveValue(b"k" * 32), (_text_source(),), (_image(),)),
        (SensitiveValue(b"k" * 32), (_image_source(),), ()),
        (SensitiveValue(b"k" * 32), (_image_source(),), (_image(), _image())),
    ],
)
def test_canonical_input_fingerprint_fails_closed_for_incomplete_image_bindings(
    key: SensitiveValue[bytes],
    sources: tuple[ContextSource, ...],
    images: tuple[RuntimeImageSnapshot, ...],
) -> None:
    with pytest.raises(ModelRuntimeSnapshotError) as error:
        canonical_input_fingerprint(
            key=key,
            run_id=RUN_ID,
            purpose="conversation_reply",
            config_sha256=b"c" * 32,
            capability_sha256=b"p" * 32,
            manifest_sha256=b"m" * 32,
            sources=sources,
            images=images,
        )
    assert error.value.code in {"MODEL_INPUT_HMAC_KEY_INVALID", "MODEL_IMAGE_SNAPSHOT_INVALID"}


@pytest.mark.unit
def test_orchestration_claims_are_deterministic_and_reject_invalid_membership() -> None:
    membership = ((UUID("01900000-0000-7000-8000-000000000030"), 1),)
    claim = orchestration_claim_fingerprint(membership)
    assert claim == orchestration_claim_fingerprint(membership)
    assert claim != orchestration_claim_fingerprint(((membership[0][0], 2),))

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_TURN_MEMBERSHIP_INVALID"):
        orchestration_claim_fingerprint(())
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_TURN_MEMBERSHIP_INVALID"):
        orchestration_claim_fingerprint(((membership[0][0], False),))


@pytest.mark.unit
def test_background_claim_is_owner_and_generation_specific() -> None:
    kwargs = {
        "owner_kind": "memory_job",
        "owner_id": UUID("01900000-0000-7000-8000-000000000040"),
        "purpose": "memory_episode",
        "generation_no": 1,
        "manifest_sha256": b"m" * 32,
    }
    claim = background_orchestration_claim_fingerprint(
        owner_kind=cast(str, kwargs["owner_kind"]),
        owner_id=cast(UUID, kwargs["owner_id"]),
        purpose=cast(str, kwargs["purpose"]),
        generation_no=cast(int, kwargs["generation_no"]),
        manifest_sha256=cast(bytes, kwargs["manifest_sha256"]),
    )
    assert claim != background_orchestration_claim_fingerprint(
        owner_kind=cast(str, kwargs["owner_kind"]),
        owner_id=cast(UUID, kwargs["owner_id"]),
        purpose=cast(str, kwargs["purpose"]),
        generation_no=2,
        manifest_sha256=cast(bytes, kwargs["manifest_sha256"]),
    )

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_BACKGROUND_CLAIM_INVALID"):
        background_orchestration_claim_fingerprint(
            owner_kind="turn",
            owner_id=cast(UUID, kwargs["owner_id"]),
            purpose=cast(str, kwargs["purpose"]),
            generation_no=cast(int, kwargs["generation_no"]),
            manifest_sha256=cast(bytes, kwargs["manifest_sha256"]),
        )


class _CredentialResult:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _CredentialResult:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._row


class _CredentialSession:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self._row = row
        self.parameters: Mapping[str, object] | None = None

    async def execute(self, _: object, parameters: Mapping[str, object]) -> _CredentialResult:
        self.parameters = parameters
        return _CredentialResult(self._row)


def _credential_row() -> dict[str, object]:
    return {
        "id": UUID("01900000-0000-7000-8000-000000000050"),
        "credential_id": CREDENTIAL_ID,
        "profile_id": PROFILE_ID,
        "version_no": 3,
        "algorithm": "aes_256_gcm",
        "key_version": 1,
        "aad_schema_version": 1,
        "nonce": b"n" * 12,
        "ciphertext": b"c" * 16,
        "secret_fingerprint": b"s" * 32,
    }


@pytest.mark.unit
async def test_runtime_credential_is_read_only_through_the_scoped_accessor() -> None:
    session = _CredentialSession(_credential_row())
    credential = await load_runtime_credential(
        cast(AsyncSession, session),
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        credential_version_id=UUID("01900000-0000-7000-8000-000000000050"),
    )

    assert credential["credential_id_from_accessor"] == CREDENTIAL_ID
    assert credential["credential_destroyed_at"] is None
    assert session.parameters == {
        "run_id": RUN_ID,
        "profile_id": PROFILE_ID,
        "credential_version_id": UUID("01900000-0000-7000-8000-000000000050"),
    }


@pytest.mark.unit
async def test_runtime_credential_fails_closed_when_accessor_returns_no_row() -> None:
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CREDENTIAL_UNAVAILABLE"):
        await load_runtime_credential(
            cast(AsyncSession, _CredentialSession(None)),
            run_id=RUN_ID,
            profile_id=PROFILE_ID,
            credential_version_id=UUID("01900000-0000-7000-8000-000000000050"),
        )


@pytest.mark.unit
def test_message_source_preserves_trust_boundary_and_rejects_tampered_content() -> None:
    content = "hello"
    row: dict[str, object] = {
        "text_content": content,
        "caption": None,
        "content_sha256": hashlib.sha256(content.encode()).digest(),
        "direction": "incoming",
        "source": "telegram",
        "revision_id": UUID("01900000-0000-7000-8000-000000000060"),
        "revision_no": 1,
        "message_id": UUID("01900000-0000-7000-8000-000000000061"),
        "telegram_created_at": NOW,
    }
    source = model_runtime._message_source(cast(Any, row), layer=ContextLayer.CURRENT)
    assert source.canonical_role == "user"
    assert source.trust_level is TrustLevel.UNTRUSTED_USER

    row["direction"] = "outgoing"
    row["source"] = "ai"
    outgoing = model_runtime._message_source(cast(Any, row), layer=ContextLayer.CURRENT)
    assert outgoing.trust_level is TrustLevel.MODEL_GENERATED_HISTORY

    row["content_sha256"] = b"x" * 32
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SOURCE_INVALID"):
        model_runtime._message_source(cast(Any, row), layer=ContextLayer.CURRENT)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("memory_type", "expected"),
    [
        ("identity", ContextLayer.IDENTITY),
        ("style", ContextLayer.PERSONALITY),
        ("relationship", ContextLayer.RELATIONSHIP_TIME),
        ("fact", ContextLayer.STRUCTURED_MEMORY),
    ],
)
def test_memory_type_maps_to_the_only_allowed_context_layer(
    memory_type: str, expected: ContextLayer
) -> None:
    assert memory_context_layer(memory_type) is expected


class _MappedRows:
    def __init__(self, row: Mapping[str, object] | None = None) -> None:
        self._row = row

    def mappings(self) -> _MappedRows:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._row

    def __iter__(self) -> object:
        return iter(())


class _RuntimeSession:
    def __init__(self, row: Mapping[str, object] | None = None, scalar: object = None) -> None:
        self.row = row
        self.scalar_value = scalar

    async def execute(self, _: object) -> _MappedRows:
        return _MappedRows(self.row)

    async def scalar(self, _: object) -> object:
        return self.scalar_value


def _manifest_item(
    source_type: str,
    source_id: UUID = IMAGE_ID,
    *,
    content: str = "hello",
    image_detail: str | None = None,
) -> ManifestItem:
    return ManifestItem(
        ordinal=1,
        layer="current",
        canonical_role="user",
        source_actor="contact",
        source_type=source_type,
        source_id=str(source_id),
        source_revision=(
            "sha256-" + (b"i" * 32).hex() if source_type == "media_object" else "version-1"
        ),
        trust_level="untrusted_user",
        token_estimate=20,
        estimated_image_tokens=256 if image_detail else 0,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        rendered_part_sha256=hashlib.sha256(content.encode()).hexdigest(),
        reasons=(),
        rank_position=None,
        base_score=None,
        final_score=None,
        image_detail=image_detail,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_source_dispatches_each_typed_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = model_runtime.ModelRuntimeRepository(
        cast(AsyncSession, _RuntimeSession()), new_uuid=lambda: RUN_ID
    )
    run = cast(
        Any,
        {"account_id": PROFILE_ID, "conversation_id": ENDPOINT_ID, "turn_id": RUN_ID},
    )
    item_row = dict.fromkeys(
        (
            "prompt_version_id",
            "message_revision_id",
            "media_object_id",
            "memory_version_id",
            "summary_version_id",
        ),
        IMAGE_ID,
    )

    async def text_loader(**_: object) -> str:
        return "loaded"

    async def image_loader(**_: object) -> tuple[str, RuntimeImageSnapshot]:
        return "loaded-image", _image()

    monkeypatch.setattr(repository, "_load_exact_prompt", text_loader)
    monkeypatch.setattr(repository, "_load_exact_message", text_loader)
    monkeypatch.setattr(repository, "_load_exact_memory", text_loader)
    monkeypatch.setattr(repository, "_load_exact_summary", text_loader)
    monkeypatch.setattr(repository, "_load_exact_image", image_loader)

    for source_type in (
        "trusted_instruction",
        "message_revision",
        "memory_version",
        "summary_version",
    ):
        item = _manifest_item(source_type)
        content, image = await repository._load_exact_source(
            run=run, item=item, item_row=cast(Any, item_row), now=NOW
        )
        assert content == "loaded"
        assert image is None
    item = _manifest_item("media_object", image_detail="auto")
    content, image = await repository._load_exact_source(
        run=run, item=item, item_row=cast(Any, item_row), now=NOW
    )
    assert content == "loaded-image"
    assert image == _image()

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SNAPSHOT_INVALID"):
        await repository._load_exact_source(
            run=run, item=_manifest_item("unknown"), item_row=cast(Any, item_row), now=NOW
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_source_rejects_untyped_or_mismatched_manifest_reference() -> None:
    repository = model_runtime.ModelRuntimeRepository(
        cast(AsyncSession, _RuntimeSession()), new_uuid=lambda: RUN_ID
    )
    item = _manifest_item("message_revision")
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SNAPSHOT_INVALID"):
        await repository._load_exact_source(
            run=cast(Any, {}),
            item=item,
            item_row=cast(Any, {"message_revision_id": UUID(int=999)}),
            now=NOW,
        )

    malformed_item = replace(item, source_id="not-a-uuid")
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SNAPSHOT_INVALID"):
        await repository._load_exact_source(
            run=cast(Any, {}),
            item=malformed_item,
            item_row=cast(Any, {"message_revision_id": IMAGE_ID}),
            now=NOW,
        )


@pytest.mark.unit
def test_validate_exact_snapshot_rejects_each_tampered_binding() -> None:
    config = canonical_config_from_row(_config_row())
    capabilities = model_capabilities_from_row(config, _capability_row())
    capability_row = {
        **_capability_row(),
        "capability_endpoint_id": ENDPOINT_ID,
        "capability_model_name": "model-a",
    }
    row: dict[str, object] = {
        **capability_row,
        "capability_status": "valid",
        "credential_destroyed_at": None,
        "credential_nonce": b"n" * 12,
        "credential_ciphertext": b"c",
        "credential_secret_fingerprint": b"s" * 32,
        "base_url": "https://example.test/v1",
        "endpoint_canonical_sha256": hashlib.sha256(b"https://example.test/v1").digest(),
        "config_sha256": model_config_digest(config),
        "run_capability_sha256": capability_digest_from_row(capability_row),
    }
    model_runtime.ModelRuntimeRepository._validate_exact_snapshot(
        cast(Any, row), config, capabilities, now=NOW
    )
    for key, value in (
        ("capability_status", "expired"),
        ("credential_destroyed_at", NOW),
        ("credential_nonce", None),
        ("endpoint_canonical_sha256", b"x" * 32),
        ("config_sha256", b"x" * 32),
        ("run_capability_sha256", b"x" * 32),
    ):
        changed = dict(row)
        changed[key] = value
        with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_RUN_SNAPSHOT_INVALID"):
            model_runtime.ModelRuntimeRepository._validate_exact_snapshot(
                cast(Any, changed), config, capabilities, now=NOW
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_and_role_guards_fail_closed_before_provider_work() -> None:
    repository = model_runtime.ModelRuntimeRepository(
        cast(AsyncSession, _RuntimeSession()), new_uuid=lambda: RUN_ID
    )
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_BACKGROUND_RUNTIME_UNAVAILABLE"):
        await repository.prepare_generation(
            run_id=RUN_ID,
            expected_logical_role=LogicalRole.MEMORY_AGENT,
            input_hmac_key=SensitiveValue(b"k" * 32),
            now=NOW,
        )
    repository._is_background_run = cast(Any, lambda _: _async_true())  # type: ignore[method-assign]
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_BACKGROUND_RUNTIME_UNAVAILABLE"):
        await repository.load_prepared_generation(
            run_id=RUN_ID,
            input_hmac_key=SensitiveValue(b"k" * 32),
            now=NOW,
        )


async def _async_true() -> bool:
    return True
