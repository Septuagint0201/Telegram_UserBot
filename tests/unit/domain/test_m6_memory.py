"""M6 deterministic memory, summary, embedding, and erasure contracts."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest

from telegram_userbot.adapters.persistence.memory_repository import _proposal_storage_id
from telegram_userbot.domain.memory import (
    DerivedKind,
    EmbeddingSpaceManager,
    EventRange,
    EvidenceGraphNode,
    FakeEmbeddingProvider,
    FreshnessInput,
    GenerationQueue,
    InputManifest,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryStore,
    ProposalState,
    ReconciliationLedger,
    SummaryCoverageError,
    SummaryKind,
    SummarySource,
    SummaryStatus,
    SummaryStore,
    SummaryVersion,
    SummaryWatermark,
    TriggerInput,
    TriggerPolicy,
    TriggerReason,
    calculate_freshness,
    chunk_text,
    evaluate_triggers,
    parse_agent_response,
    rolling_summary_due,
    summary_period,
    validate_evidence_roots,
    validate_proposal,
)
from telegram_userbot.domain.memory.embedding import ShadowBuildResult
from telegram_userbot.domain.memory.fakes import FakeMemoryAgent, FakeSummaryProvider
from telegram_userbot.domain.memory.models import (
    EmbeddingRecord,
    EmbeddingSpace,
    Evidence,
    EvidenceRole,
    InputSource,
    MemoryOperation,
    MemoryProposal,
    MemoryRecord,
    MemoryType,
    MemoryVersion,
    TrustClass,
)
from telegram_userbot.domain.memory.reconciliation import ErasureEntry
from telegram_userbot.domain.memory.validation import (
    ProposalValidationError,
    ValidatedProposal,
)

ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
CONVERSATION = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _construct[T](factory: Callable[..., T], values: Mapping[str, object]) -> T:
    return factory(**values)


def _manifest(*, visual_only: bool = False) -> tuple[InputManifest, UUID]:
    source_id = uuid4()
    content = "I prefer tea."
    source = InputSource(
        source_id=source_id,
        revision="revision-1",
        content=content,
        content_sha256=sha256(content.encode()).digest(),
        visual_only=visual_only,
    )
    return (
        InputManifest(
            id=uuid4(),
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            generation=1,
            range_start_event_id=1,
            range_end_event_id=1,
            sources=(source,),
            pipeline_version="m6-v1",
            policy_version="policy-v1",
            prompt_version="prompt-v1",
            input_token_estimate=20,
        ),
        source_id,
    )


def _payload(
    source_id: UUID,
    *,
    confidence: float = 0.9,
    visual_only: bool = False,
    semantic_key: str = "beverage preference",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "proposals": [
            {
                "operation": "create",
                "memory_type": "preference",
                "semantic_key": semantic_key,
                "payload": {"value": "tea"},
                "confidence": confidence,
                "importance": 0.7,
                "visual_only": visual_only,
                "evidence": [
                    {
                        "source_id": str(source_id),
                        "source_revision": "revision-1",
                        "source_content_sha256": sha256(b"I prefer tea.").hexdigest(),
                    }
                ],
            }
        ],
    }


def test_memory_proposal_normalizes_valid_interval_and_rejects_naive_datetime() -> None:
    source_id = uuid4()
    source = InputSource(
        source_id=source_id,
        revision="revision-1",
        content="synthetic",
        content_sha256=sha256(b"synthetic").digest(),
    )
    proposal = MemoryProposal(
        id=uuid4(),
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        operation=MemoryOperation.CREATE,
        memory_type=MemoryType.FACT,
        semantic_key="synthetic fact",
        payload={"value": "synthetic"},
        confidence=0.9,
        importance=0.5,
        evidence=(Evidence(source_id, "revision-1", source.content_sha256),),
        valid_from=datetime(2030, 1, 1, 8, tzinfo=UTC),
        valid_to=datetime(2030, 2, 1, 8, tzinfo=UTC),
    )
    assert proposal.valid_from == datetime(2030, 1, 1, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        MemoryProposal(
            id=uuid4(),
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            operation=MemoryOperation.CREATE,
            memory_type=MemoryType.FACT,
            semantic_key="synthetic fact",
            payload={"value": "synthetic"},
            confidence=0.9,
            importance=0.5,
            evidence=(Evidence(source_id, "revision-1", source.content_sha256),),
            valid_from=datetime(2030, 1, 1),  # noqa: DTZ001 - deliberately naive input
        )


def test_trigger_conditions_are_independent_or_and_compensation_is_visible() -> None:
    decision = evaluate_triggers(
        TriggerInput(
            now=NOW,
            last_eligible_at=NOW - timedelta(seconds=45),
            oldest_uncovered_at=None,
            eligible_revision_count=20,
            estimated_input_tokens=6_000,
            last_compensation_scan_at=NOW - timedelta(minutes=5),
            eligible_outgoing=True,
            auto_mode=False,
        )
    )
    assert decision.due
    assert set(decision.reasons) == {
        TriggerReason.QUIET_WINDOW,
        TriggerReason.REVISION_BATCH,
        TriggerReason.TOKEN_BATCH,
        TriggerReason.COMPENSATION_SCAN,
        TriggerReason.OUTGOING_NON_AUTO,
    }


def test_generation_running_range_is_immutable_and_new_events_get_next_generation() -> None:
    queue = GenerationQueue()
    first = queue.refresh(EventRange(10, 12))
    assert queue.refresh(EventRange(12, 15)).event_range == EventRange(10, 15)
    queue.claim(first.generation, "worker-a", fencing_token=1)
    next_generation = queue.refresh(EventRange(16, 16))
    assert next_generation.generation == 2
    assert next_generation.event_range == EventRange(16, 16)


def test_strict_parser_rejects_unknown_fields_and_validates_current_evidence() -> None:
    manifest, source_id = _manifest()
    parsed = parse_agent_response(
        _payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION
    )
    result = validate_proposal(parsed[0], manifest)
    assert result.state is ProposalState.ACCEPTED
    bad = _payload(source_id)
    bad["unexpected"] = True
    with pytest.raises(ProposalValidationError, match="unknown field"):
        parse_agent_response(bad, account_id=ACCOUNT, conversation_id=CONVERSATION)


def test_confidence_visual_only_and_low_confidence_never_auto_accept() -> None:
    manifest, source_id = _manifest(visual_only=True)
    parsed = parse_agent_response(
        _payload(source_id, confidence=0.95), account_id=ACCOUNT, conversation_id=CONVERSATION
    )
    assert validate_proposal(parsed[0], manifest).state is ProposalState.CANDIDATE
    manifest, source_id = _manifest()
    parsed = parse_agent_response(
        _payload(source_id, confidence=0.4), account_id=ACCOUNT, conversation_id=CONVERSATION
    )
    assert validate_proposal(parsed[0], manifest).state is ProposalState.REJECTED


def test_memory_store_acceptance_is_idempotent_and_forget_redacts_every_version() -> None:
    manifest, source_id = _manifest()
    parsed = parse_agent_response(
        _payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION
    )
    validated = validate_proposal(parsed[0], manifest)
    store = MemoryStore()
    first = store.accept(validated)
    second = store.accept(validated)
    assert first.memory_id is not None
    assert second.idempotent
    forgotten = store.forget(first.memory_id)
    assert forgotten.current.payload == {}
    assert forgotten.current.rendered_text is None


def test_summary_membership_period_and_late_source_invalidation() -> None:
    source_id = uuid4()
    conversation_id = uuid4()
    digest = sha256(b"source").digest()
    summary = SummaryVersion(
        id=uuid4(),
        summary_id=uuid4(),
        version_no=1,
        kind=SummaryKind.ROLLING,
        range_start_event_id=1,
        range_end_event_id=2,
        content_text="summary",
        sources=(SummarySource(source_id, "message_revision", digest, 1),),
        manifest_sha256=sha256(b"manifest").digest(),
        status=SummaryStatus.ACTIVE,
    )
    store = SummaryStore()
    store.publish(summary, conversation_id=conversation_id)
    assert store.invalidate_for_sources({source_id}) == 1
    assert store.invalidate_for_sources({source_id}) == 0
    rebuilt = replace(
        summary,
        id=uuid4(),
        version_no=2,
        range_start_event_id=3,
        range_end_event_id=3,
        status=SummaryStatus.ACTIVE,
    )
    store.publish(rebuilt, conversation_id=conversation_id, expected_version=1)
    key, start, end = summary_period(SummaryKind.DAILY, NOW, timezone_name="Asia/Shanghai")
    assert key == "2026-08-15"
    assert start < end


def test_embedding_shadow_activation_isolated_and_dimension_checked() -> None:
    manager = EmbeddingSpaceManager()
    profile = uuid4()
    shadow = manager.create_shadow(profile_id=profile, model_name="fake", dimensions=8)
    target_id = uuid4()
    result = manager.build(
        shadow,
        profile_id=profile,
        targets=((target_id, "memory_version", "stable fact"),),
        provider=FakeEmbeddingProvider(),
    )
    replay = manager.build(
        shadow,
        profile_id=profile,
        targets=((target_id, "memory_version", "stable fact"),),
    )
    assert replay.records == result.records
    with pytest.raises(ValueError, match="content changed"):
        manager.build(
            shadow,
            profile_id=profile,
            targets=((target_id, "memory_version", "changed fact"),),
        )
    active = manager.activate(result, profile_id=profile)
    assert active.state.value == "active"
    assert all(len(record.vector) == 8 for record in result.records)
    assert not chunk_text("")
    with pytest.raises(ValueError, match="selected profile"):
        manager.activate(result, profile_id=uuid4())
    empty = manager.create_shadow(profile_id=profile, model_name="fake-empty", dimensions=8)
    empty_target_id = uuid4()
    uncovered = manager.build(
        empty,
        profile_id=profile,
        targets=((empty_target_id, "memory_version", ""),),
    )
    assert not uncovered.verified
    with pytest.raises(ValueError, match="content changed"):
        manager.build(
            empty,
            profile_id=profile,
            targets=((empty_target_id, "memory_version", "now populated"),),
        )
    with pytest.raises(ValueError, match="verification failed"):
        manager.activate(uncovered, profile_id=profile)


def test_erasure_ledger_replays_without_reopening_derived_state_and_freshness_is_explicit() -> None:
    source_id = uuid4()
    ledger = ReconciliationLedger()
    entry = ledger.forget(
        source_id,
        reason="telegram_delete",
        derived_ids={DerivedKind.MEMORY: {uuid4()}, DerivedKind.EMBEDDING: {uuid4()}},
    )
    restored = ReconciliationLedger()
    assert restored.restore_replay((entry,)) == 1
    assert restored.invalidated[DerivedKind.MEMORY] == ledger.invalidated[DerivedKind.MEMORY]
    assert restored.invalidated[DerivedKind.EMBEDDING] == ledger.invalidated[DerivedKind.EMBEDDING]
    assert not restored.can_reopen(source_id)
    assert calculate_freshness(FreshnessInput(NOW, NOW, None)) == "fresh"
    assert calculate_freshness(FreshnessInput(NOW, NOW, NOW - timedelta(minutes=20))) == "stale"


def test_recursive_evidence_resolves_canonical_root_and_rejects_cycles() -> None:
    root, first, second = uuid4(), uuid4(), uuid4()
    graph = {
        root: EvidenceGraphNode(root, "canonical_message"),
        first: EvidenceGraphNode(first, "summary_version", (root,)),
        second: EvidenceGraphNode(second, "memory_version", (first,)),
    }
    assert validate_evidence_roots(second, graph) == (root,)
    cyclic = {
        first: EvidenceGraphNode(first, "memory_version", (second,)),
        second: EvidenceGraphNode(second, "summary_version", (first,)),
    }
    with pytest.raises(ProposalValidationError, match="cycle"):
        validate_evidence_roots(first, cyclic)


@pytest.mark.asyncio
async def test_fake_memory_and_summary_providers_are_offline_and_fail_explicitly() -> None:
    manifest, source_id = _manifest()
    agent = FakeMemoryAgent(json.dumps(_payload(source_id)))
    assert len(await agent.extract_validated(account_id=ACCOUNT, conversation_id=CONVERSATION)) == 1
    assert manifest.sources
    broken = FakeMemoryAgent("not-json")
    with pytest.raises(ProposalValidationError, match="not JSON"):
        await broken.extract_validated(account_id=ACCOUNT, conversation_id=CONVERSATION)
    failed = FakeMemoryAgent({}, fail_with=TimeoutError("synthetic timeout"))
    with pytest.raises(TimeoutError, match="synthetic timeout"):
        await failed.extract()
    summary = FakeSummaryProvider("summary", delay_seconds=0.001)
    assert await summary.summarize("synthetic") == "summary"


def test_memory_update_merge_manual_candidate_and_conflict_paths() -> None:
    manifest, source_id = _manifest()
    store = MemoryStore()
    first_payload = _payload(source_id)
    first_proposal = cast(list[dict[str, object]], first_payload["proposals"])[0]
    assert isinstance(first_proposal, dict)
    first_proposal["id"] = str(uuid4())
    first = validate_proposal(
        parse_agent_response(first_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[0],
        manifest,
    )
    first_result = store.accept(first)
    assert first_result.memory_id is not None

    update_payload = _payload(source_id)
    update_proposal = cast(list[dict[str, object]], update_payload["proposals"])[0]
    assert isinstance(update_proposal, dict)
    update_proposal.update(
        id=str(uuid4()), operation="update", targets=[str(first_result.memory_id)]
    )
    update = validate_proposal(
        parse_agent_response(update_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[0],
        manifest,
    )
    updated = store.accept(update, expected_versions={first_result.memory_id: 1})
    assert updated.memory_version_id is not None

    stale_payload = _payload(source_id)
    stale_proposal = cast(list[dict[str, object]], stale_payload["proposals"])[0]
    assert isinstance(stale_proposal, dict)
    stale_proposal.update(
        id=str(uuid4()), operation="update", targets=[str(first_result.memory_id)]
    )
    stale = validate_proposal(
        parse_agent_response(stale_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[0],
        manifest,
    )
    with pytest.raises(MemoryConflictError, match="version changed"):
        store.accept(stale, expected_versions={first_result.memory_id: 1})

    candidate_payload = _payload(source_id, confidence=0.7)
    candidate_proposal = cast(list[dict[str, object]], candidate_payload["proposals"])[0]
    assert isinstance(candidate_proposal, dict)
    candidate_proposal.update(id=str(uuid4()), semantic_key="manual preference")
    candidate = validate_proposal(
        parse_agent_response(candidate_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[
            0
        ],
        manifest,
    )
    assert candidate.state is ProposalState.CANDIDATE
    manually_accepted = store.accept(candidate, acceptance_kind="manual", allow_candidate=True)
    assert manually_accepted.state is ProposalState.ACCEPTED


def test_trigger_summary_and_embedding_failure_edges_fail_closed() -> None:
    assert not evaluate_triggers(
        TriggerInput(NOW, NOW, None, 0, 0, last_compensation_scan_at=NOW)
    ).due
    hard = evaluate_triggers(
        TriggerInput(NOW, None, NOW - timedelta(minutes=10), 0, 0),
        TriggerPolicy(),
    )
    assert TriggerReason.HARD_DEADLINE in hard.reasons
    with pytest.raises(ValueError, match="disjoint"):
        EventRange(1, 1).merge(EventRange(3, 3))

    assert rolling_summary_due(eligible_revision_count=50, estimated_tokens=0)
    with pytest.raises(ValueError, match="timezone"):
        summary_period(SummaryKind.DAILY, NOW, timezone_name="Invalid/Zone")
    week_key, week_start, week_end = summary_period(
        SummaryKind.WEEKLY, NOW, timezone_name="Asia/Shanghai"
    )
    assert week_key == "2026-08-10"
    assert week_start < week_end

    manager = EmbeddingSpaceManager()
    profile = uuid4()
    shadow = manager.create_shadow(profile_id=profile, model_name="fake-fail", dimensions=4)
    with pytest.raises(ValueError, match="not embeddable"):
        manager.build(shadow, profile_id=profile, targets=((uuid4(), "candidate", "text"),))
    failed = ShadowBuildResult(shadow, (), 0, False, True, True, 0)
    with pytest.raises(ValueError, match="verification failed"):
        manager.activate(failed, profile_id=profile)


def test_strict_parser_rejects_malformed_schema_enum_and_evidence_hash() -> None:
    with pytest.raises(ProposalValidationError, match="expected object"):
        parse_agent_response([], account_id=ACCOUNT, conversation_id=CONVERSATION)
    with pytest.raises(ProposalValidationError, match="schema must be 1"):
        parse_agent_response(
            {"schema_version": 2, "proposals": []},
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
        )
    with pytest.raises(ProposalValidationError, match="proposals must be a list"):
        parse_agent_response(
            {"schema_version": 1, "proposals": {}},
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
        )

    _manifest_value, source_id = _manifest()
    invalid_enum = _payload(source_id)
    proposal = cast(list[dict[str, object]], invalid_enum["proposals"])[0]
    proposal["operation"] = "invent"
    with pytest.raises(ProposalValidationError, match="operation or memory type"):
        parse_agent_response(invalid_enum, account_id=ACCOUNT, conversation_id=CONVERSATION)

    invalid_hash = _payload(source_id)
    proposal = cast(list[dict[str, object]], invalid_hash["proposals"])[0]
    evidence = cast(list[dict[str, object]], proposal["evidence"])[0]
    evidence["source_content_sha256"] = "not-a-hash"
    with pytest.raises(ProposalValidationError, match="64 hex"):
        parse_agent_response(invalid_hash, account_id=ACCOUNT, conversation_id=CONVERSATION)


def test_m6_value_objects_reject_invalid_identity_scope_hash_and_ranges() -> None:
    source_id = uuid4()
    content = "source"
    digest = sha256(content.encode()).digest()
    evidence = Evidence(source_id, "revision-1", digest)
    valid_proposal = {
        "id": uuid4(),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "operation": MemoryOperation.CREATE,
        "memory_type": MemoryType.FACT,
        "semantic_key": "Key",
        "payload": {"nested": [1, 1.5, True, None, {"tuple": ("x",)}]},
        "confidence": 0.9,
        "importance": 0.5,
        "evidence": (evidence,),
    }
    assert _construct(MemoryProposal, valid_proposal).payload["nested"]
    for proposal_changes, message in (
        ({"semantic_key": " "}, "semantic key"),
        ({"confidence": 2.0}, "between zero and one"),
        (
            {
                "valid_from": NOW,
                "valid_to": NOW - timedelta(seconds=1),
            },
            "cannot precede",
        ),
        ({"payload": {"invalid": float("inf")}}, "non-finite"),
        ({"payload": {"invalid": object()}}, "non-JSON"),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(MemoryProposal, valid_proposal | proposal_changes)

    with pytest.raises(ValueError, match="SHA-256"):
        Evidence(source_id, "revision-1", b"short")
    with pytest.raises(ValueError, match="non-negative"):
        Evidence(source_id, "revision-1", digest, span_start=-1)
    with pytest.raises(ValueError, match="after start"):
        Evidence(source_id, "revision-1", digest, span_start=1, span_end=1)
    with pytest.raises(ValueError, match="both be present"):
        Evidence(source_id, "revision-1", digest, span_start=1)

    with pytest.raises(ValueError, match="unsupported"):
        InputSource(source_id, "revision-1", content, digest, source_type="provider_raw")
    with pytest.raises(ValueError, match="SHA-256"):
        InputSource(source_id, "revision-1", content, b"short")
    with pytest.raises(ValueError, match="hash mismatch"):
        InputSource(source_id, "revision-1", content, b"x" * 32)
    source = InputSource(source_id, "revision-1", content, digest)
    manifest_values = {
        "id": uuid4(),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "generation": 1,
        "range_start_event_id": 1,
        "range_end_event_id": 1,
        "sources": (source,),
        "pipeline_version": "v1",
        "policy_version": "v1",
        "prompt_version": "v1",
    }
    for manifest_changes, message in (
        ({"generation": 0}, "generation"),
        ({"range_end_event_id": 0}, "reversed"),
        ({"input_token_estimate": -1}, "negative"),
        ({"output_schema_version": 0}, "positive"),
        ({"sources": (source, source)}, "unique"),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(InputManifest, manifest_values | manifest_changes)
    inferred_source = replace(source, trust=TrustClass.MODEL_INFERENCE)
    assert (
        _construct(InputManifest, manifest_values).manifest_sha256
        != _construct(
            InputManifest, manifest_values | {"sources": (inferred_source,)}
        ).manifest_sha256
    )


def test_m6_version_summary_and_embedding_guards_are_fail_closed() -> None:
    memory_id = uuid4()
    digest = sha256(b"source").digest()
    evidence = Evidence(uuid4(), "revision-1", digest)
    version_values = {
        "id": uuid4(),
        "memory_id": memory_id,
        "version_no": 1,
        "operation": MemoryOperation.CREATE,
        "memory_type": MemoryType.FACT,
        "semantic_key_hash": digest,
        "payload": {"value": "fact"},
        "rendered_text": "fact",
        "confidence": 0.9,
        "importance": 0.5,
        "acceptance_kind": "automatic",
        "evidence": (evidence,),
    }
    version = _construct(MemoryVersion, version_values)
    for version_changes, message in (
        ({"version_no": 0}, "positive"),
        ({"semantic_key_hash": b"short"}, "SHA-256"),
        ({"acceptance_kind": "invented"}, "unknown"),
        ({"rendered_text": "x" * 20_001}, "too long"),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(MemoryVersion, version_values | version_changes)

    record = MemoryRecord(
        memory_id,
        ACCOUNT,
        CONVERSATION,
        MemoryType.FACT,
        digest,
        1,
        versions=(version,),
    )
    assert record.current is version
    missing = MemoryRecord(uuid4(), ACCOUNT, CONVERSATION, MemoryType.FACT, digest, 1, versions=())
    with pytest.raises(RuntimeError, match="no version"):
        _ = missing.current
    with pytest.raises(ValueError, match="not contiguous"):
        record.with_version(version)

    with pytest.raises(ValueError, match="membership"):
        SummarySource(uuid4(), "message_revision", b"short", 0)
    with pytest.raises(ValueError, match="kind"):
        SummarySource(uuid4(), "provider_raw", digest, 1)
    summary_source = SummarySource(uuid4(), "message_revision", digest, 1)
    summary_values = {
        "id": uuid4(),
        "summary_id": uuid4(),
        "version_no": 1,
        "kind": SummaryKind.ROLLING,
        "range_start_event_id": 1,
        "range_end_event_id": 1,
        "content_text": "summary",
        "sources": (summary_source,),
        "manifest_sha256": digest,
    }
    for summary_changes, message in (
        ({"version_no": 0}, "range"),
        ({"range_start_event_id": -1}, "range"),
        ({"content_text": " "}, "cannot be empty"),
        ({"manifest_sha256": b"short"}, "SHA-256"),
        ({"sources": ()}, "ordinals"),
        (
            {"sources": (replace(summary_source, ordinal=2),)},
            "ordinals",
        ),
        (
            {
                "sources": (
                    summary_source,
                    replace(summary_source, ordinal=2),
                )
            },
            "unique",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(SummaryVersion, summary_values | summary_changes)

    space_values = {
        "id": uuid4(),
        "model_name": "fake",
        "dimensions": 4,
        "distance_metric": "cosine",
        "normalization": "l2",
        "chunker_version": "v1",
        "generation": 1,
    }
    for space_changes, message in (
        ({"dimensions": 0}, "identity"),
        ({"distance_metric": "bad"}, "distance"),
        ({"normalization": "bad"}, "normalization"),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(EmbeddingSpace, space_values | space_changes)
    record_values = {
        "id": uuid4(),
        "space_id": uuid4(),
        "target_id": uuid4(),
        "target_kind": "memory_version",
        "chunk_index": 0,
        "source_sha256": digest,
        "vector": (0.1, 0.2),
    }
    for record_changes, message in (
        ({"target_kind": "candidate"}, "target kind"),
        ({"chunk_index": -1}, "identity"),
        ({"vector": ()}, "vector"),
    ):
        with pytest.raises(ValueError, match=message):
            _construct(EmbeddingRecord, record_values | record_changes)


def test_trigger_generation_guards_and_fencing_fail_closed() -> None:
    with pytest.raises(ValueError, match="thresholds"):
        TriggerPolicy(revision_threshold=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        TriggerInput(NOW.replace(tzinfo=None), None, None, 0, 0)
    trigger_values: dict[str, object] = {
        "now": NOW,
        "last_eligible_at": None,
        "oldest_uncovered_at": None,
        "eligible_revision_count": 0,
        "estimated_input_tokens": 0,
        "last_compensation_scan_at": None,
    }
    for field in ("last_eligible_at", "oldest_uncovered_at", "last_compensation_scan_at"):
        with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
            _construct(
                TriggerInput,
                trigger_values | {field: NOW.replace(tzinfo=None)},
            )
    with pytest.raises(ValueError, match="negative"):
        TriggerInput(NOW, None, None, -1, 0)
    with pytest.raises(ValueError, match="event range"):
        EventRange(2, 1)

    queue = GenerationQueue()
    first = queue.refresh(EventRange(1, 1))
    second = queue.refresh(EventRange(3, 3))
    assert (first.generation, second.generation) == (1, 2)
    with pytest.raises(ValueError, match="claim identity"):
        queue.claim(1, "", fencing_token=0)
    with pytest.raises(KeyError):
        queue.claim(99, "worker", fencing_token=1)
    claimed = queue.claim(1, "worker", fencing_token=1)
    with pytest.raises(ValueError, match="manifest identity"):
        queue.seal(1, "worker", 1, " ")
    with pytest.raises(ValueError, match="already claimed"):
        queue.claim(1, "worker", fencing_token=2)
    with pytest.raises(ValueError, match="stale"):
        queue.seal(1, "other", 1, "manifest")
    sealed = queue.seal(1, "worker", 1, "manifest")
    assert sealed.input_manifest_id == "manifest"
    with pytest.raises(KeyError):
        queue.seal(99, "worker", 1, "manifest")
    with pytest.raises(ValueError, match="immutable"):
        claimed.merge_pending(EventRange(1, 2))


def test_summary_version_watermark_and_period_guards() -> None:
    source = SummarySource(uuid4(), "message_revision", sha256(b"source").digest(), 1)
    summary_id = uuid4()
    conversation_id = uuid4()

    def version(number: int, start: int, end: int) -> SummaryVersion:
        return SummaryVersion(
            id=uuid4(),
            summary_id=summary_id,
            version_no=number,
            kind=SummaryKind.ROLLING,
            range_start_event_id=start,
            range_end_event_id=end,
            content_text=f"summary-{number}",
            sources=(source,),
            manifest_sha256=sha256(f"manifest-{number}".encode()).digest(),
        )

    watermark = SummaryWatermark(summary_id, SummaryKind.ROLLING, last_included_event_id=2)
    with pytest.raises(SummaryCoverageError, match="skip"):
        watermark.advance(version(1, 4, 4))
    with pytest.raises(SummaryCoverageError, match="backwards"):
        watermark.advance(version(1, 1, 1))

    store = SummaryStore()
    with pytest.raises(SummaryCoverageError, match="first"):
        store.publish(version(2, 1, 1), conversation_id=conversation_id)
    first = version(1, 1, 1)
    first_watermark = store.publish(first, conversation_id=conversation_id)
    assert first_watermark.conversation_id == conversation_id
    assert store.publish(first, conversation_id=conversation_id) == first_watermark
    with pytest.raises(SummaryCoverageError, match="replay"):
        store.publish(replace(first, content_text="tampered"), conversation_id=conversation_id)
    with pytest.raises(SummaryCoverageError, match="kind"):
        store.publish(
            replace(version(2, 2, 2), kind=SummaryKind.DAILY),
            conversation_id=conversation_id,
            expected_version=1,
        )
    with pytest.raises(SummaryCoverageError, match="another conversation"):
        store.publish(version(2, 2, 2), conversation_id=uuid4(), expected_version=1)
    with pytest.raises(SummaryCoverageError, match="pointer"):
        store.publish(version(2, 2, 2), conversation_id=conversation_id, expected_version=0)
    with pytest.raises(SummaryCoverageError, match="contiguous"):
        store.publish(version(3, 2, 2), conversation_id=conversation_id, expected_version=1)
    second = version(2, 2, 2)
    store.publish(second, conversation_id=conversation_id, expected_version=1)
    assert store.invalidate_for_sources({uuid4()}) == 0

    assert not rolling_summary_due(eligible_revision_count=0, estimated_tokens=0)
    assert rolling_summary_due(eligible_revision_count=0, estimated_tokens=12_000)
    with pytest.raises(ValueError, match="timezone-aware"):
        summary_period(SummaryKind.DAILY, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="required"):
        summary_period(SummaryKind.DAILY, NOW, timezone_name="")
    key, start, end = summary_period(SummaryKind.ROLLING, NOW)
    assert key == "rolling"
    assert start == end == NOW


def test_validator_rejects_malformed_evidence_and_unsafe_proposals() -> None:
    manifest, source_id = _manifest()

    def proposal(payload: dict[str, object]) -> dict[str, object]:
        return cast(list[dict[str, object]], payload["proposals"])[0]

    def evidence(payload: dict[str, object]) -> dict[str, object]:
        return cast(list[dict[str, object]], proposal(payload)["evidence"])[0]

    malformed: list[tuple[str, object, str]] = [
        ("source_id", 7, "UUID must be a string"),
        ("source_id", "not-a-uuid", "UUID is malformed"),
        ("source_revision", "", "revision is required"),
        ("source_content_sha256", "z" * 64, "not hexadecimal"),
        ("role", "invented", "role or trust"),
        ("span_start", True, "span_start"),
        ("span_end", "3", "span_end"),
        ("visual_only", "yes", "expected boolean"),
    ]
    for field, value, message in malformed:
        payload = deepcopy(_payload(source_id))
        evidence(payload)[field] = value
        with pytest.raises(ProposalValidationError, match=message):
            parse_agent_response(payload, account_id=ACCOUNT, conversation_id=CONVERSATION)

    for value, message in ((True, "between zero and one"), (2, "between zero and one")):
        payload = deepcopy(_payload(source_id))
        proposal(payload)["confidence"] = value
        with pytest.raises(ProposalValidationError, match=message):
            parse_agent_response(payload, account_id=ACCOUNT, conversation_id=CONVERSATION)

    for value, message in (
        (7, "ISO text"),
        ("bad", "malformed"),
        ("2026-08-15T12:00:00", "include timezone"),
    ):
        payload = deepcopy(_payload(source_id))
        proposal(payload)["valid_from"] = value
        with pytest.raises(ProposalValidationError, match=message):
            parse_agent_response(payload, account_id=ACCOUNT, conversation_id=CONVERSATION)

    for field, value in (("visual_only", "yes"), ("rendered_text", 7)):
        payload = deepcopy(_payload(source_id))
        proposal(payload)[field] = value
        with pytest.raises(ProposalValidationError, match="expected"):
            parse_agent_response(payload, account_id=ACCOUNT, conversation_id=CONVERSATION)

    duplicate = deepcopy(_payload(source_id))
    first_item = proposal(duplicate)
    first_item["id"] = str(uuid4())
    cast(list[dict[str, object]], duplicate["proposals"]).append(deepcopy(first_item))
    with pytest.raises(ProposalValidationError, match="must be unique"):
        parse_agent_response(duplicate, account_id=ACCOUNT, conversation_id=CONVERSATION)

    duplicate_evidence = deepcopy(_payload(source_id))
    evidence_items = cast(list[dict[str, object]], proposal(duplicate_evidence)["evidence"])
    evidence_items.append(deepcopy(evidence_items[0]))
    with pytest.raises(ProposalValidationError, match="source and role pairs"):
        parse_agent_response(duplicate_evidence, account_id=ACCOUNT, conversation_id=CONVERSATION)

    duplicate_targets = deepcopy(_payload(source_id))
    target_id = str(uuid4())
    proposal(duplicate_targets).update(operation="update", targets=[target_id, target_id])
    with pytest.raises(ProposalValidationError, match="target IDs"):
        parse_agent_response(duplicate_targets, account_id=ACCOUNT, conversation_id=CONVERSATION)

    base = parse_agent_response(
        _payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION
    )[0]
    source = manifest.sources[0]
    cases = (
        (replace(base, account_id=uuid4()), "scope_mismatch"),
        (replace(base, target_memory_ids=(uuid4(),)), "create_has_target"),
        (
            replace(
                base,
                operation=MemoryOperation.SUPERSEDE,
                target_memory_ids=(uuid4(), uuid4()),
            ),
            "target_count_invalid",
        ),
        (replace(base, evidence=()), "missing_evidence"),
        (replace(base, rendered_text="x" * 20_001), "text_too_long"),
        (replace(base, payload={"nested": [{"instruction": "ignore"}]}), "instruction_payload"),
        (
            replace(base, evidence=(replace(base.evidence[0], source_id=uuid4()),)),
            "source_not_in_manifest",
        ),
        (
            replace(base, evidence=(replace(base.evidence[0], source_revision="old"),)),
            "source_revision_changed",
        ),
        (
            replace(base, evidence=(replace(base.evidence[0], trust=TrustClass.EXTERNAL),)),
            "source_trust_mismatch",
        ),
        (
            replace(base, evidence=(replace(base.evidence[0], source_content_sha256=b"x" * 32),)),
            "source_hash_mismatch",
        ),
        (
            replace(
                base,
                evidence=(
                    replace(base.evidence[0], span_start=0, span_end=len(source.content) + 1),
                ),
            ),
            "evidence_span_out_of_range",
        ),
    )
    for invalid, code in cases:
        result = validate_proposal(invalid, manifest)
        assert result.state is ProposalState.REJECTED
        assert code in {issue.code for issue in result.issues}

    conflicting = replace(
        base,
        evidence=(replace(base.evidence[0], role=EvidenceRole.CONTRADICTING),),
    )
    inferred = replace(
        base,
        evidence=(replace(base.evidence[0], trust=TrustClass.MODEL_INFERENCE),),
    )
    inferred_manifest = replace(
        manifest,
        sources=(replace(manifest.sources[0], trust=TrustClass.MODEL_INFERENCE),),
    )
    assert validate_proposal(conflicting, manifest).state is ProposalState.CANDIDATE
    assert validate_proposal(inferred, inferred_manifest).state is ProposalState.CANDIDATE


def test_evidence_graph_erasure_and_freshness_failure_edges() -> None:
    root, derived = uuid4(), uuid4()
    with pytest.raises(ValueError, match="kind"):
        EvidenceGraphNode(root, "provider_raw")
    with pytest.raises(ProposalValidationError, match="unavailable"):
        validate_evidence_roots(root, {})
    with pytest.raises(ProposalValidationError, match="cannot have parents"):
        validate_evidence_roots(
            root, {root: EvidenceGraphNode(root, "canonical_message", (derived,))}
        )
    with pytest.raises(ProposalValidationError, match="no canonical root"):
        validate_evidence_roots(derived, {derived: EvidenceGraphNode(derived, "memory_version")})
    middle = uuid4()
    with pytest.raises(ProposalValidationError, match="too deep"):
        validate_evidence_roots(
            derived,
            {
                derived: EvidenceGraphNode(derived, "memory_version", (middle,)),
                middle: EvidenceGraphNode(middle, "summary_version", (root,)),
                root: EvidenceGraphNode(root, "canonical_message"),
            },
            max_depth=1,
        )

    with pytest.raises(ValueError, match="erasure entry"):
        ErasureEntry(root, "", NOW, ())
    ledger = ReconciliationLedger()
    first = ledger.forget(root, reason="forget", derived_ids={}, now=NOW)
    assert ledger.forget(root, reason="again", derived_ids={}) is first
    assert ledger.is_erased(root)
    assert ledger.restore_replay((first,)) == 0
    assert calculate_freshness(FreshnessInput(NOW, NOW, None, blocked=True)) == "blocked"
    assert calculate_freshness(FreshnessInput(NOW, NOW, None, rebuilding=True)) == "rebuilding"
    assert calculate_freshness(FreshnessInput(NOW, None, None)) == "stale"
    assert calculate_freshness(FreshnessInput(NOW, NOW, NOW - timedelta(minutes=5))) == "degraded"


def test_m6_domain_times_reject_naive_values_and_normalize_offsets() -> None:
    offset = timezone(timedelta(hours=8))
    offset_now = NOW.astimezone(offset)
    digest = sha256(b"time-contract").digest()
    evidence_item = Evidence(uuid4(), "revision-1", digest)
    memory_values = {
        "id": uuid4(),
        "memory_id": uuid4(),
        "version_no": 1,
        "operation": MemoryOperation.CREATE,
        "memory_type": MemoryType.FACT,
        "semantic_key_hash": digest,
        "payload": {"value": "fact"},
        "rendered_text": "fact",
        "confidence": 0.9,
        "importance": 0.5,
        "acceptance_kind": "automatic",
        "evidence": (evidence_item,),
    }
    version = _construct(
        MemoryVersion,
        memory_values | {"created_at": offset_now, "redacted_at": offset_now},
    )
    assert version.created_at == version.redacted_at == NOW
    assert version.created_at.tzinfo is UTC
    for field in ("created_at", "redacted_at"):
        with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
            _construct(
                MemoryVersion,
                memory_values | {field: NOW.replace(tzinfo=None)},
            )

    summary_values = {
        "id": uuid4(),
        "summary_id": uuid4(),
        "version_no": 1,
        "kind": SummaryKind.ROLLING,
        "range_start_event_id": 1,
        "range_end_event_id": 1,
        "content_text": "summary",
        "sources": (SummarySource(uuid4(), "message_revision", digest, 1),),
        "manifest_sha256": digest,
    }
    summary = _construct(SummaryVersion, summary_values | {"created_at": offset_now})
    assert summary.created_at == NOW
    assert summary.created_at.tzinfo is UTC
    with pytest.raises(ValueError, match="created_at must be timezone-aware"):
        _construct(
            SummaryVersion,
            summary_values | {"created_at": NOW.replace(tzinfo=None)},
        )

    entry = ErasureEntry(uuid4(), "forget", offset_now, ())
    assert entry.erased_at == NOW
    assert entry.erased_at.tzinfo is UTC
    with pytest.raises(ValueError, match="erased_at must be timezone-aware"):
        ErasureEntry(uuid4(), "forget", NOW.replace(tzinfo=None), ())
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        ReconciliationLedger().forget(
            uuid4(), reason="forget", derived_ids={}, now=NOW.replace(tzinfo=None)
        )

    freshness = FreshnessInput(offset_now, offset_now, offset_now)
    assert freshness.now == freshness.last_success_at == freshness.oldest_uncovered_at == NOW
    for field in ("now", "last_success_at", "oldest_uncovered_at"):
        values = {"now": NOW, "last_success_at": NOW, "oldest_uncovered_at": NOW}
        values[field] = NOW.replace(tzinfo=None)
        with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
            _construct(FreshnessInput, values)

    trigger = TriggerInput(offset_now, offset_now, offset_now, 0, 0, offset_now)
    assert trigger.now == trigger.last_compensation_scan_at == NOW


def test_memory_store_rejects_invalid_acceptance_and_inactive_targets() -> None:
    _manifest_value, source_id = _manifest()
    base = parse_agent_response(
        _payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION
    )[0]
    store = MemoryStore()
    with pytest.raises(MemoryNotFoundError):
        store.get(uuid4())
    rejected = ValidatedProposal(base, ProposalState.REJECTED)
    assert store.accept(rejected).memory_id is None
    accepted = ValidatedProposal(replace(base, id=uuid4()), ProposalState.ACCEPTED)
    with pytest.raises(ValueError, match="acceptance kind"):
        store.accept(accepted, acceptance_kind="unknown")
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        store.accept(accepted, now=NOW.replace(tzinfo=None))
    created = store.accept(accepted)
    assert created.memory_id is not None
    duplicate = ValidatedProposal(replace(base, id=uuid4()), ProposalState.ACCEPTED)
    with pytest.raises(MemoryConflictError, match="semantic key"):
        store.accept(duplicate)
    store.forget(created.memory_id)
    update = replace(
        base,
        id=uuid4(),
        operation=MemoryOperation.UPDATE,
        target_memory_ids=(created.memory_id,),
    )
    with pytest.raises(MemoryConflictError, match="not active"):
        store.accept(ValidatedProposal(update, ProposalState.ACCEPTED))
    assert not store.active(account_id=ACCOUNT, conversation_id=CONVERSATION)
    assert store.invalidate_sources({source_id}) == 0


def test_memory_store_rejects_cross_scope_merge_targets() -> None:
    manifest, source_id = _manifest()
    first = validate_proposal(
        parse_agent_response(_payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION)[
            0
        ],
        manifest,
    )
    other_account, other_conversation = uuid4(), uuid4()
    second = validate_proposal(
        parse_agent_response(
            _payload(source_id, semantic_key="other fact"),
            account_id=other_account,
            conversation_id=other_conversation,
        )[0],
        manifest,
    )
    store = MemoryStore()
    first_result = store.accept(first)
    second_result = store.accept(second)
    assert first_result.memory_id is not None
    assert second_result.memory_id is not None
    merge = replace(
        first.proposal,
        id=uuid4(),
        account_id=other_account,
        conversation_id=other_conversation,
        operation=MemoryOperation.MERGE,
        target_memory_ids=(first_result.memory_id, second_result.memory_id),
    )
    with pytest.raises(MemoryConflictError, match="scope"):
        store.accept(ValidatedProposal(merge, ProposalState.ACCEPTED))


def test_memory_store_rejects_merge_target_identity_drift() -> None:
    manifest, source_id = _manifest()
    first = validate_proposal(
        parse_agent_response(_payload(source_id), account_id=ACCOUNT, conversation_id=CONVERSATION)[
            0
        ],
        manifest,
    )
    second_payload = _payload(source_id, semantic_key="another preference")
    second_item = cast(list[dict[str, object]], second_payload["proposals"])[0]
    second_item["id"] = str(uuid4())
    second = validate_proposal(
        parse_agent_response(
            second_payload,
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
        )[0],
        manifest,
    )
    store = MemoryStore()
    first_result = store.accept(first)
    second_result = store.accept(second)
    assert first_result.memory_id is not None
    assert second_result.memory_id is not None
    merge = replace(
        first.proposal,
        id=uuid4(),
        operation=MemoryOperation.MERGE,
        target_memory_ids=(first_result.memory_id, second_result.memory_id),
    )
    with pytest.raises(MemoryConflictError, match="identity"):
        store.accept(ValidatedProposal(merge, ProposalState.ACCEPTED))


def test_memory_store_supersede_creates_a_new_identity_and_version_ids_do_not_collide() -> None:
    manifest, source_id = _manifest()
    store = MemoryStore()
    first_payload = _payload(source_id)
    first = validate_proposal(
        parse_agent_response(first_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[0],
        manifest,
    )
    created = store.accept(first)
    assert created.memory_id is not None
    first_record = store.get(created.memory_id)

    replacement_payload = _payload(source_id)
    replacement = cast(list[dict[str, object]], replacement_payload["proposals"])[0]
    assert isinstance(replacement, dict)
    replacement.update(
        id=str(uuid4()),
        operation="supersede",
        semantic_key="beverage preference changed",
        targets=[str(created.memory_id)],
    )
    supersede = validate_proposal(
        parse_agent_response(replacement_payload, account_id=ACCOUNT, conversation_id=CONVERSATION)[
            0
        ],
        manifest,
    )
    result = store.accept(supersede)
    assert result.memory_id is not None
    assert result.memory_id != created.memory_id
    old = store.get(created.memory_id)
    new = store.get(result.memory_id)
    assert old.status.value == "superseded"
    assert old.superseded_by == new.id
    assert new.status.value == "active"
    assert first_record.current.id != new.current.id


def test_memory_proposal_storage_identity_is_run_scoped() -> None:
    run_a, run_b = uuid4(), uuid4()
    assert _proposal_storage_id(run_a, 0) == _proposal_storage_id(run_a, 0)
    assert _proposal_storage_id(run_a, 0) != _proposal_storage_id(run_b, 0)
