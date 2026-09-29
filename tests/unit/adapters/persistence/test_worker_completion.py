"""Current scope, evidence, projection and embedding switch safety branches."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_userbot.adapters.persistence import embedding_rebuild as rebuild
from telegram_userbot.adapters.persistence import memory_pipeline as backlog
from telegram_userbot.adapters.persistence import proactive_delivery as delivery
from telegram_userbot.adapters.persistence import proactive_runtime as runtime
from telegram_userbot.adapters.persistence.proactive_delivery import expire_proactive_targets
from telegram_userbot.domain.proactive.models import TypedEvidence
from tests.unit.domain.test_m7_proactive import NOW
from tests.unit.processes.test_memory_pipeline import job_row, source_row
from tests.unit.processes.test_proactive_pipeline import SECRET, result, sample

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mutation", [None, "control", "deleted", "erased", "policy", "setting"])
async def test_current_scope_locks_and_resolves_account_contact_controls(
    mutation: str | None,
) -> None:
    candidate, scope, _, _ = sample()
    account = {**scope.account, "status": "active", "deleted_at": None, "default_timezone": "UTC"}
    conversation = {
        **scope.conversation,
        "contact_id": candidate.contact_id,
        "deleted_at": None,
        "base_mode_override": None,
        "contact_paused": False,
        "temporary_human_until": None,
        "mode_version": candidate.mode_version,
        "content_revision": candidate.content_revision,
    }
    contact = {
        **scope.contact,
        "deleted_at": NOW if mutation == "deleted" else None,
        "proactive_enabled": True,
        "timezone": None,
        "automation_status": "allowed",
    }
    control = (
        None
        if mutation == "control"
        else {
            "default_base_mode": "AUTO",
            "global_paused": False,
            "maintenance_state": "inactive",
            "control_version": 1,
        }
    )
    settings = (
        {
            "version_no": 2,
            "enabled": True,
            "daily_limit": 2,
            "minimum_interval_seconds": 3600,
            "relationship_level": "friend",
            "timezone_name": "UTC",
        }
        if mutation == "setting"
        else None
    )
    values = [
        account,
        control,
        conversation,
        contact,
        None if mutation == "policy" else scope.policy_row,
        settings,
        (11, NOW - timedelta(hours=1)),
    ]
    session = AsyncMock(
        execute=AsyncMock(side_effect=[result(value) for value in values]),
        scalar=AsyncMock(
            side_effect=[
                candidate.account_id,
                uuid4() if mutation == "erased" else None,
                False,
                None,
            ]
        ),
    )
    repo = runtime.ProactiveRuntimeRepository(session)
    if mutation in {"control", "deleted", "erased", "policy"}:
        with pytest.raises(runtime.ProactiveRuntimeError, match="UNAVAILABLE"):
            await repo.scope(candidate.conversation_id, now=NOW)
    else:
        value = await repo.scope(
            candidate.conversation_id, now=NOW, own_turn=uuid4(), own_decision=uuid4()
        )
        assert value.resolution.permits_auto
        assert value.activity_revision == 11
        assert value.settings.version == (2 if mutation == "setting" else 1)
        assert "decision_id !=" in str(session.scalar.call_args.args[0])


@pytest.mark.parametrize(
    "mutation", [None, "membership", "fact", "stale", "hash", "source", "invalid"]
)
async def test_candidate_requires_current_fact_and_canonical_membership(
    mutation: str | None,
) -> None:
    candidate, _, _, _ = sample()
    occurrence = candidate.occurrences[0]
    evidence = TypedEvidence("message_revision", uuid4(), "revision-1", b"h" * 32, "synthetic")
    occurrence = replace(occurrence, evidence=(evidence,))
    row = asdict(candidate)
    item = asdict(occurrence)
    item.update(
        state="eligible", member_key=item["occurrence_key"], member_generation=item["generation"]
    )
    if mutation == "membership":
        row["membership_hash"] = b"z" * 32
    if mutation == "stale":
        item["member_generation"] = 99
    if mutation == "invalid":
        item["state"] = "invalidated"
    revision = {
        "revision_no": 1,
        "content_sha256": b"x" * 32 if mutation == "hash" else b"h" * 32,
        "message_source": "ai" if mutation == "source" else "human",
    }
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(row),
                result(rows=[item]),
                result(rows=[asdict(evidence)]),
                result(revision),
            ]
        ),
        scalar=AsyncMock(return_value=None if mutation == "fact" else occurrence.source_id),
    )
    repo = runtime.ProactiveRuntimeRepository(session)
    if mutation:
        with pytest.raises(runtime.ProactiveRuntimeError, match="CHANGED"):
            await repo.candidate(candidate.id, now=NOW)
    else:
        assert (await repo.candidate(candidate.id, now=NOW)).occurrences[0].evidence == (evidence,)
    assert not await repo.evidence_current(
        replace(evidence, source_type="rule"),
        account=candidate.account_id,
        conversation=candidate.conversation_id,
    )


@pytest.mark.parametrize(
    "kind",
    ["intention", "event", "explicit", "relationship", "badtime", "staleroot", "empty", "replay"],
)
async def test_materialization_accepts_only_canonical_typed_facts(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _, scope, _, _ = sample()
    fact = {
        "id": uuid4(),
        "version_id": uuid4(),
        "current_version_no": 1,
        "memory_type": "event"
        if kind == "event"
        else "relationship"
        if kind == "relationship"
        else "intention",
        "payload": {
            "expected_at": "bad"
            if kind == "badtime"
            else (NOW if kind == "explicit" else NOW + timedelta(minutes=5)).isoformat(),
            "owner": "self",
            "start_at": (NOW + timedelta(hours=3)).isoformat(),
            "explicit_followup": kind == "explicit",
        },
        "importance": 0.95,
        "rendered_text": "synthetic formal fact",
    }
    source = uuid4()
    root = {"message_revision_id": source, "source_content_sha256": b"h" * 32}
    revision = {
        "id": source,
        "revision_no": 1,
        "message_source": "human",
        "content_sha256": b"x" * 32 if kind == "staleroot" else b"h" * 32,
    }
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[fact]),
                result(rows=[] if kind == "empty" else [root]),
                result(revision),
            ]
        ),
        scalar=AsyncMock(return_value=uuid4() if kind == "replay" else None),
    )
    queued = AsyncMock()
    monkeypatch.setattr(runtime, "ProactiveRepository", lambda _: queued)
    count = await runtime.ProactiveRuntimeRepository(session).materialize(
        scope, secret=SECRET, now=NOW
    )
    if kind in {"badtime", "staleroot", "empty", "replay", "relationship"}:
        assert count == 0
    else:
        assert count >= 1
        published = queued.enqueue_candidate.call_args.args[0]
        assert published.occurrences[0].source_id == fact["id"]
        assert published.occurrences[0].evidence[0].source_id == source


@pytest.mark.parametrize(
    "settings",
    [
        {"allowed_reasons": ["promise_due"], "friend_min_interval": 3600},
        {"version_no": 2},
        {"unknown": 1},
    ],
)
def test_policy_options_are_typed_and_cannot_override_identity(settings: dict[str, Any]) -> None:
    _, scope, _, _ = sample()
    row = {**scope.policy_row, "settings_json": settings}
    if "version_no" in settings or "unknown" in settings:
        with pytest.raises(runtime.ProactiveRuntimeError, match="POLICY_INVALID"):
            runtime.policy_value(row)
    else:
        assert runtime.policy_value(row).friend_min_interval == timedelta(hours=1)
    assert runtime.timestamp(None) is None
    assert runtime.timestamp(NOW.isoformat()) == NOW


@pytest.mark.parametrize("phase", ["missing", "pending", "failed", "changed_config", "complete"])
async def test_shadow_switch_waits_for_complete_current_coverage(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    account, identity, config = uuid4(), uuid4(), uuid4()
    space = {
        "account_id": account,
        "id": identity,
        "dimensions": 2,
        "model_profile_id": uuid4(),
        "config_version_id": config,
    }
    missing = [("message_revision", uuid4())] if phase == "missing" else []
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[space]),
                result(),
                result(space),
                result(rows=missing),
                result(),
                result(),
            ]
        ),
        scalar=AsyncMock(
            side_effect=[phase in {"pending", "failed"}, phase == "failed"]
            if phase in {"pending", "failed"}
            else [False, uuid4() if phase == "changed_config" else config]
        ),
    )
    staging = AsyncMock(stage_target=AsyncMock(return_value=1))
    monkeypatch.setattr(rebuild, "EmbeddingRuntimeRepository", lambda _: staging)
    count = await rebuild.EmbeddingRebuildRepository(session).advance(now=NOW)
    assert count == int(phase in {"missing", "complete"})
    updates = [
        call.args[0].compile().params
        for call in session.execute.call_args_list
        if str(call.args[0]).startswith("UPDATE")
    ]
    assert [value["state"] for value in updates] == (
        ["retired", "active"] if phase == "complete" else ["failed"] if phase == "failed" else []
    )
    if phase == "missing":
        assert staging.stage_target.call_args.kwargs["space_id"] == identity
    with pytest.raises(ValueError, match="batch invalid"):
        await rebuild.EmbeddingRebuildRepository(session).advance(now=NOW, limit=0)


@pytest.mark.parametrize("dimensions", [None, 2, 3])
async def test_active_embedding_config_gets_one_resumable_space(dimensions: int | None) -> None:
    config = {
        "id": uuid4(),
        "profile_id": uuid4(),
        "protocol_options": {} if dimensions is None else {"dimensions": dimensions},
        "embedding_dimensions": [2],
        "model_name": "synthetic",
    }
    session = AsyncMock(
        execute=AsyncMock(side_effect=[result(rows=[config]), result(rows=[uuid4()])]),
        scalar=AsyncMock(side_effect=[None, 0, uuid4()]),
    )
    assert await rebuild.EmbeddingRebuildRepository(session).ensure_spaces(now=NOW) == int(
        dimensions != 3
    )
    if dimensions != 3:
        assert session.scalar.call_args.args[0].compile().params["state"] == "building"
    session.scalar = AsyncMock(return_value=uuid4())
    assert await rebuild.EmbeddingRebuildRepository(session).request_rollback(
        account_id=uuid4(), space_id=uuid4()
    )
    assert "retired" in session.scalar.call_args.args[0].compile().params.values()


@pytest.mark.parametrize("target", ["auto", "copilot", "unbound"])
async def test_expiration_cancels_only_targets_without_side_effects(target: str) -> None:

    group, draft, turn = uuid4(), uuid4(), uuid4()
    row = {
        "outbound_group_id": group if target == "auto" else None,
        "copilot_draft_id": draft if target == "copilot" else None,
    }
    session = AsyncMock(
        execute=AsyncMock(return_value=result(rows=[row])), scalar=AsyncMock(return_value=turn)
    )
    assert await expire_proactive_targets(session, now=NOW) == int(target != "unbound")
    queries = [
        str(item.args[0]) for item in session.execute.call_args_list + session.scalar.call_args_list
    ]
    if target == "auto":
        assert any("first_side_effect_at IS NULL" in query for query in queries)
        assert any("UPDATE outbound_intents" in query for query in queries)
    if target != "unbound":
        assert any("UPDATE conversation_turns" in query for query in queries)


@pytest.mark.parametrize("state", ["held", "send_unknown", "released", "expired"])
async def test_delivery_rechecks_canonical_facts_without_double_charging(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:

    candidate, scope, _, accepted = sample()
    row = {"id": accepted[0]["id"], "state": "accepted", "candidate_id": candidate.id}
    hold = {"state": state, "expires_at": NOW + timedelta(minutes=5)}
    session = AsyncMock(execute=AsyncMock(side_effect=[result(row), result(hold)]))
    facts = AsyncMock(
        candidate=AsyncMock(return_value=candidate), scope=AsyncMock(return_value=scope)
    )
    decisions = AsyncMock(decision=AsyncMock(return_value=accepted))
    monkeypatch.setattr(delivery, "ProactiveRuntimeRepository", lambda _: facts)
    monkeypatch.setattr(delivery, "ProactiveGenerationRepository", lambda _: decisions)
    assert await delivery.authorize_proactive_delivery(
        session, decision_id=row["id"], turn_id=uuid4(), control_version=1, now=NOW
    ) == (state in {"held", "send_unknown"})
    assert facts.scope.call_args.kwargs["own_decision"] == row["id"]
    assert all(str(item.args[0]).startswith("SELECT") for item in session.execute.call_args_list)


@pytest.mark.parametrize("size", [4, 40])
async def test_backlog_partition_preserves_first_unprocessed_event(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:

    row = {**job_row(), "range_end_event_id": size}
    roots = [{**source_row(), "source_event_id": index + 1} for index in range(min(size, 33))]
    updated = {**row, "range_end_event_id": 10}
    session = AsyncMock(execute=AsyncMock(side_effect=[result(rows=roots), result(updated)]))
    pending = AsyncMock()
    monkeypatch.setattr(backlog, "MemoryRepository", lambda _: pending)
    actual = await backlog.MemoryPipelineRepository(session)._partition_backlog(row, now=NOW)
    if size == 4:
        assert actual == row
        pending.refresh_pending_job.assert_not_awaited()
    else:
        cutoff = session.execute.call_args.args[0].compile().params["range_end_event_id"]
        assert 1 <= cutoff < size
        remaining = pending.refresh_pending_job.call_args.kwargs["event_range"]
        assert remaining.start == cutoff + 1
        assert remaining.end == size
