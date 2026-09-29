"""Numerical validation and provider failures preserve durable work semantics."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from telegram_userbot.adapters.llm.protocols import ProviderProtocolError, ProviderWireResponse
from telegram_userbot.adapters.persistence.embedding_runtime import (
    EmbeddingRuntimeError,
    PreparedEmbedding,
    checked_vector,
    embedding_job_id,
)
from telegram_userbot.adapters.persistence.model_runtime import RuntimeEndpointSnapshot
from telegram_userbot.domain.model_config import CanonicalModelConfig, LogicalRole, ModelProtocol
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding, CredentialKeyring
from telegram_userbot.processes import embedding_runtime as runtime
from telegram_userbot.processes.worker_executors import JobExecutionError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "vector", [(1.0,), (True, 2.0), (float("nan"), 1.0), (float("inf"), 1.0), (0.0, 0.0)]
)
def test_invalid_vector_rejected(vector: tuple[float, ...]) -> None:
    with pytest.raises(EmbeddingRuntimeError, match="EMBEDDING_VECTOR_INVALID"):
        checked_vector(vector, dimensions=2, normalization="l2")


def test_stable_normalization_and_space_policy() -> None:
    assert checked_vector((3e200, 4e200), dimensions=2, normalization="l2") == pytest.approx(
        [0.6, 0.8]
    )
    assert checked_vector((0.0, 0.0), dimensions=2, normalization="none") == [0.0, 0.0]
    with pytest.raises(EmbeddingRuntimeError, match="NORMALIZATION_UNSUPPORTED"):
        checked_vector((1.0, 2.0), dimensions=2, normalization="unknown")
    assert embedding_job_id(UUID(int=1)) == embedding_job_id(UUID(int=1))
    assert embedding_job_id(UUID(int=1)) != embedding_job_id(UUID(int=2))


def prepared() -> tuple[PreparedEmbedding, CredentialKeyring]:
    profile, credential, endpoint, record, policy = (UUID(int=i) for i in range(1, 6))
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"s" * 32)},
    )
    binding = CredentialBinding(LogicalRole.EMBEDDING, profile, credential, 1)
    return PreparedEmbedding(
        record,
        {"dimensions": 2, "normalization": "l2"},
        CanonicalModelConfig(
            profile,
            LogicalRole.EMBEDDING,
            endpoint,
            credential,
            ModelProtocol.EMBEDDING,
            "synthetic",
            None,
            None,
            1,
            True,
            {},
        ),
        RuntimeEndpointSnapshot(
            endpoint, "https://embedding.example.invalid/v1", policy, 1, "public"
        ),
        binding,
        keyring.encrypt(SensitiveValue("synthetic-key"), binding=binding),
        SensitiveValue("synthetic-private-text"),
    ), keyring


class Resolver:
    def resolve(self, hostname: str, port: int) -> frozenset[IPv4Address]:
        return frozenset({IPv4Address("8.8.8.8")})


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any]:
    value, keyring = prepared()
    repo = MagicMock()
    repo.prepare = AsyncMock(return_value=value)
    repo.complete = AsyncMock()
    repo.fail = AsyncMock()
    monkeypatch.setattr(runtime, "EmbeddingRuntimeRepository", lambda _: repo)
    ctx = MagicMock()
    ctx.lease_lost = asyncio.Event()
    ctx.job.attempt_count = 1
    ctx.job.max_attempts = 5
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    ctx.sessions.return_value.__aenter__ = AsyncMock(return_value=session)
    transport = MagicMock()
    transport.send = AsyncMock(
        return_value=ProviderWireResponse(
            200,
            SensitiveValue(
                {
                    "data": [{"index": 0, "embedding": [3.0, 4.0]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            ),
        )
    )
    executor = runtime.EmbeddingExecutor(
        keyring=keyring,
        resolver=Resolver(),
        transport_factory=lambda **_: transport,
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    return ctx, repo, transport, executor


async def test_wire_call_and_completed_replay(setup: tuple[Any, Any, Any, Any]) -> None:
    ctx, repo, transport, executor = setup
    await executor(ctx)
    repo.complete.assert_awaited_once()
    request = transport.send.call_args.args[0]
    assert request.body.reveal_for_use()["input"] == ["synthetic-private-text"]
    assert request.headers["authorization"].reveal_for_use() == "Bearer synthetic-key"
    assert "synthetic-private-text" not in repr(repo.prepare.return_value)
    repo.prepare.return_value = None
    await executor(ctx)
    assert transport.send.await_count == 1


@pytest.mark.parametrize("when", ["before", "during"])
async def test_lease_loss_stops_write(setup: tuple[Any, Any, Any, Any], when: str) -> None:
    ctx, repo, transport, executor = setup
    if when == "before":
        ctx.lease_lost.set()
    else:

        def respond(_: Any) -> Any:
            ctx.lease_lost.set()
            return transport.send.return_value

        transport.send.side_effect = respond
    with pytest.raises(JobExecutionError, match="WORKER_JOB_FENCE_LOST"):
        await executor(ctx)
    repo.complete.assert_not_awaited()
    repo.fail.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ProviderProtocolError("MODEL_RATE_LIMITED", retryable=True),
        RuntimeError("private"),
    ],
)
@pytest.mark.parametrize("last_attempt", [True, False])
async def test_transient_failure_keeps_bounded_retry_budget(
    setup: tuple[Any, Any, Any, Any],
    error: Exception,
    last_attempt: bool,
) -> None:
    ctx, repo, transport, executor = setup
    ctx.job.attempt_count = 5 if last_attempt else 1
    transport.send.side_effect = error
    with pytest.raises(JobExecutionError) as failure:
        await executor(ctx)
    assert failure.value.retryable
    assert "private" not in str(failure.value)
    assert repo.fail.await_count == int(last_attempt)
    repo.complete.assert_not_awaited()


async def test_cancel_leaves_lease_for_recovery(setup: tuple[Any, Any, Any, Any]) -> None:
    ctx, repo, transport, executor = setup
    transport.send.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await executor(ctx)
    repo.fail.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["private_endpoint", "bad_key", "bad_vector", "bad_output", "repository"]
)
async def test_terminal_failure_is_content_free(
    setup: tuple[Any, Any, Any, Any],
    failure: str,
) -> None:
    ctx, repo, transport, executor = setup
    value = repo.prepare.return_value
    if failure == "private_endpoint":
        repo.prepare.return_value = replace(
            value, endpoint=replace(value.endpoint, network_category="private")
        )
    elif failure == "bad_key":
        repo.prepare.return_value = replace(value, binding=replace(value.binding, version_no=2))
    elif failure == "repository":
        repo.prepare.side_effect = EmbeddingRuntimeError("EMBEDDING_SOURCE_UNAVAILABLE")
        repo.fail.side_effect = EmbeddingRuntimeError("WORKER_JOB_FENCE_LOST")
    else:
        transport.send.return_value = ProviderWireResponse(
            200,
            SensitiveValue(
                {"data": [{"index": 0, "embedding": [float("nan"), 1.0]}]}
                if failure == "bad_vector"
                else {"data": []}
            ),
        )
    with pytest.raises(JobExecutionError) as caught:
        await executor(ctx)
    assert not caught.value.retryable
    repo.fail.assert_awaited_once()
    repo.complete.assert_not_awaited()
