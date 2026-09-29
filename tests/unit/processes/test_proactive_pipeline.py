"""Production proactive control flow with deterministic SQL and provider boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from tests.integration.test_m8_worker_complete import Transport
from tests.unit.domain.test_m7_proactive import NOW, candidate_for, policy
from tests.unit.processes.test_embedding_runtime import Resolver
from tests.unit.processes.test_memory_pipeline import prepared as memory_prepared

from telegram_userbot.adapters.persistence import proactive_generation as generation
from telegram_userbot.adapters.persistence import proactive_runtime as current
from telegram_userbot.adapters.persistence.proactive_generation import PreparedProactive
from telegram_userbot.domain.conversation.mode import (
    AccountControl,
    BaseMode,
    ConversationControl,
    MaintenanceState,
    resolve_mode,
)
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.proactive.jobs import DueJob, DueJobState
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    ContactSettings,
    ProactiveAction,
    membership_digest,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding
from telegram_userbot.platform.health.disk import disk_admission
from telegram_userbot.processes import proactive_pipeline as pipeline

pytestmark = pytest.mark.unit
SECRET = b"s" * 32


def result(value: Any = None, rows: Any = ()) -> Any:
    item = MagicMock()
    item.mappings.return_value = item
    item.scalars.return_value = item
    item.one.return_value = value
    item.one_or_none.return_value = value
    item.all.return_value = list(rows)
    item.__iter__.return_value = iter(rows)
    return item


def sample(mode: BaseMode = BaseMode.AUTO) -> tuple[Any, current.ProactiveScope, DueJob, Any]:
    active_policy = policy()
    candidate = candidate_for(active_policy=active_policy)
    occurrence = replace(
        candidate.occurrences[0],
        source_type="intention",
        source_version="1",
        contact_setting_version=1,
    )
    candidate = replace(
        candidate,
        occurrences=(occurrence,),
        membership_hash=membership_digest((occurrence,)),
        contact_setting_version=1,
    )
    policy_row = {
        "id": active_policy.version_id,
        "version_no": 1,
        "enabled": True,
        "timezone_name": "UTC",
        "quiet_start_local": "23:00",
        "quiet_end_local": "08:00",
        "account_daily_limit": 10,
        "contact_bypass_daily_limit": 1,
        "activity_suppression_seconds": 900,
        "settings_json": {},
    }
    scope = current.ProactiveScope(
        {"id": candidate.account_id},
        {"id": candidate.conversation_id},
        {"id": candidate.contact_id},
        policy_row,
        active_policy,
        ContactSettings(candidate.contact_id, version=1, enabled=True, timezone_name="UTC"),
        resolve_mode(
            account=AccountControl(mode, False, MaintenanceState.INACTIVE, 1),
            conversation=ConversationControl(
                None, False, None, candidate.mode_version, candidate.content_revision
            ),
            now=NOW,
        ),
        NOW - timedelta(hours=1),
        candidate.activity_revision,
        None,
        False,
    )
    job = DueJob(
        uuid4(),
        candidate.account_id,
        b"j" * 32,
        NOW,
        NOW + timedelta(hours=1),
        DueJobState.LEASED,
        uuid4(),
        NOW + timedelta(minutes=2),
        1,
        1,
    )
    decision = AgentDecision(
        candidate.id, ProactiveAction.SEND_NOW, "timely_support", (occurrence.id,), "Check in", 0.5
    )
    return candidate, scope, job, ({"id": uuid4()}, decision)


def prepared(
    purpose: str = "proactive_decision", *, mode: BaseMode = BaseMode.AUTO
) -> tuple[PreparedProactive, current.ProactiveScope, Any, Any]:
    candidate, scope, job, accepted = sample(mode)
    old, keyring = memory_prepared()
    role = LogicalRole.MAIN_AI if purpose == "proactive_final" else LogicalRole.PROACTIVE_AGENT
    binding = CredentialBinding(
        role, old.model.config.profile_id, old.model.config.credential_id, 1
    )
    model = replace(
        old.model,
        config=replace(old.model.config, logical_role=role),
        binding=binding,
        envelope=keyring.encrypt(SensitiveValue("synthetic-provider-key"), binding=binding),
    )
    body = json.dumps(
        {"purpose": purpose, "occurrences": [{"id": str(candidate.occurrences[0].id)}]}
    )
    value = PreparedProactive(
        job,
        candidate,
        model,
        uuid4(),
        purpose,
        b"f" * 32,
        SensitiveValue(body),
        1,
        accepted[0]["id"] if purpose == "proactive_final" else None,
    )
    return value, scope, accepted, keyring


@pytest.mark.parametrize("mutation", [None, "state", "owner", "token", "expiry", "missing"])
async def test_fence_rejects_late_or_replaced_workers(mutation: str | None) -> None:
    value, _, _, _ = prepared()
    row = {**asdict(value.job), "state": "leased", "candidate_id": value.candidate.id}
    if mutation == "state":
        row["state"] = "succeeded"
    if mutation == "owner":
        row["lease_owner"] = uuid4()
    if mutation == "token":
        row["fencing_token"] = 2
    if mutation == "expiry":
        row["lease_expires_at"] = NOW
    session = AsyncMock(
        execute=AsyncMock(return_value=result(None if mutation == "missing" else row))
    )
    repo: Any = generation.ProactiveGenerationRepository(session)
    if mutation:
        with pytest.raises(current.ProactiveRuntimeError, match="LEASE_LOST"):
            await repo.fence(value.job, now=NOW)
    else:
        assert await repo.fence(value.job, now=NOW) == row


@pytest.mark.parametrize("purpose", ["proactive_decision", "proactive_final"])
async def test_sealing_replay_pins_time_credentials_and_detects_input_change(
    monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    value, scope, accepted, _ = prepared(purpose)
    stored: dict[str, Any] = {}
    statements = []

    async def execute(statement: Any) -> Any:
        statements.append(statement)
        sql = str(statement)
        if sql.startswith("SELECT"):
            if "FROM proactive_input_manifests" in sql:
                return result(stored or None)
            if "FROM prompt_versions" in sql:
                return result({"id": UUID(int=99), "version_no": 1})
            if "FROM model_runs" in sql:
                return result(
                    {
                        "account_control_version_snapshot": 1,
                        "adapter_version": generation.ADAPTER_VERSION,
                    }
                )
        if sql.startswith("INSERT INTO proactive_input_manifests"):
            stored.update(statement.compile().params)
        if sql.startswith("UPDATE proactive_input_manifests"):
            stored.update(statement.compile().params)
        return result()

    session = AsyncMock(execute=AsyncMock(side_effect=execute))
    repo: Any = generation.ProactiveGenerationRepository(session)
    repo.fence = AsyncMock(return_value={"candidate_id": value.candidate.id})
    repo.runtime = AsyncMock(
        candidate=AsyncMock(return_value=value.candidate), scope=AsyncMock(return_value=scope)
    )
    repo.decision = AsyncMock(return_value=accepted if purpose == "proactive_final" else None)
    loader = AsyncMock(return_value=value.model)
    monkeypatch.setattr(generation, "load_memory_model", loader)
    first = await repo.prepare(value.job, purpose=purpose, secret=SECRET, now=NOW)
    stored["scope_erased_at"] = None
    replay = await repo.prepare(
        value.job, purpose=purpose, secret=SECRET, now=NOW + timedelta(seconds=10)
    )
    assert replay.input_fingerprint == first.input_fingerprint
    assert loader.call_args.kwargs["config_id"] == value.model.config_id
    assert loader.call_args.kwargs["credential_version_id"] == value.model.credential_version_id
    assert (
        sum(str(item).startswith("INSERT INTO proactive_input_manifests") for item in statements)
        == 1
    )
    repo.runtime.scope.return_value = replace(
        scope, policy_row={**scope.policy_row, "account_daily_limit": 9}
    )
    with pytest.raises(current.ProactiveRuntimeError, match="INPUT_CHANGED"):
        await repo.prepare(value.job, purpose=purpose, secret=SECRET, now=NOW)
    repo.runtime.scope.return_value = scope
    stored["scope_erased_at"] = NOW
    with pytest.raises(current.ProactiveRuntimeError, match="SCOPE_ERASED"):
        await repo.prepare(value.job, purpose=purpose, secret=SECRET, now=NOW)


@pytest.mark.parametrize("mode", [BaseMode.AUTO, BaseMode.COPILOT])
async def test_publication_requires_live_hold_and_binds_one_target(
    monkeypatch: pytest.MonkeyPatch, mode: BaseMode
) -> None:
    value, scope, accepted, _ = prepared("proactive_final", mode=mode)
    hold = {
        "state": "held",
        "expires_at": NOW + timedelta(minutes=5),
        "outbound_group_id": None,
        "copilot_draft_id": None,
    }
    session = AsyncMock(
        execute=AsyncMock(return_value=result(hold)), scalar=AsyncMock(return_value=2)
    )
    repo: Any = generation.ProactiveGenerationRepository(session)
    repo.verify = AsyncMock(return_value=scope)
    repo.decision = AsyncMock(return_value=accepted)
    budgets = AsyncMock()
    delivery = AsyncMock()
    monkeypatch.setattr(generation, "ProactiveRepository", lambda _: budgets)
    monkeypatch.setattr(generation, "TelegramLifecycleRepository", lambda _: delivery)
    identity = await repo.publish(value, content="A check in", secret=SECRET, now=NOW)
    assert budgets.bind_budget_target.call_args.kwargs["target_id"] == identity
    assert delivery.create_delivery_group.await_count == int(mode is BaseMode.AUTO)
    params = [item.args[0].compile().params for item in session.execute.call_args_list]
    assert any(row.get("delivery_turn_id") is not None for row in params)
    assert any(row.get("trigger_kind") == "proactive" for row in params)
    hold["expires_at"] = NOW
    with pytest.raises(current.ProactiveRuntimeError, match="RESERVATION_UNAVAILABLE"):
        await repo.publish(value, content="A check in", secret=SECRET, now=NOW)
    await repo.finish_run(
        value, raw="private result", secret=SECRET, now=NOW, input_tokens=5, output_tokens=6
    )
    assert "private result" not in str(session.execute.call_args_list[-2:])


@pytest.mark.parametrize("state", ["held", "expired", "send_unknown"])
async def test_reservation_reuse_keeps_original_deadline(state: str) -> None:
    value, scope, accepted, _ = prepared("proactive_final")
    hold = {
        "state": state,
        "expires_at": NOW + timedelta(minutes=3),
        "target": "auto_send",
        "account_local_date": NOW.date(),
        "contact_local_date": NOW.date(),
    }
    session = AsyncMock(execute=AsyncMock(return_value=result(hold)))
    repo: Any = generation.ProactiveGenerationRepository(session)
    repo.runtime.scope = AsyncMock(return_value=scope)
    repo.decision = AsyncMock(return_value=accepted)
    assert await repo.reserve(value, now=NOW) == (state == "held")
    assert session.execute.await_count == 1


def session_factory() -> tuple[Any, Any]:
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    context = AsyncMock()
    context.__aenter__.return_value = session
    return MagicMock(return_value=context), session


@pytest.mark.parametrize("action", ["send_now", "none", "defer_once", "existing"])
async def test_pipeline_runs_provider_and_persists_only_validated_results(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    value, scope, accepted, keyring = prepared()
    final = replace(
        value,
        purpose="proactive_final",
        run_id=uuid4(),
        decision_id=accepted[0]["id"],
        user_input=SensitiveValue(json.dumps({"purpose": "proactive_final", "occurrences": []})),
    )
    sessions, _ = session_factory()
    transport = Transport(action="send_now" if action == "existing" else action)
    # This transport's defer timestamp is tied to integration NOW, so use a local callback response.
    if action == "defer_once":
        original = transport.send

        async def defer(request: Any) -> Any:
            response = await original(request)
            body = response.body.reveal_for_use()
            text = json.loads(body["choices"][0]["message"]["content"])
            text["defer_until"] = (NOW + timedelta(minutes=1)).isoformat()
            body["choices"][0]["message"]["content"] = json.dumps(text)
            return replace(response, body=SensitiveValue(body))

        monkeypatch.setattr(transport, "send", defer)
    generation_repo = AsyncMock()
    generation_repo.fence.return_value = {
        "job_kind": "candidate_due",
        "candidate_id": value.candidate.id,
    }
    generation_repo.decision.return_value = accepted if action == "existing" else None
    generation_repo.prepare.side_effect = lambda *_, **kw: (
        value if kw["purpose"] == "proactive_decision" else final
    )
    generation_repo.verify.return_value = scope
    generation_repo.reserve.return_value = True
    lifecycle = AsyncMock()
    monkeypatch.setattr(pipeline, "ProactiveGenerationRepository", lambda _: generation_repo)
    monkeypatch.setattr(pipeline, "ProactiveRepository", lambda _: lifecycle)
    runtime: Any = pipeline.ProactivePublisher(
        sessions=sessions,
        keyring=keyring,
        secret=SensitiveValue(SECRET),
        admission=lambda: disk_admission(total_bytes=1000 * 1024**3, available_bytes=900 * 1024**3),
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: NOW,
    )
    await runtime.execute(value.job, asyncio.Event())
    assert generation_repo.publish.await_count == int(action == "send_now")
    assert lifecycle.record_decision.await_count == int(action != "existing")
    assert generation_repo.finish_run.await_count == (
        2 if action == "send_now" else 0 if action == "existing" else 1
    )


@pytest.mark.parametrize("failure", [None, "retry", "terminal", "cancel"])
async def test_scheduler_always_reaps_and_fences_failure_state(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    value, _, _, keyring = prepared()
    sessions, session = session_factory()
    lifecycle = AsyncMock(
        recover_expired=AsyncMock(return_value=1),
        reap_budget=AsyncMock(return_value=2),
        claim_next=AsyncMock(return_value=value.job),
    )
    scanner = AsyncMock(scan=AsyncMock(return_value=(3, None)))
    repository = AsyncMock(fence=AsyncMock(return_value={"candidate_id": value.candidate.id}))
    monkeypatch.setattr(pipeline, "ProactiveRepository", lambda _: lifecycle)
    monkeypatch.setattr(pipeline, "ProactiveRuntimeRepository", lambda _: scanner)
    monkeypatch.setattr(pipeline, "ProactiveGenerationRepository", lambda _: repository)
    expiration = AsyncMock(return_value=4)
    monkeypatch.setattr(pipeline, "expire_proactive_targets", expiration)
    runtime: Any = pipeline.ProactivePublisher(
        sessions=sessions,
        keyring=keyring,
        secret=SensitiveValue(SECRET),
        admission=lambda: disk_admission(total_bytes=1000 * 1024**3, available_bytes=900 * 1024**3),
        now=lambda: NOW,
    )
    error = (
        None
        if failure is None
        else TimeoutError()
        if failure == "retry"
        else asyncio.CancelledError()
        if failure == "cancel"
        else RuntimeError("private response")
    )
    runtime.execute = AsyncMock(return_value=False, side_effect=error)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runtime.publish(now=NOW)
        lifecycle.complete.assert_not_awaited()
    else:
        assert await runtime.publish(now=NOW) == (11 if failure is None else 10)
        if failure:
            assert lifecycle.complete.call_args.kwargs["succeeded"] == (failure == "terminal")
            assert "private response" not in str(session.execute.call_args_list)
    runtime.admission = lambda: disk_admission(total_bytes=1000 * 1024**3, available_bytes=0)
    assert await runtime.publish(now=NOW) == 7
    assert expiration.await_count == 2
