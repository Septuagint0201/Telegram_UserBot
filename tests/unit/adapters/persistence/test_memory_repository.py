"""Fake-first tests for memory persistence scope and replay guards."""

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.memory_repository import (
    MemoryJobLease,
    MemoryRepository,
)
from telegram_userbot.domain.memory.models import (
    EmbeddingRecord,
    EmbeddingRecordState,
    EmbeddingSpace,
    Evidence,
    InputManifest,
    InputSource,
    MemoryOperation,
    MemoryProposal,
    MemoryType,
    ProposalState,
    SummaryKind,
    SummarySource,
    SummaryVersion,
)
from telegram_userbot.domain.memory.summary import SummaryCoverageError
from telegram_userbot.domain.memory.validation import ValidatedProposal

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(
        self,
        row: dict[str, Any] | None = None,
        *,
        rowcount: int = 0,
        scalar: Any = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._row = row
        self._scalar = scalar
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row

    def one(self) -> dict[str, Any]:
        assert self._row is not None
        return self._row

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar


def _repository(session: AsyncMock | None = None) -> tuple[MemoryRepository, AsyncMock]:
    value = session or AsyncMock()
    return MemoryRepository(cast(AsyncSession, value)), value


def _source(source_type: str = "message_revision", *, source_id: UUID | None = None) -> InputSource:
    content = f"synthetic-{source_type}"
    return InputSource(
        source_id or uuid4(),
        "revision-1",
        content,
        sha256(content.encode()).digest(),
        source_type=source_type,
    )


def _manifest(
    account_id: UUID,
    conversation_id: UUID,
    sources: tuple[InputSource, ...],
) -> InputManifest:
    return InputManifest(
        id=uuid4(),
        account_id=account_id,
        conversation_id=conversation_id,
        generation=1,
        range_start_event_id=1,
        range_end_event_id=1,
        sources=sources,
        pipeline_version="m6-v1",
        policy_version="policy-v1",
        prompt_version="prompt-v1",
    )


def _proposal(
    account_id: UUID,
    conversation_id: UUID,
    source: InputSource,
) -> ValidatedProposal:
    evidence = Evidence(source.source_id, source.revision, source.content_sha256)
    return ValidatedProposal(
        MemoryProposal(
            id=uuid4(),
            account_id=account_id,
            conversation_id=conversation_id,
            operation=MemoryOperation.CREATE,
            memory_type=MemoryType.FACT,
            semantic_key="synthetic key",
            payload={"value": "synthetic"},
            confidence=0.95,
            importance=0.5,
            evidence=(evidence,),
            rendered_text="synthetic",
        ),
        ProposalState.ACCEPTED,
    )


@pytest.mark.asyncio
async def test_memory_scope_helpers_accept_each_supported_source_and_reject_mismatch() -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    session.scalar.return_value = uuid4()

    await repo._require_message_revision_scope(
        uuid4(), account_id=account_id, conversation_id=conversation_id
    )
    for kind in ("message_revision", "media_object", "memory_version", "summary_version"):
        await repo._require_manifest_source_scope(
            _source(kind), account_id=account_id, conversation_id=conversation_id
        )

    with pytest.raises(ValueError, match="unsupported manifest"):
        await repo._require_manifest_source_scope(
            SimpleNamespace(source_type="provider_raw", source_id=uuid4()),
            account_id=account_id,
            conversation_id=conversation_id,
        )
    session.scalar.return_value = None
    with pytest.raises(ValueError, match="message revision"):
        await repo._require_message_revision_scope(
            uuid4(), account_id=account_id, conversation_id=conversation_id
        )
    with pytest.raises(ValueError, match="manifest source"):
        await repo._require_manifest_source_scope(
            _source("summary_version"),
            account_id=account_id,
            conversation_id=conversation_id,
        )


@pytest.mark.asyncio
async def test_embedding_target_and_owner_helpers_cover_all_durable_target_kinds() -> None:
    repo, session = _repository()
    account_id = uuid4()
    space_id = uuid4()
    session.scalar.return_value = uuid4()
    records = tuple(
        EmbeddingRecord(
            uuid4(),
            space_id,
            uuid4(),
            kind,
            index,
            b"s" * 32,
            (0.1, 0.2),
        )
        for index, kind in enumerate(("memory_version", "summary_version", "message_revision"))
    )
    for record in records:
        await repo._require_embedding_target_scope(record, account_id=account_id)

    rows = (
        {
            "memory_version_id": uuid4(),
            "summary_version_id": None,
            "message_revision_id": None,
        },
        {
            "memory_version_id": None,
            "summary_version_id": uuid4(),
            "message_revision_id": None,
        },
        {
            "memory_version_id": None,
            "summary_version_id": None,
            "message_revision_id": uuid4(),
        },
    )
    for row in rows:
        assert await repo._embedding_record_belongs_to_account(row, account_id=account_id)
    session.scalar.return_value = None
    with pytest.raises(ValueError, match="embedding target"):
        await repo._require_embedding_target_scope(records[0], account_id=account_id)
    assert not await repo._embedding_record_belongs_to_account(rows[-1], account_id=account_id)


@pytest.mark.asyncio
async def test_erasure_scope_helper_rejects_unknown_missing_and_cross_account_targets() -> None:
    repo, session = _repository()
    account_id, conversation_id, target_id = uuid4(), uuid4(), uuid4()
    with pytest.raises(ValueError, match="unsupported"):
        await repo._require_erasure_derived_scope(
            {"memory": {target_id}}, account_id=account_id, conversation_id=conversation_id
        )

    session.execute.return_value = _Result()
    with pytest.raises(ValueError, match="embedding target"):
        await repo._require_erasure_derived_scope(
            {"embedding": {target_id}}, account_id=account_id, conversation_id=conversation_id
        )
    session.execute.return_value = _Result(
        {"account_id": uuid4(), "memory_version_id": uuid4(), "summary_version_id": None}
    )
    with pytest.raises(ValueError, match="embedding target"):
        await repo._require_erasure_derived_scope(
            {"embedding": {target_id}}, account_id=account_id, conversation_id=conversation_id
        )

    session.execute.return_value = _Result(
        {
            "account_id": account_id,
            "memory_version_id": uuid4(),
            "summary_version_id": None,
            "message_revision_id": None,
        }
    )
    session.scalar.return_value = uuid4()
    await repo._require_erasure_derived_scope(
        {"embedding": {target_id}}, account_id=account_id, conversation_id=conversation_id
    )
    await repo._require_erasure_derived_scope(
        {"summary": {target_id}}, account_id=account_id, conversation_id=None
    )
    session.scalar.return_value = None
    with pytest.raises(ValueError, match="summary target"):
        await repo._require_erasure_derived_scope(
            {"summary": {target_id}}, account_id=account_id, conversation_id=conversation_id
        )


@pytest.mark.asyncio
async def test_create_manifest_validates_scope_and_requires_fenced_seal() -> None:
    repo, session = _repository()
    account_id, conversation_id, owner = uuid4(), uuid4(), uuid4()
    sources = tuple(
        _source(kind)
        for kind in ("message_revision", "media_object", "memory_version", "summary_version")
    )
    manifest = _manifest(account_id, conversation_id, sources)
    lease = MemoryJobLease(uuid4(), account_id, conversation_id, "episode", 1, 1, 1, owner, 2)
    repo._require_manifest_source_scope = AsyncMock()  # type: ignore[method-assign]
    session.execute.side_effect = [
        _Result(),
        *(_Result() for _ in sources),
        _Result(rowcount=1),
    ]
    await repo.create_manifest(manifest, lease=lease, now=NOW)
    assert repo._require_manifest_source_scope.await_count == len(sources)

    with pytest.raises(ValueError, match="does not match"):
        await repo.create_manifest(
            manifest,
            lease=MemoryJobLease(
                lease.id,
                account_id,
                conversation_id,
                "episode",
                2,
                1,
                1,
                owner,
                2,
            ),
            now=NOW,
        )
    session.execute.side_effect = [
        _Result(),
        *(_Result() for _ in sources),
        _Result(rowcount=0),
    ]
    with pytest.raises(RuntimeError, match="stale"):
        await repo.create_manifest(manifest, lease=lease, now=NOW)


@pytest.mark.asyncio
async def test_record_proposal_binds_job_run_manifest_and_evidence_scope() -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    source = _source()
    manifest = _manifest(account_id, conversation_id, (source,))
    validated = _proposal(account_id, conversation_id, source)
    job_id, model_run_id = uuid4(), uuid4()
    repo._require_message_revision_scope = AsyncMock()  # type: ignore[method-assign]
    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.return_value = _Result(rowcount=1)
    assert await repo.record_proposal(
        validated,
        job_id=job_id,
        model_run_id=model_run_id,
        proposal_ordinal=0,
        manifest=manifest,
    )
    repo._require_message_revision_scope.assert_awaited_once()

    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.return_value = _Result(rowcount=0)
    assert not await repo.record_proposal(
        validated,
        job_id=job_id,
        model_run_id=model_run_id,
        proposal_ordinal=0,
        manifest=manifest,
    )
    session.scalar.side_effect = [None]
    with pytest.raises(ValueError, match="job and manifest"):
        await repo.record_proposal(
            validated,
            job_id=job_id,
            model_run_id=model_run_id,
            proposal_ordinal=0,
            manifest=manifest,
        )
    session.scalar.side_effect = [job_id, None]
    with pytest.raises(ValueError, match="model run"):
        await repo.record_proposal(
            validated,
            job_id=job_id,
            model_run_id=model_run_id,
            proposal_ordinal=0,
            manifest=manifest,
        )


def _summary(
    conversation_summary_id: UUID,
    *,
    version: int = 1,
    start: int = 1,
    end: int = 2,
) -> SummaryVersion:
    source = SummarySource(uuid4(), "message_revision", b"m" * 32, 1)
    return SummaryVersion(
        uuid4(),
        conversation_summary_id,
        version,
        SummaryKind.ROLLING,
        start,
        end,
        f"summary-{version}",
        (source,),
        bytes([version]) * 32,
    )


@pytest.mark.asyncio
async def test_publish_summary_covers_initial_replay_and_watermark_cas() -> None:
    account_id, conversation_id, summary_id = uuid4(), uuid4(), uuid4()
    first = _summary(summary_id)
    repo, session = _repository()
    repo._require_message_revision_scope = AsyncMock()  # type: ignore[method-assign]
    session.execute.side_effect = [
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
    ]
    watermark = await repo.publish_summary(
        first, account_id=account_id, conversation_id=conversation_id, now=NOW
    )
    assert watermark.last_included_event_id == 2
    assert watermark.version == 2

    durable_watermark = {
        "last_included_event_id": 2,
        "version": 2,
        "last_summary_version_id": first.id,
    }
    durable_summary = {
        "summary_kind": SummaryKind.ROLLING.value,
        "current_version_no": 1,
    }
    durable_summary_version = {
        "id": first.id,
        "summary_id": first.summary_id,
        "version_no": first.version_no,
        "range_start_event_id": first.range_start_event_id,
        "range_end_event_id": first.range_end_event_id,
        "content_text": first.content_text,
        "content_sha256": sha256(first.content_text.encode()).digest(),
        "model_run_id": None,
        "pipeline_version": "m6-v1",
        "output_schema_version": 1,
        "manifest_sha256": first.manifest_sha256,
        "invalidation_state": first.status.value,
    }
    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(durable_summary_version),
    ]
    replay = await repo.publish_summary(
        first, account_id=account_id, conversation_id=conversation_id, now=NOW
    )
    assert replay.last_summary_version_id == first.id

    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(durable_summary_version | {"content_text": "tampered"}),
    ]
    with pytest.raises(SummaryCoverageError, match="summary replay"):
        await repo.publish_summary(
            first, account_id=account_id, conversation_id=conversation_id, now=NOW
        )

    second = _summary(summary_id, version=2, start=2, end=3)
    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(rowcount=1),
    ]
    advanced = await repo.publish_summary(
        second,
        account_id=account_id,
        conversation_id=conversation_id,
        expected_version=1,
        now=NOW,
    )
    assert advanced.version == 3

    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(rowcount=0),
    ]
    with pytest.raises(SummaryCoverageError, match="watermark changed"):
        await repo.publish_summary(
            second,
            account_id=account_id,
            conversation_id=conversation_id,
            expected_version=1,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_write_embeddings_checks_space_identity_upserts_targets_and_activates() -> None:
    repo, session = _repository()
    account_id, profile_id, config_id = uuid4(), uuid4(), uuid4()
    space = EmbeddingSpace(uuid4(), "fake-embedding", 2, "cosine", "l2", "v1", 1)
    records = tuple(
        EmbeddingRecord(uuid4(), space.id, uuid4(), kind, 0, b"e" * 32, (0.1, 0.2))
        for kind in ("memory_version", "summary_version")
    )
    durable_space = {
        "account_id": account_id,
        "model_profile_id": profile_id,
        "config_version_id": config_id,
        "model_name_snapshot": space.model_name,
        "dimensions": space.dimensions,
        "distance_metric": space.distance_metric,
        "normalization": space.normalization,
        "chunker_version": space.chunker_version,
        "generation": space.generation,
        "state": "building",
    }
    repo._require_embedding_target_scope = AsyncMock()  # type: ignore[method-assign]
    session.execute.side_effect = [
        _Result(),
        _Result(durable_space),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
    ]
    session.scalar.return_value = 0
    assert (
        await repo.write_embedding_records(
            space,
            records,
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            now=NOW,
        )
        == 2
    )
    assert repo._require_embedding_target_scope.await_count == 2

    session.execute.side_effect = [_Result(), _Result(durable_space | {"generation": 2})]
    with pytest.raises(ValueError, match="durable identity"):
        await repo.write_embedding_records(
            space,
            (),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
        )
    wrong = EmbeddingRecord(uuid4(), uuid4(), uuid4(), "memory_version", 0, b"e" * 32, (0.1, 0.2))
    with pytest.raises(ValueError, match="dimension or space"):
        await repo.write_embedding_records(
            space,
            (wrong,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
        )


@pytest.mark.asyncio
async def test_write_embeddings_rejects_replay_mutation_and_non_ready_activation() -> None:
    account_id, profile_id, config_id = uuid4(), uuid4(), uuid4()
    space = EmbeddingSpace(uuid4(), "fake-embedding", 2, "cosine", "l2", "v1", 1)
    record = EmbeddingRecord(uuid4(), space.id, uuid4(), "memory_version", 0, b"e" * 32, (0.1, 0.2))
    durable_space = {
        "account_id": account_id,
        "model_profile_id": profile_id,
        "config_version_id": config_id,
        "model_name_snapshot": space.model_name,
        "dimensions": space.dimensions,
        "distance_metric": space.distance_metric,
        "normalization": space.normalization,
        "chunker_version": space.chunker_version,
        "generation": space.generation,
        "state": "building",
    }
    durable_record = {
        "id": record.id,
        "account_id": account_id,
        "source_sha256": b"x" * 32,
        "vector_payload": list(record.vector),
        "dimensions": space.dimensions,
        "chunker_version": space.chunker_version,
        "state": record.state.value,
    }
    repo, session = _repository()
    repo._require_embedding_target_scope = AsyncMock()  # type: ignore[method-assign]
    session.execute.side_effect = [
        _Result(),
        _Result(durable_space),
        _Result(durable_record),
    ]
    with pytest.raises(ValueError, match="record replay"):
        await repo.write_embedding_records(
            space,
            (record,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            now=NOW,
        )

    pending = EmbeddingRecord(
        uuid4(),
        space.id,
        uuid4(),
        "memory_version",
        0,
        b"p" * 32,
        (0.1, 0.2),
        EmbeddingRecordState.PENDING,
    )
    fresh_repo, fresh_session = _repository()
    with pytest.raises(ValueError, match="ready records"):
        await fresh_repo.write_embedding_records(
            space,
            (pending,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            now=NOW,
        )
    fresh_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_and_erasure_are_account_scoped_and_idempotently_shaped() -> None:
    repo, session = _repository()
    account_id, conversation_id, memory_id = uuid4(), uuid4(), uuid4()
    session.execute.side_effect = [_Result()]
    assert not await repo.forget_memory(account_id=account_id, memory_id=memory_id, now=NOW)

    session.execute.side_effect = [_Result({"id": memory_id}), _Result(), _Result(), _Result()]
    assert await repo.forget_memory(account_id=account_id, memory_id=memory_id, now=NOW)

    repo._require_erasure_derived_scope = AsyncMock()  # type: ignore[method-assign]
    repo.forget_memory = AsyncMock(return_value=True)  # type: ignore[method-assign]
    embedding_id, summary_id = uuid4(), uuid4()
    session.execute.side_effect = [
        _Result({"id": memory_id, "conversation_id": conversation_id}),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
    ]
    request_id = await repo.record_erasure(
        account_id=account_id,
        memory_id=memory_id,
        derived_ids={"embedding": {embedding_id}, "summary": {summary_id}},
        now=NOW,
    )
    assert request_id
    repo._require_erasure_derived_scope.assert_awaited_once()
    repo.forget_memory.assert_awaited_once()

    session.execute.side_effect = [_Result()]
    with pytest.raises(ValueError, match="outside"):
        await repo.record_erasure(account_id=account_id, memory_id=memory_id, now=NOW)
    with pytest.raises(ValueError, match="metadata"):
        await repo.record_erasure(
            account_id=account_id, memory_id=memory_id, policy_version=0, now=NOW
        )
    with pytest.raises(ValueError, match="source"):
        await repo.record_erasure(account_id=account_id, now=NOW)
