"""Fake-first coverage for exact model-runtime snapshot boundaries."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import telegram_userbot.adapters.persistence.model_runtime as runtime_module
from telegram_userbot.adapters.persistence.model_runtime import (
    ModelRuntimeRepository,
    ModelRuntimeSnapshotError,
    RuntimeImageSnapshot,
    canonical_config_from_row,
    capability_digest_from_row,
    model_config_digest,
)
from telegram_userbot.domain.context import (
    Candidate,
    ContextLayer,
    ContextPolicy,
    ContextSource,
    ManifestItem,
    TrustLevel,
)
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000101")
CONVERSATION_ID = UUID("01900000-0000-7000-8000-000000000102")
TURN_ID = UUID("01900000-0000-7000-8000-000000000103")
RUN_ID = UUID("01900000-0000-7000-8000-000000000104")
PROMPT_ID = UUID("01900000-0000-7000-8000-000000000105")
MESSAGE_ID = UUID("01900000-0000-7000-8000-000000000106")
REVISION_ID = UUID("01900000-0000-7000-8000-000000000107")
IMAGE_ID = UUID("01900000-0000-7000-8000-000000000108")
MEMORY_ID = UUID("01900000-0000-7000-8000-000000000109")
SUMMARY_ID = UUID("01900000-0000-7000-8000-00000000010a")
PROFILE_ID = UUID("01900000-0000-7000-8000-00000000010b")
ENDPOINT_ID = UUID("01900000-0000-7000-8000-00000000010c")
CREDENTIAL_ID = UUID("01900000-0000-7000-8000-00000000010d")
CREDENTIAL_VERSION_ID = UUID("01900000-0000-7000-8000-00000000010e")


class _Result:
    def __init__(
        self,
        rows: Sequence[object] = (),
        *,
        rowcount: int = 0,
    ) -> None:
        self.rows = tuple(rows)
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> object | None:
        if len(self.rows) > 1:
            raise AssertionError("synthetic result contains multiple rows")
        return self.rows[0] if self.rows else None

    def all(self) -> list[object]:
        return list(self.rows)

    def __iter__(self) -> Any:
        return iter(self.rows)


class _Session:
    def __init__(
        self,
        *,
        results: Sequence[_Result] = (),
        scalars: Sequence[object] = (),
    ) -> None:
        self.results = deque(results)
        self.scalars = deque(scalars)
        self.statements: list[object] = []

    async def execute(self, statement: object, *_args: object, **_kwargs: object) -> _Result:
        self.statements.append(statement)
        return self.results.popleft() if self.results else _Result()

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self.scalars.popleft() if self.scalars else None


def _repo(session: _Session | None = None) -> ModelRuntimeRepository:
    return ModelRuntimeRepository(
        cast(AsyncSession, session or _Session()),
        new_uuid=lambda: UUID("01900000-0000-7000-8000-0000000001ff"),
    )


def _config_row() -> dict[str, object]:
    return {
        "model_profile_id": PROFILE_ID,
        "logical_role": "main_ai",
        "endpoint_id": ENDPOINT_ID,
        "credential_id": CREDENTIAL_ID,
        "protocol": "openai_responses",
        "model_name": "synthetic-model",
        "temperature": 0.25,
        "max_output_tokens": 512,
        "timeout_seconds": 30,
        "enabled": True,
        "protocol_options": {"reasoning_effort": "low"},
    }


def _capability_row() -> dict[str, object]:
    return {
        "capability_endpoint_id": ENDPOINT_ID,
        "capability_protocol": "openai_responses",
        "capability_model_name": "synthetic-model",
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
        "expires_at": NOW + timedelta(hours=1),
    }


def _base_run_row() -> dict[str, object]:
    config = canonical_config_from_row(_config_row())
    capability = _capability_row()
    claim = b"q" * 32
    endpoint = "https://example.test/v1"
    return {
        **_config_row(),
        **capability,
        "id": RUN_ID,
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "turn_id": TURN_ID,
        "purpose": "conversation_reply",
        "logical_role": "main_ai",
        "config_version_id": UUID("01900000-0000-7000-8000-000000000110"),
        "credential_version_id": CREDENTIAL_VERSION_ID,
        "account_control_version_snapshot": 1,
        "mode_version_snapshot": 1,
        "content_revision_snapshot": 1,
        "orchestration_claim_fingerprint": claim,
        "input_fingerprint": claim,
        "context_manifest_id": None,
        "state": "running",
        "cancel_requested_at": None,
        "run_capability_sha256": capability_digest_from_row(capability),
        "config_sha256": model_config_digest(config),
        "capability_status": "valid",
        "credential_destroyed_at": None,
        "credential_nonce": b"n" * 12,
        "credential_ciphertext": b"c" * 16,
        "credential_secret_fingerprint": b"s" * 32,
        "base_url": endpoint,
        "endpoint_canonical_sha256": hashlib.sha256(endpoint.encode()).digest(),
        "network_policy_id": UUID("01900000-0000-0000-0000-000000000111"),
        "network_policy_version": 1,
        "network_category": "provider",
        "turn_state": "generating",
        "output_schema_version": 1,
        "prompt_version": "main-ai-v1",
        "prompt_bundle_sha256": b"p" * 32,
        "credential_version_id_from_accessor": CREDENTIAL_VERSION_ID,
        "credential_id_from_accessor": CREDENTIAL_ID,
        "credential_profile_id": PROFILE_ID,
        "credential_version_no": 1,
        "credential_algorithm": "aes_256_gcm",
        "credential_key_version": 1,
        "credential_aad_schema_version": 1,
    }


def _item(  # noqa: PLR0913 - fixture fields mirror the persisted manifest
    source_type: str,
    source_id: UUID,
    content: str,
    *,
    layer: str = "current",
    revision: str | None = None,
    image_detail: str | None = None,
    image_tokens: int = 0,
    reasons: tuple[str, ...] = (),
) -> ManifestItem:
    if revision is None:
        revision = {
            "trusted_instruction": "version-1",
            "message_revision": "revision-1",
            "media_object": "sha256-" + (b"i" * 32).hex(),
            "memory_version": "version-1",
            "summary_version": "version-1",
        }[source_type]
    return ManifestItem(
        ordinal=1,
        layer=layer,
        canonical_role="system" if source_type == "trusted_instruction" else "user",
        source_actor="packaged_prompt" if source_type == "trusted_instruction" else "contact",
        source_type=source_type,
        source_id=str(source_id),
        source_revision=revision,
        trust_level="system" if source_type == "trusted_instruction" else "untrusted_user",
        token_estimate=20,
        estimated_image_tokens=image_tokens,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        rendered_part_sha256=hashlib.sha256(content.encode()).hexdigest(),
        reasons=reasons,
        rank_position=None,
        base_score=None,
        final_score=None,
        image_detail=image_detail,
    )


def _source(item: ManifestItem, content: str) -> ContextSource:
    return ContextSource(
        candidate=Candidate(
            UUID(item.source_id),
            item.source_revision,
            f"source:{item.source_id}",
            ContextLayer(item.layer),
            NOW,
            item.token_estimate,
        ),
        canonical_role=item.canonical_role,
        source_actor=item.source_actor,
        trust_level=TrustLevel(item.trust_level),
        content=SensitiveValue(content),
        source_type=item.source_type,
        image_detail=item.image_detail,
        image_tokens=item.estimated_image_tokens,
    )


def _image_snapshot() -> RuntimeImageSnapshot:
    return RuntimeImageSnapshot(IMAGE_ID, "media/image.png", b"i" * 32, "image/png", 100)


def _policy() -> ContextPolicy:
    return ContextPolicy(version="context-v1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_owner_and_exact_source_loaders_cover_happy_and_missing_paths() -> None:
    prompt = "synthetic prompt"
    prompt_item = _item("trusted_instruction", PROMPT_ID, prompt, layer="instruction")
    run = {
        "logical_role": "main_ai",
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "prompt_bundle_sha256": hashlib.sha256(prompt.encode()).digest(),
    }
    prompt_session = _Session(
        results=(
            _Result(
                (
                    {
                        "template_body": prompt,
                        "version_no": 1,
                        "template_sha256": hashlib.sha256(prompt.encode()).digest(),
                    },
                )
            ),
        )
    )
    assert (
        await _repo(prompt_session)._load_exact_prompt(
            run=cast(Any, run), item=prompt_item, source_id=PROMPT_ID
        )
        == prompt
    )

    message = "synthetic message"
    message_item = _item("message_revision", REVISION_ID, message)
    message_session = _Session(
        results=(
            _Result(
                (
                    {
                        "revision_no": 1,
                        "text_content": message,
                        "caption": None,
                        "content_sha256": hashlib.sha256(message.encode()).digest(),
                        "redacted_at": None,
                        "current_revision_no": 1,
                        "deleted_at": None,
                        "is_tombstone": False,
                    },
                )
            ),
        )
    )
    assert (
        await _repo(message_session)._load_exact_message(
            run=cast(Any, run), item=message_item, source_id=REVISION_ID
        )
        == message
    )

    memory = "synthetic memory"
    memory_item = _item("memory_version", MEMORY_ID, memory)
    memory_session = _Session(
        results=(
            _Result(
                (
                    {
                        "version_no": 1,
                        "rendered_text": memory,
                        "redacted_at": None,
                        "current_version_no": 1,
                        "status": "active",
                    },
                )
            ),
        )
    )
    assert (
        await _repo(memory_session)._load_exact_memory(
            run=cast(Any, run), item=memory_item, source_id=MEMORY_ID
        )
        == memory
    )

    summary = "synthetic summary"
    summary_item = _item("summary_version", SUMMARY_ID, summary)
    summary_session = _Session(
        results=(
            _Result(
                (
                    {
                        "version_no": 1,
                        "content_text": summary,
                        "content_sha256": hashlib.sha256(summary.encode()).digest(),
                        "invalidation_state": "active",
                        "redacted_at": None,
                        "current_version_no": 1,
                        "status": "active",
                    },
                )
            ),
        )
    )
    run_with_conversation = {**run, "conversation_id": CONVERSATION_ID}
    assert (
        await _repo(summary_session)._load_exact_summary(
            run=cast(Any, run_with_conversation), item=summary_item, source_id=SUMMARY_ID
        )
        == summary
    )

    for method, item, source_id in (
        ("_load_exact_prompt", prompt_item, PROMPT_ID),
        ("_load_exact_message", message_item, REVISION_ID),
        ("_load_exact_memory", memory_item, MEMORY_ID),
        ("_load_exact_summary", summary_item, SUMMARY_ID),
    ):
        with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SOURCE_INVALID"):
            await getattr(_repo(_Session(results=(_Result(),))), method)(
                run=cast(Any, run_with_conversation), item=item, source_id=source_id
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_exact_image_and_manifest_source_replay_paths() -> None:
    digest = b"i" * 32
    metadata = (
        f"[IMAGE media_object_id={IMAGE_ID} sha256={digest.hex()} "
        "mime=image/png width=640 height=480 detail=auto]"
    )
    item = _item(
        "media_object",
        IMAGE_ID,
        metadata,
        image_detail="auto",
        image_tokens=256,
    )
    image_row = {
        "id": IMAGE_ID,
        "parent_object_id": None,
        "object_kind": "provider_copy",
        "status": "ready",
        "sha256": digest,
        "storage_key": "media/image.png",
        "validated_mime": "image/png",
        "byte_size": 100,
        "expires_at": NOW + timedelta(hours=1),
        "width": 640,
        "height": 480,
    }
    run = {"account_id": ACCOUNT_ID, "conversation_id": CONVERSATION_ID, "turn_id": TURN_ID}
    image_repo = _repo(_Session(results=(_Result((image_row,)),), scalars=(True,)))
    content, snapshot = await image_repo._load_exact_image(
        run=cast(Any, run), item=item, source_id=IMAGE_ID, now=NOW
    )
    assert content == metadata
    assert snapshot == RuntimeImageSnapshot(IMAGE_ID, "media/image.png", digest, "image/png", 100)

    manifest_repo = _repo(_Session(results=(_Result((image_row,)),), scalars=(True,)))
    source, image = await manifest_repo._load_exact_source(
        run=cast(Any, run),
        item=item,
        item_row=cast(Any, {"media_object_id": IMAGE_ID}),
        now=NOW,
    )
    assert source == metadata
    assert image == snapshot

    source_item = _item(
        "message_revision",
        REVISION_ID,
        "hello",
        reasons=("eligible", "budget_borrowed_from:recent"),
    )
    source_repo = _repo(_Session())
    object.__setattr__(source_repo, "_load_exact_source", AsyncMock(return_value=("hello", None)))
    loaded, images = await source_repo._load_exact_manifest_sources(
        row=cast(Any, run),
        manifest=cast(Any, SimpleNamespace(items=(source_item,))),
        item_rows=(cast(Any, {"message_revision_id": REVISION_ID}),),
        now=NOW,
    )
    assert len(loaded) == 1
    assert images == ()
    assert loaded[0].selection_reasons == ("eligible",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_entrypoints_dispatch_create_and_replay_branches() -> None:
    repository = _repo()
    row = _base_run_row()
    row["context_manifest_id"] = None
    prepared = object()
    repository._is_background_run = cast(Any, AsyncMock(return_value=False))  # type: ignore[method-assign]
    repository._load_exact_run = cast(Any, AsyncMock(side_effect=[row, row, row]))  # type: ignore[method-assign]
    repository._prepare_generation_from_row = cast(Any, AsyncMock(return_value=prepared))  # type: ignore[method-assign]
    repository._load_prepared_generation_from_row = cast(Any, AsyncMock(return_value=prepared))  # type: ignore[method-assign]

    assert (
        await repository.prepare_or_load_generation(
            run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )
        is prepared
    )
    assert (
        await repository.prepare_generation(
            run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )
        is prepared
    )
    row["context_manifest_id"] = IMAGE_ID
    repository._load_orchestration_claim_fingerprint = cast(Any, AsyncMock(return_value=b"q" * 32))  # type: ignore[method-assign]
    assert (
        await repository.prepare_or_load_generation(
            run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )
        is prepared
    )
    assert cast(Any, repository._prepare_generation_from_row).await_count == 2
    assert cast(Any, repository._load_prepared_generation_from_row).await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_prepared_entrypoint_and_background_owner_detection() -> None:
    owner_session = _Session(
        results=(
            _Result((SimpleNamespace(memory_job_id=None, proactive_job_id=None),)),
            _Result((SimpleNamespace(memory_job_id=IMAGE_ID, proactive_job_id=None),)),
        )
    )
    repository = _repo(owner_session)
    assert await repository._is_background_run(RUN_ID) is False
    assert await repository._is_background_run(RUN_ID) is True

    repository = _repo()
    prepared = object()
    repository._is_background_run = cast(Any, AsyncMock(return_value=False))  # type: ignore[method-assign]
    repository._load_exact_run = cast(Any, AsyncMock(return_value=_base_run_row()))  # type: ignore[method-assign]
    repository._load_prepared_generation_from_row = cast(Any, AsyncMock(return_value=prepared))  # type: ignore[method-assign]
    assert (
        await repository.load_prepared_generation(
            run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )
        is prepared
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_and_prompt_loaders_cover_valid_and_unavailable_rows() -> None:
    policy_row = {
        "id": IMAGE_ID,
        "version_no": 3,
        "max_input_tokens": 24_000,
        "safety_reserve_basis_points": 500,
        "minimum_safety_reserve_tokens": 1_024,
        "current_budget_basis_points": 2_000,
        "recent_budget_basis_points": 3_000,
        "profile_budget_basis_points": 1_500,
        "structured_budget_basis_points": 1_500,
        "semantic_budget_basis_points": 1_000,
        "summary_budget_basis_points": 1_000,
        "structured_limit": 12,
        "semantic_limit": 8,
        "ann_candidate_limit": 64,
        "current_image_limit": 10,
        "fallback_auto_image_tokens": 2_048,
    }
    retrieval_row = {"id": SUMMARY_ID, "version_no": 4}
    prompt_text = "loaded prompt"
    prompt_hash = hashlib.sha256(prompt_text.encode()).digest()
    session = _Session(
        results=(
            _Result((policy_row,)),
            _Result((retrieval_row,)),
            _Result(
                (
                    {
                        "id": PROMPT_ID,
                        "version_no": 2,
                        "template_body": prompt_text,
                        "template_sha256": prompt_hash,
                    },
                )
            ),
        )
    )
    repository = _repo(session)
    policy_id, policy = await repository._load_context_policy(
        cast(Any, {"logical_role": "main_ai", "purpose": "conversation_reply"})
    )
    assert policy_id == IMAGE_ID
    assert policy.version == "context-v3"
    assert await repository._load_retrieval_policy() == (SUMMARY_ID, "retrieval-v4")
    prompt = await repository._load_prompt(
        cast(
            Any,
            {
                "logical_role": "main_ai",
                "prompt_bundle_sha256": prompt_hash,
                "prompt_version": "main-ai-v2",
            },
        )
    )
    assert prompt["id"] == PROMPT_ID

    for method, row in (
        ("_load_context_policy", {"logical_role": "main_ai", "purpose": "x"}),
        ("_load_retrieval_policy", None),
    ):
        with pytest.raises(ModelRuntimeSnapshotError):
            await getattr(_repo(_Session(results=(_Result(),))), method)(
                cast(Any, row) if method == "_load_context_policy" else None
            ) if method == "_load_context_policy" else await getattr(
                _repo(_Session(results=(_Result(),))), method
            )()
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_PROMPT_SNAPSHOT_INVALID"):
        await _repo(_Session(results=(_Result(),)))._load_prompt(
            cast(
                Any,
                {
                    "logical_role": "main_ai",
                    "prompt_bundle_sha256": prompt_hash,
                    "prompt_version": "main-ai-v2",
                },
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestration_claim_loader_and_exact_run_guards() -> None:
    message_id = UUID("01900000-0000-7000-8000-000000000120")
    session = _Session(
        results=(_Result((SimpleNamespace(message_id=message_id, message_revision_no=2),)),)
    )
    repository = _repo(session)
    claim = await repository._load_orchestration_claim_fingerprint(
        cast(Any, {"turn_id": TURN_ID, "account_id": ACCOUNT_ID})
    )
    assert len(claim) == 32

    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_GENERATION_ROLE_INVALID"):
        await repository._load_exact_run(
            run_id=RUN_ID,
            expected_logical_role=cast(Any, "embedding"),
            now=NOW,
            prepared=None,
        )

    base = _base_run_row()
    base["capability_expires_at"] = NOW + timedelta(hours=1)
    credential = {
        "credential_version_id_from_accessor": CREDENTIAL_VERSION_ID,
        "credential_profile_id": PROFILE_ID,
        "credential_id_from_accessor": CREDENTIAL_ID,
        "credential_version_no": 1,
        "credential_algorithm": "aes_256_gcm",
        "credential_key_version": 1,
        "credential_aad_schema_version": 1,
        "credential_nonce": b"n" * 12,
        "credential_ciphertext": b"c" * 16,
        "credential_secret_fingerprint": b"s" * 32,
    }
    original_loader = runtime_module.load_runtime_credential
    runtime_module.load_runtime_credential = AsyncMock(return_value=credential)
    try:
        repository = _repo(_Session(results=(_Result((base,)),)))
        loaded = await repository._load_exact_run(
            run_id=RUN_ID,
            expected_logical_role=LogicalRole.MAIN_AI,
            now=NOW,
            prepared=None,
        )
        assert loaded["credential_id_from_accessor"] == CREDENTIAL_ID
        with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_RUN_SNAPSHOT_UNAVAILABLE"):
            await _repo(_Session(results=(_Result(),)))._load_exact_run(
                run_id=RUN_ID, expected_logical_role=LogicalRole.MAIN_AI, now=NOW, prepared=True
            )
    finally:
        runtime_module.load_runtime_credential = original_loader


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_and_replay_helpers_cover_success_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _base_run_row()
    row["orchestration_claim_fingerprint"] = b"q" * 32
    row["input_fingerprint"] = b"q" * 32
    source = _source(_item("message_revision", REVISION_ID, "hello"), "hello")
    built_manifest = SimpleNamespace(
        id=IMAGE_ID,
        manifest_sha256=(b"m" * 32).hex(),
    )
    built = SimpleNamespace(manifest=built_manifest, ordered_sources=(source,))
    repository = _repo(_Session(results=(_Result(rowcount=1),)))
    monkeypatch.setattr(repository, "_validate_exact_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(
        repository, "_load_orchestration_claim_fingerprint", AsyncMock(return_value=b"q" * 32)
    )
    monkeypatch.setattr(
        repository, "_load_context_policy", AsyncMock(return_value=(IMAGE_ID, _policy()))
    )
    monkeypatch.setattr(
        repository, "_load_retrieval_policy", AsyncMock(return_value=(SUMMARY_ID, "retrieval-v1"))
    )
    monkeypatch.setattr(
        repository,
        "_load_prompt",
        AsyncMock(return_value={"template_body": "prompt", "id": PROMPT_ID, "version_no": 1}),
    )
    monkeypatch.setattr(repository, "_load_sources", AsyncMock(return_value=((source,), ())))
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.build_context", lambda **_: built
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.canonical_input_fingerprint",
        lambda **_: b"f" * 32,
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.ContextRepository.save_manifest",
        AsyncMock(),
    )
    prepared = await repository._prepare_generation_from_row(
        row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
    )
    assert prepared.context_manifest_id == UUID("01900000-0000-7000-8000-0000000001ff")

    row["context_manifest_id"] = IMAGE_ID
    manifest = SimpleNamespace(id=IMAGE_ID, items=(source.candidate,))
    monkeypatch.setattr(
        repository, "_load_bound_manifest", AsyncMock(return_value=(manifest, ({},)))
    )
    monkeypatch.setattr(
        repository, "_load_exact_manifest_sources", AsyncMock(return_value=((source,), ()))
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.rebuild_context", lambda *_: built
    )
    row["input_fingerprint"] = b"f" * 32
    prepared_replay = await repository._load_prepared_generation_from_row(
        row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
    )
    assert prepared_replay.canonical_input_fingerprint == b"f" * 32

    row["input_fingerprint"] = b"x" * 32
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_INPUT_FINGERPRINT_MISMATCH"):
        await repository._load_prepared_generation_from_row(
            row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_snapshot_fail_closed_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _base_run_row()
    source = _source(_item("message_revision", REVISION_ID, "hello"), "hello")
    repository = _repo(_Session(results=(_Result(rowcount=0),)))
    repository._validate_exact_snapshot = cast(Any, lambda *a, **k: None)  # type: ignore[method-assign]
    repository._load_orchestration_claim_fingerprint = cast(Any, AsyncMock(return_value=b"x" * 32))  # type: ignore[method-assign]
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_TURN_MEMBERSHIP_INVALID"):
        await repository._prepare_generation_from_row(
            row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )

    row["orchestration_claim_fingerprint"] = b"x" * 32
    row["input_fingerprint"] = b"x" * 32
    monkeypatch.setattr(
        repository, "_load_context_policy", AsyncMock(return_value=(IMAGE_ID, _policy()))
    )
    monkeypatch.setattr(
        repository, "_load_retrieval_policy", AsyncMock(return_value=(SUMMARY_ID, "retrieval-v1"))
    )
    monkeypatch.setattr(
        repository,
        "_load_prompt",
        AsyncMock(return_value={"template_body": "prompt", "id": PROMPT_ID, "version_no": 1}),
    )
    monkeypatch.setattr(repository, "_load_sources", AsyncMock(return_value=((source,), ())))
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.ContextRepository.save_manifest",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.build_context",
        lambda **_: SimpleNamespace(
            manifest=SimpleNamespace(manifest_sha256=(b"m" * 32).hex()), ordered_sources=(source,)
        ),
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.canonical_input_fingerprint",
        lambda **_: b"f" * 32,
    )
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_RUN_CONTEXT_BIND_CONFLICT"):
        await repository._prepare_generation_from_row(
            row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )

    row["context_manifest_id"] = IMAGE_ID
    monkeypatch.setattr(
        repository, "_load_orchestration_claim_fingerprint", AsyncMock(return_value=b"q" * 32)
    )
    monkeypatch.setattr(
        repository, "_load_bound_manifest", AsyncMock(return_value=(SimpleNamespace(items=()), ()))
    )
    monkeypatch.setattr(
        repository, "_load_exact_manifest_sources", AsyncMock(return_value=((source,), ()))
    )
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_TURN_MEMBERSHIP_INVALID"):
        await repository._load_prepared_generation_from_row(
            row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_rebuild_and_exact_source_integrity_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _base_run_row()
    row["context_manifest_id"] = IMAGE_ID
    row["orchestration_claim_fingerprint"] = b"q" * 32
    source = _source(_item("message_revision", REVISION_ID, "hello"), "hello")
    repository = _repo()
    monkeypatch.setattr(repository, "_validate_exact_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(
        repository, "_load_orchestration_claim_fingerprint", AsyncMock(return_value=b"q" * 32)
    )
    monkeypatch.setattr(
        repository,
        "_load_bound_manifest",
        AsyncMock(return_value=(SimpleNamespace(items=(source,)), ({},))),
    )
    monkeypatch.setattr(
        repository, "_load_exact_manifest_sources", AsyncMock(return_value=((source,), ()))
    )
    monkeypatch.setattr(
        "telegram_userbot.adapters.persistence.model_runtime.rebuild_context",
        lambda *_: (_ for _ in ()).throw(ValueError("tampered")),
    )
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SNAPSHOT_INVALID"):
        await repository._load_prepared_generation_from_row(
            row=cast(Any, row), run_id=RUN_ID, input_hmac_key=SensitiveValue(b"k" * 32), now=NOW
        )

    # Each exact loader must reject a changed immutable source revision/content.
    prompt = _item("trusted_instruction", PROMPT_ID, "prompt", layer="instruction")
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_PROMPT_SNAPSHOT_INVALID"):
        await _repo(
            _Session(
                results=(
                    _Result(
                        (
                            {
                                "template_body": "prompt",
                                "version_no": 1,
                                "template_sha256": hashlib.sha256(b"other").digest(),
                            },
                        )
                    ),
                )
            )
        )._load_prompt(
            cast(
                Any,
                {
                    "logical_role": "main_ai",
                    "prompt_bundle_sha256": hashlib.sha256(b"prompt").digest(),
                    "prompt_version": "main-ai-v1",
                },
            )
        )
    message = _item("message_revision", REVISION_ID, "hello")
    bad_message = {
        "revision_no": 1,
        "text_content": "hello",
        "caption": None,
        "content_sha256": hashlib.sha256(b"hello").digest(),
        "redacted_at": NOW,
        "current_revision_no": 1,
        "deleted_at": None,
        "is_tombstone": False,
    }
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SOURCE_INVALID"):
        await _repo(_Session(results=(_Result((bad_message,)),)))._load_exact_message(
            run=cast(Any, {"account_id": ACCOUNT_ID, "conversation_id": CONVERSATION_ID}),
            item=message,
            source_id=REVISION_ID,
        )
    image = _item("media_object", IMAGE_ID, "bad", image_detail="auto", image_tokens=256)
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_IMAGE_SNAPSHOT_INVALID"):
        await _repo(_Session())._load_exact_image(
            run=cast(Any, {"account_id": ACCOUNT_ID, "conversation_id": CONVERSATION_ID}),
            item=image,
            source_id=IMAGE_ID,
            now=NOW,
        )
    with pytest.raises(ModelRuntimeSnapshotError, match="MODEL_CONTEXT_SOURCE_INVALID"):
        ModelRuntimeRepository._validate_source_content(prompt, "different")
