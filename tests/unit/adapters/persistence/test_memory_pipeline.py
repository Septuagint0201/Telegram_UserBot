"""Closed input admission and fencing branches, with content-free diagnostics."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, fields, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence import embedding_runtime as embedding
from telegram_userbot.adapters.persistence import memory_inputs as inputs
from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError
from telegram_userbot.adapters.persistence.memory_pipeline import (
    ADAPTER_VERSION,
    MemoryPipelineRepository,
    memory_background_id,
)
from telegram_userbot.adapters.persistence.model_runtime import model_config_digest
from tests.unit.adapters.persistence.test_model_snapshots import _row as capability_row
from tests.unit.processes.test_memory_pipeline import NOW, SECRET, job_row, prepared, source_row

pytestmark = pytest.mark.unit


def result(value: Any = None, *, rows: list[Any] | None = None) -> Any:
    item = MagicMock()
    item.mappings.return_value = item
    item.one_or_none.return_value = value
    item.one.return_value = value
    item.all.return_value = rows or []
    return item


def model_rows() -> list[Any]:
    value, _ = prepared()
    model = value.model
    config = model.config
    row = {
        **{item.name: getattr(config, item.name) for item in fields(config)},
        "id": model.config_id,
        "model_profile_id": config.profile_id,
        "logical_role": config.logical_role.value,
        "protocol": config.protocol.value,
        "config_sha256": model_config_digest(config),
        "capability_snapshot_id": UUID(int=70),
        "credential_version_no": 1,
    }
    cap = {
        **capability_row(),
        "status": "valid",
        "endpoint_id": config.endpoint_id,
        "protocol": config.protocol.value,
        "model_name": config.model_name,
        "supports_text": True,
        "supported_input_roles": ["system", "user"],
        "observed_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
        "chat_token_limit_field": "max_tokens",
    }
    meta = SimpleNamespace(id=model.credential_version_id, version_no=1)
    envelope = {**asdict(model.envelope), "credential_id": config.credential_id}
    prompt = {
        "template_body": "synthetic prompt",
        "template_sha256": hashlib.sha256(b"synthetic prompt").digest(),
    }
    return [row, cap, meta, envelope, prompt, asdict(model.endpoint)]


@pytest.mark.parametrize("sealed", [False, True])
async def test_model_snapshot_follows_exact_credential_and_config(sealed: bool) -> None:
    values = model_rows()
    if sealed:
        values[1]["expires_at"] = NOW - timedelta(minutes=1)
    session = AsyncMock(execute=AsyncMock(side_effect=[result(value) for value in values]))
    snapshot = await inputs.load_memory_model(
        session,
        prompt_version="prompt-v1",
        now=NOW,
        config_id=values[0]["id"] if sealed else None,
        credential_version_id=values[2].id if sealed else None,
    )
    assert snapshot.config_id == values[0]["id"]
    assert snapshot.credential_version_id == values[2].id
    assert snapshot.prompt.reveal_for_use() == "synthetic prompt"
    assert "synthetic prompt" not in repr(snapshot)
    assert "ciphertext" not in repr(snapshot)
    metadata = session.execute.call_args_list[2].args[0].compile().params
    assert (values[2].id in metadata.values()) == sealed


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_config", "MEMORY_MODEL_UNAVAILABLE"),
        ("config_hash", "MEMORY_CONFIG_INVALID"),
        ("missing_capability", "MEMORY_CAPABILITY_INVALID"),
        ("expired", "MEMORY_CAPABILITY_INVALID"),
        ("model", "MEMORY_CAPABILITY_INVALID"),
        ("roles", "MEMORY_CAPABILITY_INVALID"),
        ("text", "MEMORY_CAPABILITY_INVALID"),
        ("metadata", "MEMORY_CREDENTIAL_UNAVAILABLE"),
        ("credential", "MEMORY_CREDENTIAL_UNAVAILABLE"),
        ("binding", "MEMORY_CREDENTIAL_UNAVAILABLE"),
        ("prompt_version", "MEMORY_PROMPT_UNSUPPORTED"),
        ("prompt", "MEMORY_PROMPT_INVALID"),
        ("prompt_hash", "MEMORY_PROMPT_INVALID"),
    ],
)
async def test_model_admission_rejects_incomplete_provenance(mutation: str, code: str) -> None:
    values = model_rows()
    if mutation == "missing_config":
        values[0] = None
    elif mutation == "config_hash":
        values[0]["config_sha256"] = b"x" * 32
    elif mutation == "missing_capability":
        values[1] = None
    elif mutation == "expired":
        values[1]["expires_at"] = NOW
    elif mutation == "model":
        values[1]["model_name"] = "different"
    elif mutation == "roles":
        values[1]["supported_input_roles"] = ["user"]
    elif mutation == "text":
        values[1]["supports_text"] = False
    elif mutation == "metadata":
        values[2] = None
    elif mutation == "credential":
        values[3] = None
    elif mutation == "binding":
        values[3]["credential_id"] = UUID(int=99)
    elif mutation == "prompt":
        values[4] = None
    elif mutation == "prompt_hash":
        values[4]["template_sha256"] = b"x" * 32
    session = AsyncMock(execute=AsyncMock(side_effect=[result(value) for value in values]))
    with pytest.raises(MemoryPipelineError, match=code):
        await inputs.load_memory_model(
            session,
            prompt_version="custom" if mutation == "prompt_version" else "prompt-v1",
            now=NOW,
        )


async def test_source_selection_keeps_formal_context_and_canonical_roots() -> None:
    memory = {"id": UUID(int=40), "version_no": 2, "rendered_text": "memory"}
    summary = {
        "id": UUID(int=41),
        "version_no": 1,
        "content_text": "summary",
        "content_sha256": hashlib.sha256(b"summary").digest(),
    }
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[source_row()]),
                result(rows=[memory]),
                result(rows=[{**source_row(), "evidence_hash": source_row()["content_sha256"]}]),
                result(summary),
            ]
        )
    )
    session.scalar.return_value = 1
    sources = await inputs.select_memory_sources(session, job_row())
    assert [item.source_type for item in sources] == [
        "message_revision",
        "memory_version",
        "summary_version",
    ]
    assert sources[1].content == "memory"
    query = str(session.execute.call_args_list[0].args[0])
    assert "memory_evidence" not in query
    assert "source_event_id BETWEEN" in query


async def test_input_limits_reject_without_truncation() -> None:
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[source_row()] * 1001),
                result(rows=[{}] * 201),
            ]
        )
    )
    with pytest.raises(MemoryPipelineError, match="INPUT_LIMIT_EXCEEDED"):
        await inputs.select_memory_sources(session, job_row())


@pytest.mark.parametrize("kind", ["message_revision", "memory_version", "summary_version"])
@pytest.mark.parametrize("changed", [False, True])
async def test_reload_requires_current_membership_hash_and_trust(kind: str, changed: bool) -> None:
    row = source_row()
    body = "derived" if kind != "message_revision" else inputs.message_source(row).content
    item = {
        "source_type": kind,
        f"{kind}_id": row["id"],
        "source_revision": "revision-1" if kind == "message_revision" else "version-1",
        "source_content_sha256": b"x" * 32 if changed else hashlib.sha256(body.encode()).digest(),
        "trust_class": "user_statement" if kind == "message_revision" else "trusted_derived",
        "source_redacted": False,
        "source_visual_only": False,
    }
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[item]),
                result(row if kind == "message_revision" else (body, 1)),
            ]
        )
    )
    if changed:
        with pytest.raises(MemoryPipelineError, match="SOURCE_CHANGED"):
            await inputs.reload_memory_sources(session, job_row())
    else:
        assert (await inputs.reload_memory_sources(session, job_row()))[0].content == body


@pytest.mark.parametrize("reason", ["projection", "pending", "missing", "covered"])
async def test_watermark_coverage_cannot_ignore_unprocessed_sources(reason: str) -> None:
    source = inputs.message_source(source_row())
    session = AsyncMock()
    session.scalar.side_effect = [
        UUID(int=1) if reason == "projection" else None,
        UUID(int=2) if reason == "vision" else None,
    ]
    session.execute.side_effect = [
        result(
            rows=[
                SimpleNamespace(
                    id=source.source_id,
                    source_status="pending" if reason == "pending" else "resolved",
                )
            ]
        ),
        result(rows=[{"id": source.source_id}]),
    ]
    if reason == "covered":
        await inputs.require_coverage(session, job_row(), (source,))
    else:
        with pytest.raises(
            MemoryPipelineError,
            match={
                "projection": "PROJECTION_PENDING",
                "pending": "SOURCE_PENDING",
                "vision": "VISION_UNAVAILABLE",
                "missing": "COVERAGE_CHANGED",
            }[reason],
        ):
            await inputs.require_coverage(session, job_row(), ())


def worker_job() -> Any:
    row = job_row()
    return SimpleNamespace(
        id=memory_background_id(row["id"]),
        account_id=row["account_id"],
        job_type="memory.generate",
        lease_owner=row["lease_owner"],
        fencing_token=1,
        attempt_count=1,
        max_attempts=5,
    )


@pytest.mark.parametrize(
    "case", ["valid", "missing_parent", "extra_payload", "wrong_id", "missing_domain", "wrong_type"]
)
async def test_worker_parent_and_domain_binding(case: str) -> None:
    job = worker_job()
    payload = {
        "memory_job_id": str(job_row()["id"]),
        "conversation_id": str(job_row()["conversation_id"]),
    }
    if case == "extra_payload":
        payload["private"] = "must reject"
    if case == "wrong_id":
        payload["memory_job_id"] = str(UUID(int=99))
    if case == "wrong_type":
        job.job_type = "other"
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(),
                result(None if case == "missing_parent" else {"payload": payload}),
                result(),
                result(None if case == "missing_domain" else job_row()),
            ]
        )
    )
    repo = MemoryPipelineRepository(session)
    if case == "valid":
        assert (await repo.fenced_job(job, now=NOW))["id"] == job_row()["id"]
        sql = str(session.execute.call_args_list[1].args[0])
        assert "fencing_token" in sql
        assert "lease_expires_at >" in sql
    else:
        with pytest.raises(MemoryPipelineError):
            await repo.fenced_job(job, now=NOW)


@pytest.mark.parametrize(("retryable", "attempt"), [(False, 1), (True, 1), (True, 5)])
async def test_failures_keep_bounded_domain_and_attempt_states(
    retryable: bool, attempt: int
) -> None:
    session = AsyncMock()
    repo = MemoryPipelineRepository(session)
    repo.fenced_job = AsyncMock(return_value=job_row())  # type: ignore[method-assign]
    job = worker_job()
    job.attempt_count = attempt
    await repo.fail(job, code="MEMORY_PROVIDER_TIMEOUT", retryable=retryable, now=NOW)
    writes = [call.args[0].compile().params for call in session.execute.call_args_list]
    terminal = not retryable or attempt == 5
    assert writes[0]["state"] == ("dead_letter" if terminal else "retry_wait")
    assert writes[-1]["state"] == ("terminal_failed" if terminal else "retryable_failed")


async def test_replaced_domain_lease_cannot_be_failed_by_old_worker() -> None:
    value, _ = prepared()
    session = AsyncMock()
    repo = MemoryPipelineRepository(session)
    repo.fenced_job = AsyncMock(return_value={**job_row(), "job_version": 9})  # type: ignore[method-assign]
    with pytest.raises(MemoryPipelineError, match="MEMORY_JOB_FENCE_LOST"):
        await repo.fail(
            worker_job(), code="OLD_RESULT", retryable=False, now=NOW, expected=value.lease
        )
    session.execute.assert_not_awaited()


async def test_final_fence_reloads_sources_and_rejects_input_drift() -> None:

    value, _ = prepared()
    session = AsyncMock()
    repo = MemoryPipelineRepository(session)
    repo.fenced_job = AsyncMock(return_value=job_row())  # type: ignore[method-assign]
    repo.require_scope = AsyncMock()  # type: ignore[method-assign]
    repo._inputs = AsyncMock(return_value=replace(value, input_fingerprint=b"z" * 32))  # type: ignore[method-assign]
    with pytest.raises(MemoryPipelineError, match="MEMORY_INPUT_CHANGED"):
        await repo.verify_completion(worker_job(), value, secret=SECRET, now=NOW)
    repo._inputs.assert_awaited_once()


@pytest.mark.parametrize(
    "parent_state",
    [None, "leased", "succeeded", "dead_letter", "cancelled", "failed", "sealed_success"],
)
async def test_compensation_rearms_only_unattempted_quiet_window_jobs(
    parent_state: str | None,
) -> None:
    row = {**job_row(), "state": "pending", "input_manifest_id": None, "attempt_count": 0}
    parent = None if parent_state is None else {"id": worker_job().id, "state": parent_state}
    if parent_state == "sealed_success":
        row["input_manifest_id"] = UUID(int=55)
        parent = {"id": worker_job().id, "state": "succeeded"}
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result(rows=[row]),
                result(),
                result(row),
                result(parent),
                *[result() for _ in range(4)],
            ]
        )
    )
    repo = MemoryPipelineRepository(session)
    repo.require_scope = AsyncMock()  # type: ignore[method-assign]
    count = await repo.enqueue_pending(now=NOW)
    assert count == int(parent_state in {None, "succeeded"})
    writes = [call.args[0].compile().params for call in session.execute.call_args_list[4:]]
    if parent_state is None:
        assert writes[0]["payload"] == {
            "memory_job_id": str(row["id"]),
            "conversation_id": str(row["conversation_id"]),
        }
    elif parent_state == "succeeded":
        assert writes[0]["attempt_count"] == 0
    elif parent_state in {"dead_letter", "cancelled", "failed", "sealed_success"}:
        assert writes[0]["state"] == "dead_letter"
    else:
        assert not writes


@pytest.mark.parametrize("state", ["succeeded", "cancelled", "dead_letter", "quiet", "unsupported"])
async def test_claim_admission_never_calls_model_for_closed_or_not_due_jobs(state: str) -> None:
    row = job_row()
    row["state"] = state if state not in {"quiet", "unsupported"} else "pending"
    if state == "quiet":
        row["quiet_until"] = NOW + timedelta(seconds=30)
    if state == "unsupported":
        row["pipeline_version"] = "unknown"
    session = AsyncMock()
    repo = MemoryPipelineRepository(session)
    repo.fenced_job = AsyncMock(return_value=row)  # type: ignore[method-assign]
    repo.require_scope = AsyncMock()  # type: ignore[method-assign]
    if state == "unsupported":
        with pytest.raises(MemoryPipelineError, match="PIPELINE_UNSUPPORTED"):
            await repo.prepare(worker_job(), secret=SECRET, now=NOW)
    else:
        assert await repo.prepare(worker_job(), secret=SECRET, now=NOW) is None
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("replay", [False, True])
async def test_claim_journals_one_run_with_bounded_attempt_identity(replay: bool) -> None:
    value, _ = prepared()
    row = job_row()

    existing = (
        {"input_fingerprint": value.input_fingerprint, "adapter_version": ADAPTER_VERSION}
        if replay
        else None
    )
    session = AsyncMock(
        execute=AsyncMock(side_effect=[result(row), result(existing), result(), result(), result()])
    )
    repo = MemoryPipelineRepository(session)
    repo.fenced_job = AsyncMock(return_value=row)  # type: ignore[method-assign]
    repo.require_scope = AsyncMock()  # type: ignore[method-assign]
    repo._inputs = AsyncMock(return_value=value)  # type: ignore[method-assign]
    assert await repo.prepare(worker_job(), secret=SECRET, now=NOW) is value
    journal = session.execute.call_args_list[-1].args[0].compile().params
    assert journal["attempt_no"] == 1
    assert journal["state"] == "started"
    assert "synthetic-private-message" not in str(journal)


async def test_embedding_producer_stages_deterministic_chunks_and_keeps_replay_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "abc" * 1000
    memory = MagicMock(load_current_embedding_target=AsyncMock(return_value=body))
    monkeypatch.setattr(embedding, "MemoryRepository", lambda _: memory)
    session = AsyncMock()
    session.execute.return_value = result(rows=[{"id": UUID(int=81), "dimensions": 2}])
    session.scalar.side_effect = [UUID(int=1), UUID(int=2), UUID(int=3), None, None, None]
    repo = embedding.EmbeddingRuntimeRepository(session)
    assert (
        await repo.stage_target(
            account_id=UUID(int=1), target_kind="memory_version", target_id=UUID(int=2), now=NOW
        )
        == 3
    )
    assert (
        await repo.stage_target(
            account_id=UUID(int=1), target_kind="memory_version", target_id=UUID(int=2), now=NOW
        )
        == 0
    )
    writes = [call.args[0].compile().params for call in session.scalar.call_args_list]
    assert [item["id"] for item in writes[:3]] == [item["id"] for item in writes[3:]]
    assert [item["chunk_index"] for item in writes[:3]] == [0, 1, 2]
    assert all(item["state"] == "pending" and item["vector_payload"] == [] for item in writes)
    assert body not in str(writes)
    assert all(
        "ON CONFLICT DO NOTHING" in str(call.args[0]) for call in session.scalar.call_args_list
    )
    session.execute.return_value = result(rows=[])
    assert (
        await repo.stage_target(
            account_id=UUID(int=1), target_kind="message_revision", target_id=UUID(int=2), now=NOW
        )
        == 0
    )
    with pytest.raises(ValueError, match="target kind"):
        await repo.stage_target(
            account_id=UUID(int=1), target_kind="unsupported", target_id=UUID(int=2), now=NOW
        )
