"""Memory input provenance and provider failures never bypass durable decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from tests.unit.processes.test_embedding_runtime import Resolver
from tests.unit.processes.test_embedding_runtime import prepared as embedding_prepared

from telegram_userbot.adapters.llm.protocols import ProviderProtocolError, ProviderWireResponse
from telegram_userbot.adapters.persistence.memory_inputs import (
    MemoryModelSnapshot,
    MemoryPipelineError,
    message_source,
)
from telegram_userbot.adapters.persistence.memory_pipeline import (
    PreparedMemory,
    due,
    input_document,
    input_fingerprint,
    lease_value,
    manifest_value,
    memory_background_id,
)
from telegram_userbot.domain.memory.models import TrustClass
from telegram_userbot.domain.messaging.events import BodyKind, MessageBody
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding
from telegram_userbot.processes import memory_pipeline as runtime
from telegram_userbot.processes.worker_executors import JobExecutionError

pytestmark = pytest.mark.unit
NOW = datetime(2030, 1, 1, tzinfo=UTC)
SECRET = b"m" * 32


def source_row() -> dict[str, Any]:
    body = MessageBody(
        BodyKind.TEXT, "synthetic-private-message", ({"type": "bold", "offset": 0, "length": 3},)
    )
    return {
        "id": UUID(int=30),
        "revision_no": 1,
        "body_kind": "text",
        "text_content": body.text,
        "caption": None,
        "entities": body.entities,
        "content_sha256": body.content_sha256,
        "message_source": "telegram_user",
    }


def job_row() -> dict[str, Any]:
    return {
        "id": UUID(int=20),
        "account_id": UUID(int=21),
        "conversation_id": UUID(int=22),
        "job_kind": "episode",
        "generation": 1,
        "range_start_event_id": 1,
        "range_end_event_id": 10,
        "lease_owner": UUID(int=23),
        "job_version": 2,
        "input_manifest_id": UUID(int=24),
        "pipeline_version": "m6-v1",
        "policy_version": "policy-v1",
        "prompt_version": "prompt-v1",
        "input_schema_version": 1,
        "output_schema_version": 1,
        "quiet_until": NOW,
        "hard_due_at": NOW + timedelta(minutes=10),
        "eligible_revision_count": 1,
        "estimated_input_tokens": 1,
        "state": "running",
        "lease_expires_at": NOW + timedelta(seconds=60),
        "attempt_count": 1,
    }


def prepared() -> tuple[PreparedMemory, Any]:
    embedding, keyring = embedding_prepared()
    old = embedding.config
    config = CanonicalModelConfig(
        old.profile_id,
        LogicalRole.MEMORY_AGENT,
        old.endpoint_id,
        old.credential_id,
        ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        "synthetic",
        None,
        1000,
        1,
        True,
        {},
    )
    cap = ModelCapabilities(
        ProfileKind.GENERATION,
        frozenset({config.protocol}),
        True,
        False,
        True,
        False,
        False,
        32768,
        4096,
        frozenset({"system", "user"}),
        chat_token_limit_field="max_tokens",  # noqa: S106 - provider field name
    )
    binding = CredentialBinding(LogicalRole.MEMORY_AGENT, old.profile_id, old.credential_id, 1)
    model = MemoryModelSnapshot(
        UUID(int=5),
        UUID(int=6),
        config,
        cap,
        b"c" * 32,
        embedding.endpoint,
        binding,
        keyring.encrypt(SensitiveValue("synthetic-key"), binding=binding),
        SensitiveValue("Synthetic prompt"),
        b"p" * 32,
    )
    manifest = manifest_value(job_row(), (message_source(source_row()),))
    body = input_document(manifest, [])
    return PreparedMemory(
        lease_value(job_row()),
        manifest,
        model,
        SensitiveValue(body),
        input_fingerprint(SECRET, model, manifest, body),
    ), keyring


def test_canonical_message_envelope_hash_and_trust() -> None:
    row = source_row()
    value = message_source(row)
    assert value.content_sha256 == hashlib.sha256(value.content.encode()).digest()
    assert json.loads(value.content)["text"] == row["text_content"]
    assert value.trust is TrustClass.USER_STATEMENT
    assert message_source({**row, "message_source": "ai"}).trust is TrustClass.MODEL_INFERENCE
    assert message_source({**row, "message_source": "human"}).trust is TrustClass.USER_STATEMENT
    caption = MessageBody(BodyKind.CAPTION, "caption")
    assert (
        json.loads(
            message_source(
                {
                    **row,
                    "body_kind": "caption",
                    "caption": "caption",
                    "entities": [],
                    "content_sha256": caption.content_sha256,
                }
            ).content
        )["kind"]
        == "caption"
    )
    with pytest.raises(MemoryPipelineError, match="MEMORY_SOURCE_INVALID"):
        message_source(
            {**row, "content_sha256": hashlib.sha256(row["text_content"].encode()).digest()}
        )


def test_request_and_fingerprint_pin_content_versions_without_repr_leaks() -> None:
    value, _ = prepared()
    assert "synthetic-private-message" not in repr(value)
    assert "synthetic-key" not in repr(value.model)
    request = runtime.generation_request(value)
    assert [item.role for item in request.messages] == ["system", "user"]
    assert "untrusted" in request.messages[0].content[0].value.reveal_for_use()
    assert value.input_fingerprint == input_fingerprint(
        SECRET, value.model, value.manifest, value.user_input.reveal_for_use()
    )
    for model in (
        replace(value.model, config_id=UUID(int=77)),
        replace(value.model, credential_version_id=UUID(int=78)),
        replace(value.model, prompt_sha256=b"z" * 32),
        replace(value.model, capability_sha256=b"z" * 32),
    ):
        assert (
            input_fingerprint(SECRET, model, value.manifest, value.user_input.reveal_for_use())
            != value.input_fingerprint
        )
    assert (
        input_fingerprint(SECRET, value.model, value.manifest, "changed") != value.input_fingerprint
    )
    assert memory_background_id(UUID(int=1)) != memory_background_id(UUID(int=2))


@pytest.mark.parametrize(
    "change",
    [{}, {"eligible_revision_count": 20}, {"estimated_input_tokens": 6000}, {"hard_due_at": NOW}],
)
def test_quiet_and_hard_thresholds(change: dict[str, Any]) -> None:
    row = {**job_row(), "quiet_until": NOW + timedelta(minutes=1)}
    assert due({**row, **change}, NOW) == bool(change)
    assert due(job_row(), NOW)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any, Any]:
    value, keyring = prepared()
    repo = MagicMock(prepare=AsyncMock(return_value=value), fail=AsyncMock())
    results = MagicMock(complete=AsyncMock())
    monkeypatch.setattr(runtime, "MemoryPipelineRepository", lambda _: repo)
    monkeypatch.setattr(runtime, "MemoryResultRepository", lambda _: results)
    ctx = MagicMock()
    ctx.lease_lost = asyncio.Event()
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    ctx.sessions.return_value.__aenter__ = AsyncMock(return_value=session)
    transport = MagicMock(
        send=AsyncMock(
            return_value=ProviderWireResponse(
                200,
                SensitiveValue(
                    {
                        "choices": [
                            {
                                "message": {"content": '{"schema_version":1,"proposals":[]}'},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ),
            )
        )
    )
    executor = runtime.MemoryPipelineExecutor(
        keyring=keyring,
        fingerprint_secret=SensitiveValue(SECRET),
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: NOW,
    )
    return ctx, repo, results, transport, executor


async def test_provider_success_and_replay(setup: tuple[Any, ...]) -> None:
    ctx, repo, results, transport, executor = setup
    await executor(ctx)
    results.complete.assert_awaited_once()
    assert results.complete.call_args.args[2] == ()
    request = transport.send.call_args.args[0]
    assert request.headers["authorization"].reveal_for_use() == "Bearer synthetic-key"
    repo.prepare.return_value = None
    await executor(ctx)
    assert transport.send.await_count == 1


@pytest.mark.parametrize("when", ["before", "during"])
async def test_cancellation_fence_prevents_output(setup: tuple[Any, ...], when: str) -> None:
    ctx, repo, results, transport, executor = setup
    if when == "before":
        ctx.lease_lost.set()
    else:

        def response(_: Any) -> Any:
            ctx.lease_lost.set()
            return transport.send.return_value

        transport.send.side_effect = response
    with pytest.raises(JobExecutionError, match="WORKER_JOB_FENCE_LOST"):
        await executor(ctx)
    results.complete.assert_not_awaited()
    assert repo.fail.call_args.kwargs["expected"] == (
        None if when == "before" else repo.prepare.return_value.lease
    )


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (TimeoutError(), "MEMORY_PROVIDER_TIMEOUT", True),
        (ProviderProtocolError("MODEL_RATE_LIMITED", retryable=True), "MODEL_RATE_LIMITED", True),
        (ValueError("private"), "MEMORY_RESULT_REJECTED", False),
        (RuntimeError("private"), "MEMORY_EXECUTION_FAILED", True),
    ],
)
async def test_stable_failure_codes(
    setup: tuple[Any, ...], error: Exception, code: str, retryable: bool
) -> None:
    ctx, repo, results, transport, executor = setup
    transport.send.side_effect = error
    with pytest.raises(JobExecutionError, match=code) as failure:
        await executor(ctx)
    assert failure.value.retryable == retryable
    assert repo.fail.call_args.kwargs["code"] == code
    results.complete.assert_not_awaited()


async def test_shutdown_leaves_started_attempt_for_recovery(setup: tuple[Any, ...]) -> None:
    ctx, repo, results, transport, executor = setup
    transport.send.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await executor(ctx)
    repo.fail.assert_not_awaited()
    results.complete.assert_not_awaited()


@pytest.mark.parametrize("kind", ["invalid", "length", "private_endpoint", "bad_key"])
async def test_bad_output_and_endpoint_fail_closed(setup: tuple[Any, ...], kind: str) -> None:
    ctx, repo, results, transport, executor = setup
    value = repo.prepare.return_value
    if kind == "private_endpoint":
        repo.prepare.return_value = replace(
            value,
            model=replace(
                value.model, endpoint=replace(value.model.endpoint, network_category="private")
            ),
        )
    elif kind == "bad_key":
        repo.prepare.return_value = replace(
            value,
            model=replace(
                value.model, envelope=replace(value.model.envelope, ciphertext=b"x" * 32)
            ),
        )
    else:
        body = transport.send.return_value.body.reveal_for_use()
        body["choices"][0]["message"]["content"] = (
            "invalid" if kind == "invalid" else '{"schema_version":1,"proposals":[]}'
        )
        body["choices"][0]["finish_reason"] = "length" if kind == "length" else "stop"
    with pytest.raises(JobExecutionError) as failure:
        await executor(ctx)
    assert not failure.value.retryable
    results.complete.assert_not_awaited()
