"""Embedding provider composition; database transactions never span HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from telegram_userbot.adapters.llm.protocols import (
    CanonicalEmbeddingRequest,
    CanonicalProtocolClient,
    ProviderProtocolError,
)
from telegram_userbot.adapters.persistence.embedding_runtime import (
    EmbeddingRuntimeError,
    EmbeddingRuntimeRepository,
    PreparedEmbedding,
)
from telegram_userbot.platform.crypto import CredentialCryptoError, CredentialKeyring
from telegram_userbot.platform.network import (
    EndpointPolicyError,
    HostResolver,
    SystemHostResolver,
    validate_endpoint,
)
from telegram_userbot.processes.model_gateway import (
    ProviderTransportFactory,
    SnapshotEndpointPolicyResolver,
    default_transport_factory,
)
from telegram_userbot.processes.worker_executors import JobExecutionContext, JobExecutionError


class EmbeddingExecutor:
    def __init__(
        self,
        *,
        keyring: CredentialKeyring,
        resolver: HostResolver | None = None,
        transport_factory: ProviderTransportFactory = default_transport_factory,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._keyring = keyring
        self._resolver = resolver or SystemHostResolver()
        self._transport_factory = transport_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def __call__(self, context: JobExecutionContext) -> None:
        try:
            self._require_lease(context)
            async with context.sessions() as session, session.begin():
                prepared = await EmbeddingRuntimeRepository(session).prepare(
                    context.job, now=self._now()
                )
            if prepared is None:
                return
            self._require_lease(context)
            async with asyncio.timeout(prepared.config.timeout_seconds):
                vector = await self._embed(prepared)
            self._require_lease(context)
            async with context.sessions() as session, session.begin():
                await EmbeddingRuntimeRepository(session).complete(
                    context.job, prepared, vector, now=self._now()
                )
        except EmbeddingRuntimeError as error:
            await self._fail(context, error.code, retryable=error.retryable)
        except ProviderProtocolError as error:
            await self._fail(context, error.code, retryable=error.retryable)
        except CredentialCryptoError:
            await self._fail(context, "EMBEDDING_CREDENTIAL_UNAVAILABLE", retryable=False)
        except EndpointPolicyError:
            await self._fail(context, "EMBEDDING_ENDPOINT_REJECTED", retryable=False)
        except TimeoutError:
            await self._fail(context, "EMBEDDING_PROVIDER_TIMEOUT", retryable=True)
        except Exception:
            await self._fail(context, "EMBEDDING_EXECUTION_FAILED", retryable=True)

    @staticmethod
    def _require_lease(context: JobExecutionContext) -> None:
        if context.lease_lost.is_set():
            raise EmbeddingRuntimeError("WORKER_JOB_FENCE_LOST", retryable=True)

    async def _embed(self, prepared: PreparedEmbedding) -> tuple[float, ...]:
        policy = SnapshotEndpointPolicyResolver().resolve(prepared.endpoint)
        endpoint = validate_endpoint(
            prepared.endpoint.base_url, policy=policy, resolver=self._resolver
        )
        if (
            endpoint.base_url != prepared.endpoint.base_url
            or endpoint.policy_id != prepared.endpoint.network_policy_id
            or endpoint.policy_version != prepared.endpoint.network_policy_version
            or endpoint.category != prepared.endpoint.network_category
        ):
            raise EmbeddingRuntimeError("EMBEDDING_ENDPOINT_SNAPSHOT_CHANGED")
        api_key = self._keyring.decrypt(prepared.envelope, binding=prepared.binding)
        transport = self._transport_factory(
            endpoint=endpoint, policy=policy, resolver=self._resolver
        )
        result = await CanonicalProtocolClient(transport).embed(
            config=prepared.config,
            request=CanonicalEmbeddingRequest((prepared.content,)),
            api_key=api_key,
        )
        if len(result.vectors) != 1:
            raise EmbeddingRuntimeError("EMBEDDING_VECTOR_INVALID")
        return result.vectors[0]

    async def _fail(self, context: JobExecutionContext, code: str, *, retryable: bool) -> None:
        if not retryable or context.job.attempt_count >= context.job.max_attempts:
            async with context.sessions() as session, session.begin():
                # A replacement lease or erasure may own the row now. The
                # consumer's final state transition is fenced independently.
                with suppress(EmbeddingRuntimeError):
                    await EmbeddingRuntimeRepository(session).fail(
                        context.job, code=code, now=self._now()
                    )
        raise JobExecutionError(code, retryable=retryable)
