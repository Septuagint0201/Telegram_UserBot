"""Fake-first tests for memory persistence scope and replay guards."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.memory_repository import (
    MemoryJobLease,
    MemoryRepository,
    ReviewAction,
)
from telegram_userbot.domain.memory.lifecycle import AcceptanceResult, MemoryConflictError
from telegram_userbot.domain.memory.models import (
    EmbeddingRecord,
    EmbeddingRecordState,
    EmbeddingSpace,
    EmbeddingState,
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
from telegram_userbot.domain.shared.hashing import stable_json_bytes

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
ERASURE_SECRET = b"e" * 32


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


class _NestedTransaction:
    async def __aenter__(self) -> _NestedTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        return False


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
async def test_summary_sources_must_remain_current_and_hash_bound() -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    digest = b"s" * 32
    message_source = SummarySource(uuid4(), "message_revision", digest, 1)
    valid_message = {
        "revision_no": 2,
        "content_sha256": digest,
        "redacted_at": None,
        "current_revision_no": 2,
        "deleted_at": None,
        "is_tombstone": False,
    }
    session.execute.return_value = _Result(valid_message)
    await repo._require_summary_source_current(
        message_source,
        account_id=account_id,
        conversation_id=conversation_id,
    )
    assert "FOR UPDATE" in str(session.execute.await_args.args[0])
    message_changes: tuple[dict[str, object], ...] = (
        {"current_revision_no": 3},
        {"content_sha256": b"x" * 32},
        {"redacted_at": NOW},
        {"deleted_at": NOW, "is_tombstone": True},
    )
    for changes in message_changes:
        session.execute.return_value = _Result(valid_message | changes)
        with pytest.raises(ValueError, match="message source is stale"):
            await repo._require_summary_source_current(
                message_source,
                account_id=account_id,
                conversation_id=conversation_id,
            )

    prior_source = SummarySource(uuid4(), "prior_summary_version", digest, 1)
    valid_prior = {
        "version_no": 3,
        "content_sha256": digest,
        "invalidation_state": "active",
        "redacted_at": None,
        "current_version_no": 3,
        "status": "active",
    }
    session.execute.return_value = _Result(valid_prior)
    await repo._require_summary_source_current(
        prior_source,
        account_id=account_id,
        conversation_id=conversation_id,
    )
    assert "FOR UPDATE" in str(session.execute.await_args.args[0])
    prior_changes: tuple[dict[str, object], ...] = (
        {"current_version_no": 4},
        {"content_sha256": b"x" * 32},
        {"invalidation_state": "invalidated"},
        {"status": "quarantined"},
        {"redacted_at": NOW},
    )
    for changes in prior_changes:
        session.execute.return_value = _Result(valid_prior | changes)
        with pytest.raises(ValueError, match="prior summary source is stale"):
            await repo._require_summary_source_current(
                prior_source,
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
    recorded_id = await repo.record_proposal(
        validated,
        job_id=job_id,
        model_run_id=model_run_id,
        proposal_ordinal=0,
        manifest=manifest,
    )
    assert isinstance(recorded_id, UUID)
    repo._require_message_revision_scope.assert_awaited_once()

    durable_row = {
        "id": recorded_id,
        "account_id": account_id,
        "contact_id": None,
        "conversation_id": conversation_id,
        "memory_job_id": job_id,
        "model_run_id": model_run_id,
        "model_role": "memory_agent",
        "idempotency_key": sha256(
            stable_json_bytes({"model_run_id": str(model_run_id), "proposal_ordinal": 0})
        ).digest(),
        "proposal_ordinal": 0,
        "operation": validated.proposal.operation.value,
        "memory_type": validated.proposal.memory_type.value,
        "semantic_key_hash": validated.proposal.semantic_key_hash,
        "payload_schema_version": 1,
        "proposed_payload": dict(validated.proposal.payload),
        "proposed_text": validated.proposal.rendered_text,
        "proposed_confidence": Decimal("0.9500"),
        "proposed_importance": Decimal("0.5000"),
        "proposed_valid_from": validated.proposal.valid_from,
        "proposed_valid_to": validated.proposal.valid_to,
        "visual_only": validated.proposal.visual_only,
        "state": validated.state.value,
        "validation_code": None,
        "validator_policy_version": "m6-v1",
        "retention_class": "memory_proposal",
    }
    durable_evidence = {
        "message_revision_id": source.source_id,
        "evidence_role": validated.proposal.evidence[0].role.value,
        "quoted_span_start": validated.proposal.evidence[0].span_start,
        "quoted_span_end": validated.proposal.evidence[0].span_end,
        "source_content_sha256": source.content_sha256,
        "trust_class": validated.proposal.evidence[0].trust.value,
    }
    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.side_effect = [
        _Result(rowcount=0),
        _Result(durable_row),
        _Result(rows=[]),
        _Result(rows=[durable_evidence]),
    ]
    replayed_id = await repo.record_proposal(
        validated,
        job_id=job_id,
        model_run_id=model_run_id,
        proposal_ordinal=0,
        manifest=manifest,
    )
    assert replayed_id == recorded_id

    mutated = replace(
        validated,
        proposal=replace(validated.proposal, payload={"value": "mutated"}),
    )
    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.side_effect = [_Result(rowcount=0), _Result(durable_row)]
    with pytest.raises(ValueError, match="replay does not match durable identity"):
        await repo.record_proposal(
            mutated,
            job_id=job_id,
            model_run_id=model_run_id,
            proposal_ordinal=0,
            manifest=manifest,
        )

    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.side_effect = [
        _Result(rowcount=0),
        _Result(durable_row),
        _Result(rows=[]),
        _Result(rows=[]),
    ]
    with pytest.raises(ValueError, match="replay does not match durable evidence"):
        await repo.record_proposal(
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

    mismatched = replace(
        validated,
        proposal=replace(
            validated.proposal,
            evidence=(replace(validated.proposal.evidence[0], source_revision="revision-2"),),
        ),
    )
    session.scalar.side_effect = [job_id, model_run_id]
    session.execute.side_effect = None
    session.execute.return_value = _Result(rowcount=1)
    with pytest.raises(ValueError, match="canonical message root"):
        await repo.record_proposal(
            mismatched,
            job_id=job_id,
            model_run_id=model_run_id,
            proposal_ordinal=1,
            manifest=manifest,
        )


@pytest.mark.asyncio
async def test_proposal_lookup_normalizes_postgres_numeric_scores() -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    proposal = _proposal(account_id, conversation_id, _source()).proposal
    recorded_id = uuid4()
    row = {
        "id": recorded_id,
        "operation": proposal.operation.value,
        "memory_type": proposal.memory_type.value,
        "semantic_key_hash": proposal.semantic_key_hash,
        "proposed_payload": dict(proposal.payload),
        "proposed_text": proposal.rendered_text,
        "proposed_confidence": Decimal("0.9500"),
        "proposed_importance": Decimal("0.5000"),
        "proposed_valid_from": proposal.valid_from,
        "proposed_valid_to": proposal.valid_to,
        "visual_only": proposal.visual_only,
    }
    session.execute.return_value = _Result(row)
    assert await repo._proposal_row(recorded_id, proposal) == row

    session.execute.return_value = _Result({**row, "proposed_importance": Decimal("0.5001")})
    with pytest.raises(ValueError, match="does not match"):
        await repo._proposal_row(recorded_id, proposal)


@pytest.mark.asyncio
@pytest.mark.parametrize("contact_source", ["proposal", "conversation", "missing"])
async def test_accept_create_requires_and_resolves_contact_scope(contact_source: str) -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    source = _source()
    validated = _proposal(account_id, conversation_id, source)
    evidence = validated.proposal.evidence[0]
    recorded_id = uuid4()
    contact_id = uuid4()
    proposal_row = {
        "id": recorded_id,
        "state": "candidate",
        "model_run_id": uuid4(),
        "validator_policy_version": 1,
        "contact_id": contact_id if contact_source == "proposal" else None,
    }
    repo._proposal_row = AsyncMock(return_value=proposal_row)  # type: ignore[method-assign]
    session.execute.side_effect = [
        _Result(rows=[]),
        _Result(
            rows=[
                {
                    "message_revision_id": source.source_id,
                    "evidence_role": evidence.role.value,
                    "quoted_span_start": evidence.span_start,
                    "quoted_span_end": evidence.span_end,
                    "source_content_sha256": evidence.source_content_sha256,
                    "trust_class": evidence.trust.value,
                }
            ]
        ),
        *(_Result() for _ in range(5)),
    ]
    scalar_values: list[object] = [0, None]
    if contact_source != "proposal":
        scalar_values.append(contact_id if contact_source == "conversation" else None)
    session.scalar.side_effect = scalar_values

    if contact_source == "missing":
        with pytest.raises(MemoryConflictError, match="no contact scope"):
            await repo.accept_validated_proposal(
                validated,
                recorded_proposal_id=recorded_id,
                allow_candidate=True,
                now=NOW,
            )
        assert session.execute.await_count == 2
        return

    result = await repo.accept_validated_proposal(
        validated,
        recorded_proposal_id=recorded_id,
        allow_candidate=True,
        now=NOW,
    )
    assert result.state is ProposalState.ACCEPTED
    assert result.memory_id is not None
    assert result.memory_version_id is not None
    if contact_source == "conversation":
        assert session.scalar.await_count == 3


@pytest.mark.asyncio
async def test_accept_proposal_uses_recorded_target_snapshot() -> None:
    repo, session = _repository()
    account_id, conversation_id, target_id = uuid4(), uuid4(), uuid4()
    source = _source()
    base = _proposal(account_id, conversation_id, source)
    validated = replace(
        base,
        proposal=replace(
            base.proposal,
            operation=MemoryOperation.UPDATE,
            target_memory_ids=(target_id,),
        ),
    )
    recorded_id = uuid4()
    repo._proposal_row = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": recorded_id, "state": "candidate"}
    )
    session.execute.side_effect = [
        _Result(
            rows=[
                {
                    "target_memory_id": target_id,
                    "target_version_no_snapshot": 1,
                    "target_role": "primary",
                }
            ]
        ),
        _Result(
            rows=[
                {
                    "message_revision_id": source.source_id,
                    "evidence_role": "primary",
                    "quoted_span_start": None,
                    "quoted_span_end": None,
                    "source_content_sha256": source.content_sha256,
                    "trust_class": source.trust.value,
                }
            ]
        ),
        _Result(
            rows=[
                {
                    "id": target_id,
                    "current_version_no": 2,
                    "status": "active",
                    "memory_type": "fact",
                    "semantic_key_hash": validated.proposal.semantic_key_hash,
                }
            ]
        ),
    ]
    session.scalar.return_value = 0
    with pytest.raises(MemoryConflictError, match="target version changed"):
        await repo.accept_validated_proposal(
            validated,
            recorded_proposal_id=recorded_id,
            allow_candidate=True,
        )


@pytest.mark.asyncio
async def test_accept_merge_rejects_target_identity_drift() -> None:
    repo, session = _repository()
    account_id, conversation_id = uuid4(), uuid4()
    source = _source()
    first_target, second_target = uuid4(), uuid4()
    base = _proposal(account_id, conversation_id, source)
    validated = replace(
        base,
        proposal=replace(
            base.proposal,
            operation=MemoryOperation.MERGE,
            target_memory_ids=(first_target, second_target),
        ),
    )
    recorded_id = uuid4()
    repo._proposal_row = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": recorded_id, "state": "candidate"}
    )
    session.execute.side_effect = [
        _Result(
            rows=[
                {
                    "target_memory_id": first_target,
                    "target_version_no_snapshot": 1,
                    "target_role": "primary",
                },
                {
                    "target_memory_id": second_target,
                    "target_version_no_snapshot": 1,
                    "target_role": "merge_source",
                },
            ]
        ),
        _Result(
            rows=[
                {
                    "message_revision_id": source.source_id,
                    "evidence_role": "primary",
                    "quoted_span_start": None,
                    "quoted_span_end": None,
                    "source_content_sha256": source.content_sha256,
                    "trust_class": source.trust.value,
                }
            ]
        ),
        _Result(
            rows=[
                {
                    "id": first_target,
                    "current_version_no": 1,
                    "status": "active",
                    "memory_type": "preference",
                    "semantic_key_hash": validated.proposal.semantic_key_hash,
                },
                {
                    "id": second_target,
                    "current_version_no": 1,
                    "status": "active",
                    "memory_type": "fact",
                    "semantic_key_hash": validated.proposal.semantic_key_hash,
                },
            ]
        ),
    ]
    session.scalar.return_value = 0
    with pytest.raises(MemoryConflictError, match="target memory identity"):
        await repo.accept_validated_proposal(
            validated,
            recorded_proposal_id=recorded_id,
            allow_candidate=True,
        )


@pytest.mark.asyncio
async def test_accept_supersede_releases_same_key_before_insert() -> None:
    repo, session = _repository()
    account_id, conversation_id, target_id = uuid4(), uuid4(), uuid4()
    source = _source()
    base = _proposal(account_id, conversation_id, source)
    validated = replace(
        base,
        proposal=replace(
            base.proposal,
            operation=MemoryOperation.SUPERSEDE,
            target_memory_ids=(target_id,),
        ),
    )
    recorded_id, contact_id = uuid4(), uuid4()
    repo._proposal_row = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": recorded_id,
            "state": "candidate",
            "model_run_id": uuid4(),
            "validator_policy_version": 1,
            "contact_id": contact_id,
        }
    )
    session.execute.side_effect = [
        _Result(
            rows=[
                {
                    "target_memory_id": target_id,
                    "target_version_no_snapshot": 1,
                    "target_role": "superseded",
                }
            ]
        ),
        _Result(
            rows=[
                {
                    "message_revision_id": source.source_id,
                    "evidence_role": "primary",
                    "quoted_span_start": None,
                    "quoted_span_end": None,
                    "source_content_sha256": source.content_sha256,
                    "trust_class": source.trust.value,
                }
            ]
        ),
        _Result(
            rows=[
                {
                    "id": target_id,
                    "current_version_no": 1,
                    "status": "active",
                    "memory_type": validated.proposal.memory_type.value,
                    "semantic_key_hash": validated.proposal.semantic_key_hash,
                }
            ]
        ),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
    ]
    session.scalar.side_effect = [0, target_id]
    result = await repo.accept_validated_proposal(
        validated,
        recorded_proposal_id=recorded_id,
        allow_candidate=True,
        now=NOW,
    )
    assert result.state is ProposalState.ACCEPTED
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    first_release = next(
        index
        for index, statement in enumerate(statements)
        if "UPDATE memories" in statement and "superseded_by_memory_id" in statement
    )
    memory_insert = next(
        index for index, statement in enumerate(statements) if "INSERT INTO memories" in statement
    )
    assert first_release < memory_insert


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
    repo._require_summary_source_current = AsyncMock()  # type: ignore[method-assign]
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
    durable_sources = [
        {
            "summary_version_id": first.id,
            "ordinal": first.sources[0].ordinal,
            "message_revision_id": first.sources[0].source_id,
            "prior_summary_version_id": None,
            "source_content_sha256": first.sources[0].content_sha256,
        }
    ]
    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(durable_summary_version),
        _Result(rows=durable_sources),
    ]
    repo._require_summary_source_current.side_effect = ValueError("source stale")
    replay = await repo.publish_summary(
        first, account_id=account_id, conversation_id=conversation_id, now=NOW
    )
    assert replay.last_summary_version_id == first.id
    assert repo._require_summary_source_current.await_count == 1
    repo._require_summary_source_current.side_effect = None

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

    session.execute.side_effect = [
        _Result(),
        _Result(durable_watermark),
        _Result(durable_summary),
        _Result(durable_summary_version),
        _Result(rows=[durable_sources[0] | {"source_content_sha256": b"x" * 32}]),
    ]
    with pytest.raises(SummaryCoverageError, match="source membership changed"):
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
    source_content = "SYNTHETIC_EMBEDDING_SOURCE"
    source_hash = sha256(source_content.encode()).digest()
    records = tuple(
        EmbeddingRecord(uuid4(), space.id, uuid4(), kind, 0, source_hash, (0.1, 0.2))
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
    repo._require_embedding_target_current = AsyncMock(  # type: ignore[method-assign]
        return_value=source_content
    )
    persisted_records = [
        {
            "memory_version_id": record.target_id
            if record.target_kind == "memory_version"
            else None,
            "summary_version_id": record.target_id
            if record.target_kind == "summary_version"
            else None,
            "message_revision_id": None,
            "chunk_index": record.chunk_index,
            "chunker_version": space.chunker_version,
            "source_sha256": record.source_sha256,
            "dimensions": space.dimensions,
            "state": record.state.value,
            "invalidated_at": None,
        }
        for record in records
    ]
    session.execute.side_effect = [
        _Result(),
        _Result(),
        _Result(durable_space),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        _Result(rows=persisted_records),
        _Result(
            {
                "vector_payload": list(records[0].vector),
                "dimensions": space.dimensions,
                "source_sha256": source_hash,
            }
        ),
        _Result(),
        _Result(rowcount=1),
    ]
    assert (
        await repo.write_embedding_records(
            space,
            records,
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            expected_target_chunks={
                (record.target_kind, record.target_id): 1 for record in records
            },
            final_delta=0,
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
    with pytest.raises(ValueError, match="building space"):
        await repo.write_embedding_records(
            replace(space, state=EmbeddingState.ACTIVE),
            (),
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

    proof_repo, proof_session = _repository()
    with pytest.raises(ValueError, match="activation proof is incomplete"):
        await proof_repo.write_embedding_records(
            space,
            (record,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            now=NOW,
        )
    proof_session.execute.assert_not_awaited()

    coverage_repo, coverage_session = _repository()
    coverage_repo._require_embedding_target_current = AsyncMock()  # type: ignore[method-assign]
    coverage_session.execute.side_effect = [
        _Result(),
        _Result(),
        _Result(durable_space),
        _Result(rows=[]),
    ]
    with pytest.raises(ValueError, match="target coverage is incomplete"):
        await coverage_repo.write_embedding_records(
            space,
            (),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            expected_target_chunks={(record.target_kind, record.target_id): 1},
            final_delta=0,
            now=NOW,
        )

    active_repo, active_session = _repository()
    active_repo._require_embedding_target_scope = AsyncMock()  # type: ignore[method-assign]
    active_session.execute.side_effect = [
        _Result(),
        _Result(),
        _Result(durable_space | {"state": "active"}),
        _Result(),
    ]
    with pytest.raises(ValueError, match="active embedding space is immutable"):
        await active_repo.write_embedding_records(
            space,
            (record,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            expected_target_chunks={(record.target_kind, record.target_id): 1},
            final_delta=0,
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
@pytest.mark.parametrize(
    "failure",
    [
        "source_hash",
        "sample_missing",
        "sample_dimensions",
        "sample_hash",
        "sample_vector",
        "sample_nonfinite",
    ],
)
async def test_embedding_activation_verifies_canonical_hash_and_sample_retrieval(
    failure: str,
) -> None:
    account_id, profile_id, config_id = uuid4(), uuid4(), uuid4()
    space = EmbeddingSpace(uuid4(), "fake-embedding", 2, "cosine", "l2", "v1", 1)
    content = "SYNTHETIC_EMBEDDING_SOURCE"
    source_hash = sha256(content.encode()).digest()
    record = EmbeddingRecord(
        uuid4(), space.id, uuid4(), "memory_version", 0, source_hash, (0.1, 0.2)
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
    persisted = {
        "memory_version_id": record.target_id,
        "summary_version_id": None,
        "message_revision_id": None,
        "chunk_index": 0,
        "chunker_version": space.chunker_version,
        "source_sha256": source_hash,
        "dimensions": space.dimensions,
        "state": record.state.value,
        "invalidated_at": None,
    }
    repo, session = _repository()
    repo._require_embedding_target_scope = AsyncMock()  # type: ignore[method-assign]
    repo._require_embedding_target_current = AsyncMock(  # type: ignore[method-assign]
        return_value="CHANGED_SOURCE" if failure == "source_hash" else content
    )
    results = [
        _Result(),
        _Result(),
        _Result(durable_space),
        _Result(),
        _Result(),
        _Result(rows=[persisted]),
    ]
    if failure != "source_hash":
        sample_row: dict[str, Any] = {
            "vector_payload": list(record.vector),
            "dimensions": space.dimensions,
            "source_sha256": source_hash,
        }
        if failure == "sample_dimensions":
            sample_row["dimensions"] = space.dimensions + 1
        elif failure == "sample_hash":
            sample_row["source_sha256"] = b"short"
        elif failure == "sample_vector":
            sample_row["vector_payload"] = [0.1]
        elif failure == "sample_nonfinite":
            sample_row["vector_payload"] = [0.1, float("nan")]
        results.append(_Result(None if failure == "sample_missing" else sample_row))
    session.execute.side_effect = results
    expected_error = "source hashes are stale" if failure == "source_hash" else "sample retrieval"
    with pytest.raises(ValueError, match=expected_error):
        await repo.write_embedding_records(
            space,
            (record,),
            account_id=account_id,
            model_profile_id=profile_id,
            config_version_id=config_id,
            activate=True,
            expected_target_chunks={(record.target_kind, record.target_id): 1},
            final_delta=0,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_forget_and_erasure_are_account_scoped_and_idempotently_shaped() -> None:
    repo, session = _repository()
    account_id, conversation_id, memory_id = uuid4(), uuid4(), uuid4()
    session.execute.side_effect = [_Result()]
    assert not await repo.forget_memory(account_id=account_id, memory_id=memory_id, now=NOW)

    session.execute.side_effect = [_Result({"id": memory_id}), _Result(), _Result(), _Result()]
    assert await repo.forget_memory(account_id=account_id, memory_id=memory_id, now=NOW)

    session.execute.reset_mock()
    session.execute.side_effect = [_Result({"id": memory_id, "status": "forgotten"})]
    assert await repo.forget_memory(account_id=account_id, memory_id=memory_id, now=NOW)
    assert session.execute.await_count == 1

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
        scope_secret=ERASURE_SECRET,
        now=NOW,
    )
    assert request_id
    repo._require_erasure_derived_scope.assert_awaited_once()
    repo.forget_memory.assert_awaited_once()

    session.execute.side_effect = [_Result()]
    with pytest.raises(ValueError, match="outside"):
        await repo.record_erasure(
            account_id=account_id,
            memory_id=memory_id,
            scope_secret=ERASURE_SECRET,
            now=NOW,
        )
    with pytest.raises(ValueError, match="metadata"):
        await repo.record_erasure(
            account_id=account_id,
            memory_id=memory_id,
            policy_version=0,
            scope_secret=ERASURE_SECRET,
            now=NOW,
        )
    with pytest.raises(ValueError, match="source"):
        await repo.record_erasure(account_id=account_id, scope_secret=ERASURE_SECRET, now=NOW)
    with pytest.raises(ValueError, match="metadata"):
        await repo.record_erasure(
            account_id=account_id,
            memory_id=memory_id,
            scope_secret=b"short",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_review_action_requires_and_returns_the_expected_proposal_version() -> None:
    repo, session = _repository()
    account_id, conversation_id, proposal_id, action_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with pytest.raises(ValueError, match="review action"):
        await repo.issue_review_action(
            account_id=account_id,
            conversation_id=conversation_id,
            admin_actor_id=42,
            bot_chat_id=42,
            action="accept",
            proposal_id=proposal_id,
            memory_id=None,
            token=b"t" * 32,
            expires_at=NOW + timedelta(minutes=5),
        )

    action_row = {
        "id": action_id,
        "account_id": account_id,
        "conversation_id": conversation_id,
        "action": "accept",
        "proposal_id": proposal_id,
        "memory_id": None,
        "expected_proposal_version": 3,
        "expected_memory_version": None,
        "admin_actor_id": 42,
        "expires_at": NOW + timedelta(minutes=5),
    }
    proposal_row = {
        "state": "candidate",
        "review_version": 3,
        "expires_at": NOW + timedelta(hours=1),
    }
    session.execute.side_effect = [_Result(action_row), _Result(proposal_row)]
    session.scalar.side_effect = [conversation_id, 0, 0, action_id]
    action = await repo.consume_review_action(
        token=b"t" * 32,
        admin_actor_id=42,
        bot_chat_id=42,
        now=NOW,
    )
    assert action is not None
    assert action.expected_proposal_version == 3


@pytest.mark.asyncio
async def test_review_action_rechecks_evidence_and_expiry_before_confirmation_or_work() -> None:
    repo, session = _repository()
    account_id, conversation_id, proposal_id, action_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    action_row = {
        "id": action_id,
        "account_id": account_id,
        "conversation_id": conversation_id,
        "action": "accept",
        "proposal_id": proposal_id,
        "memory_id": None,
        "expected_proposal_version": 1,
        "expected_memory_version": None,
        "admin_actor_id": 42,
        "expires_at": NOW + timedelta(minutes=5),
    }
    proposal_row = {
        "state": "candidate",
        "review_version": 1,
        "expires_at": NOW + timedelta(hours=1),
    }
    session.execute.side_effect = [
        _Result(action_row),
        _Result(proposal_row),
        _Result(rowcount=1),
    ]
    session.scalar.side_effect = [conversation_id, 1]
    assert (
        await repo.consume_review_action(
            token=b"t" * 32,
            admin_actor_id=42,
            bot_chat_id=42,
            now=NOW,
        )
        is None
    )
    terminal_statement = session.execute.await_args_list[-1].args[0]
    assert "memory_proposal_evidence_changed" in terminal_statement.compile().params.values()

    expired_row = dict(action_row, state="confirmed", expires_at=NOW)
    session.reset_mock()
    session.execute.side_effect = [_Result(expired_row), _Result(rowcount=1)]
    assert await repo.lock_confirmed_review_action(account_id=account_id, now=NOW) is None
    expiry_statement = session.execute.await_args_list[-1].args[0]
    assert "review_action_expired" in expiry_statement.compile().params.values()


def _review_action(action: str) -> ReviewAction:
    proposal_id = uuid4() if action in {"accept", "reject"} else None
    memory_id = uuid4() if action == "forget" else None
    return ReviewAction(
        uuid4(),
        uuid4(),
        uuid4(),
        action,
        proposal_id,
        memory_id,
        2 if proposal_id is not None else None,
        3 if memory_id is not None else None,
        42,
        NOW + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_review_action_executor_terminalizes_success_and_conflict() -> None:
    repo, session = _repository()
    action = _review_action("reject")
    session.begin_nested = MagicMock(return_value=_NestedTransaction())
    repo.lock_confirmed_review_action = AsyncMock(return_value=action)  # type: ignore[method-assign]
    repo._apply_review_action = AsyncMock()  # type: ignore[method-assign]
    repo.finish_review_action = AsyncMock(return_value=True)  # type: ignore[method-assign]

    execution = await repo.execute_next_review_action(
        account_id=action.account_id,
        now=NOW,
        erasure_scope_secret=ERASURE_SECRET,
    )
    assert execution is not None
    assert (execution.action, execution.applied, execution.reason_code) == (
        "reject",
        True,
        None,
    )

    repo._apply_review_action.side_effect = MemoryConflictError("synthetic conflict")
    execution = await repo.execute_next_review_action(
        account_id=action.account_id,
        now=NOW,
        erasure_scope_secret=ERASURE_SECRET,
    )
    assert execution is not None
    assert (execution.applied, execution.reason_code) == (False, "memory_review_conflict")
    finish_call = repo.finish_review_action.await_args
    assert finish_call is not None
    assert finish_call.kwargs["reason_code"] == "memory_review_conflict"


@pytest.mark.asyncio
async def test_review_action_apply_paths_bind_actor_version_and_erasure_key() -> None:
    repo, session = _repository()
    accepted = _review_action("accept")
    validated = SimpleNamespace()
    repo._persisted_candidate_for_review = AsyncMock(  # type: ignore[method-assign]
        return_value=(validated, {})
    )
    repo._accept_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value=AcceptanceResult(
            cast(UUID, accepted.proposal_id),
            ProposalState.ACCEPTED,
            uuid4(),
            uuid4(),
        )
    )
    await repo._apply_review_action(
        action=accepted,
        now=NOW,
        erasure_scope_secret=ERASURE_SECRET,
    )
    acceptance_call = repo._accept_proposal.await_args
    assert acceptance_call is not None
    assert acceptance_call.kwargs["decision_actor_type"] == "human"
    assert acceptance_call.kwargs["decision_actor_id"] == "42"

    rejected = _review_action("reject")
    session.execute.return_value = _Result(rowcount=1)
    await repo._apply_review_action(
        action=rejected,
        now=NOW,
        erasure_scope_secret=ERASURE_SECRET,
    )
    reject_params = session.execute.await_args.args[0].compile().params.values()
    assert "manual_reject" in reject_params
    assert "42" in reject_params
    assert rejected.expected_proposal_version in reject_params

    forgotten = _review_action("forget")
    repo.record_erasure = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    await repo._apply_review_action(
        action=forgotten,
        now=NOW,
        erasure_scope_secret=ERASURE_SECRET,
    )
    erasure_call = repo.record_erasure.await_args
    assert erasure_call is not None
    assert erasure_call.kwargs["scope_secret"] == ERASURE_SECRET
    assert erasure_call.kwargs["requested_by"] == "control_admin:42"
