from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.processes.model_gateway as gateway_module
from telegram_userbot.adapters.llm import (
    ProviderTransport,
    ProviderWireRequest,
    ProviderWireResponse,
)
from telegram_userbot.adapters.persistence.model_runtime import (
    PreparedModelGeneration,
    RuntimeEndpointSnapshot,
    RuntimeImageSnapshot,
)
from telegram_userbot.application.ports.model import (
    ModelGatewayError,
    ModelRequest,
)
from telegram_userbot.domain.context import Candidate, ContextLayer, ContextSource, TrustLevel
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelProtocol,
    ProfileKind,
)
from telegram_userbot.domain.shared.ids import RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding, CredentialKeyring
from telegram_userbot.processes.model_gateway import (
    ProductionModelGateway,
    RuntimeImageLoader,
    SnapshotEndpointPolicyResolver,
    build_production_model_gateway,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
RUN = UUID(int=1)
ACCOUNT = UUID(int=2)
CONVERSATION = UUID(int=3)
TURN = UUID(int=4)
PROFILE = UUID(int=5)
ENDPOINT = UUID(int=6)
CREDENTIAL = UUID(int=7)
POLICY = UUID(int=8)
IMAGE = UUID(int=9)
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"


class _Transaction:
    def __init__(self, owner: _Sessions) -> None:
        self._owner = owner

    async def __aenter__(self) -> _Transaction:
        assert not self._owner.transaction_active
        self._owner.transaction_active = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self._owner.transaction_active = False


class _Session:
    def __init__(self, owner: _Sessions) -> None:
        self._owner = owner

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction(self._owner)


class _Sessions:
    def __init__(self) -> None:
        self.transaction_active = False

    def __call__(self) -> _Session:
        return _Session(self)


class _Repository:
    prepared: ClassVar[PreparedModelGeneration]
    sessions: ClassVar[_Sessions]
    calls = 0
    expected_roles: ClassVar[list[LogicalRole]] = []

    def __init__(self, session: object, *, new_uuid: object) -> None:
        del session, new_uuid

    async def prepare_or_load_generation(self, **kwargs: object) -> PreparedModelGeneration:
        assert self.sessions.transaction_active
        type(self).calls += 1
        type(self).expected_roles.append(cast(LogicalRole, kwargs["expected_logical_role"]))
        await asyncio.sleep(0)
        return replace(self.prepared, run_id=cast(UUID, kwargs["run_id"]))


class _Resolver:
    def resolve(self, hostname: str, port: int) -> frozenset[ipaddress.IPv4Address]:
        assert hostname == "api.example.test"
        assert port == 443
        return frozenset({ipaddress.IPv4Address("8.8.8.8")})


class _ImageLoader:
    def __init__(self, payloads: tuple[bytes, ...]) -> None:
        self.payloads = deque(payloads)
        self.calls = 0

    async def load(self, image: RuntimeImageSnapshot) -> SensitiveValue[bytes]:
        assert image.object_id == IMAGE
        self.calls += 1
        return SensitiveValue(self.payloads.popleft())


class _Transport:
    def __init__(
        self,
        sessions: _Sessions,
        *,
        response: ProviderWireResponse | None = None,
    ) -> None:
        self.sessions = sessions
        self.response = response or ProviderWireResponse(
            200,
            SensitiveValue(
                {
                    "output_text": "synthetic answer",
                    "status": "completed",
                    "usage": {"input_tokens": 21, "output_tokens": 4},
                }
            ),
            provider_request_id="request-safe-1",
        )
        self.requests: list[ProviderWireRequest] = []

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        assert not self.sessions.transaction_active
        self.requests.append(request)
        return self.response


def _prepared(
    role: LogicalRole = LogicalRole.MAIN_AI,
) -> tuple[PreparedModelGeneration, CredentialKeyring]:
    digest = hashlib.sha256(PNG).digest()
    config = CanonicalModelConfig(
        PROFILE,
        role,
        ENDPOINT,
        CREDENTIAL,
        ModelProtocol.OPENAI_RESPONSES,
        "synthetic-model",
        0.2,
        512,
        10,
        True,
        {},
    )
    capabilities = ModelCapabilities(
        ProfileKind.GENERATION,
        frozenset({ModelProtocol.OPENAI_RESPONSES}),
        True,
        True,
        role is not LogicalRole.MAIN_AI,
        False,
        role is LogicalRole.MAIN_AI,
        32_000,
        512,
        frozenset({"system", "user", "assistant"}),
        max_images_per_request=1,
        max_image_bytes_per_request=len(PNG),
        auto_image_tokens=2_048,
    )
    prompt = ContextSource(
        Candidate(
            UUID(int=10),
            "version-1",
            "prompt:main-ai",
            ContextLayer.INSTRUCTION,
            None,
            3,
        ),
        "system",
        "packaged_prompt",
        TrustLevel.SYSTEM,
        SensitiveValue("trusted synthetic prompt"),
        "trusted_instruction",
    )
    image = ContextSource(
        Candidate(
            IMAGE,
            f"sha256-{digest.hex()}",
            f"media:{IMAGE}",
            ContextLayer.CURRENT,
            NOW,
            3,
        ),
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue("[SYNTHETIC_IMAGE]"),
        "media_object",
        image_detail="auto",
        image_tokens=2_048,
    )
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"k" * 32)},
    )
    binding = CredentialBinding(role, PROFILE, CREDENTIAL, 1)
    envelope = keyring.encrypt(SensitiveValue("SYNTHETIC_API_KEY"), binding=binding)
    return (
        PreparedModelGeneration(
            RUN,
            ACCOUNT,
            CONVERSATION,
            TURN,
            "conversation_reply",
            config,
            b"c" * 32,
            capabilities,
            b"a" * 32,
            RuntimeEndpointSnapshot(
                ENDPOINT,
                "https://api.example.test/v1",
                POLICY,
                1,
                "public",
            ),
            binding,
            envelope,
            UUID(int=11),
            b"m" * 32,
            b"i" * 32,
            b"n" * 32,
            (prompt, image) if role is LogicalRole.MAIN_AI else (prompt,),
            (
                (RuntimeImageSnapshot(IMAGE, "synthetic.png", digest, "image/png", len(PNG)),)
                if role is LogicalRole.MAIN_AI
                else ()
            ),
        ),
        keyring,
    )


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_loader: _ImageLoader,
    transport: _Transport,
    max_prepared_runs: int = 256,
    role: LogicalRole = LogicalRole.MAIN_AI,
) -> ProductionModelGateway:
    prepared, keyring = _prepared(role)
    _Repository.prepared = prepared
    _Repository.sessions = transport.sessions
    _Repository.calls = 0
    _Repository.expected_roles = []
    monkeypatch.setattr(gateway_module, "ModelRuntimeRepository", _Repository)
    return ProductionModelGateway(
        cast(async_sessionmaker[AsyncSession], transport.sessions),
        keyring=keyring,
        input_hmac_key=SensitiveValue(b"h" * 32),
        endpoint_policies=SnapshotEndpointPolicyResolver(),
        resolver=_Resolver(),
        image_loader=cast(RuntimeImageLoader, image_loader),
        new_uuid=lambda: UUID(int=12),
        transport_factory=lambda **_: cast(ProviderTransport, transport),
        max_prepared_runs=max_prepared_runs,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_closes_transaction_freezes_image_and_returns_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    loader = _ImageLoader((PNG, PNG))
    gateway = _gateway(monkeypatch, image_loader=loader, transport=transport)

    response = await gateway.generate(ModelRequest(RunId(RUN), "main_ai", (b"i" * 32).hex()))

    assert response.text.reveal_for_use() == "synthetic answer"
    assert response.input_tokens == 21
    assert response.output_tokens == 4
    assert response.finish_reason == "completed"
    assert response.provider_request_id == "request-safe-1"
    assert response.http_status == 200
    assert loader.calls == 2
    assert _Repository.calls == 1
    assert _Repository.expected_roles == [LogicalRole.MAIN_AI]
    assert len(transport.requests) == 1
    assert "data:image/png;base64," in str(transport.requests[0].body.reveal_for_use())
    assert "SYNTHETIC_API_KEY" not in repr(transport.requests[0])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [LogicalRole.MAIN_AI, LogicalRole.MEMORY_AGENT, LogicalRole.PROACTIVE_AGENT],
)
async def test_gateway_binds_each_generation_request_to_the_durable_run_role(
    monkeypatch: pytest.MonkeyPatch,
    role: LogicalRole,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    loader = _ImageLoader((PNG, PNG) if role is LogicalRole.MAIN_AI else ())
    gateway = _gateway(
        monkeypatch,
        image_loader=loader,
        transport=transport,
        role=role,
    )

    response = await gateway.generate(ModelRequest(RunId(RUN), role.value, (b"i" * 32).hex()))

    assert response.text.reveal_for_use() == "synthetic answer"
    assert _Repository.expected_roles == [role]
    assert len(transport.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_profile",
    ["memory_agent", "proactive_agent", "embedding", "unknown"],
)
async def test_gateway_rejects_caller_selected_role_mismatch_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    requested_profile: str,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
        role=LogicalRole.MAIN_AI,
    )

    with pytest.raises(ModelGatewayError, match="MODEL_PROFILE_MISMATCH"):
        await gateway.generate(ModelRequest(RunId(RUN), requested_profile, (b"i" * 32).hex()))

    assert not transport.requests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_rejects_image_toctou_and_input_fingerprint_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    changed = PNG[:-1] + bytes([PNG[-1] ^ 1])
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, changed)),
        transport=transport,
    )

    with pytest.raises(ModelGatewayError, match="MODEL_IMAGE_SNAPSHOT_INVALID"):
        await gateway.generate(ModelRequest(RunId(RUN), "main_ai", (b"i" * 32).hex()))
    assert not transport.requests

    fingerprint_gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
    )
    with pytest.raises(ModelGatewayError, match="MODEL_INPUT_FINGERPRINT_MISMATCH"):
        await fingerprint_gateway.generate(ModelRequest(RunId(RUN), "main_ai", "0" * 64))
    assert not transport.requests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_preserves_only_bounded_provider_failure_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(
        sessions,
        response=ProviderWireResponse(
            503,
            SensitiveValue({"error": "private provider detail"}),
            provider_request_id="request-safe-503",
            retry_after_seconds=7,
        ),
    )
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.generate(ModelRequest(RunId(RUN), "main_ai", (b"i" * 32).hex()))

    error = captured.value
    assert error.code == "PROVIDER_TRANSIENT"
    assert error.retryable
    assert error.http_status == 503
    assert error.retry_after_seconds == 7
    assert error.provider_request_id == "request-safe-503"
    assert "private provider detail" not in repr(error)


@pytest.mark.unit
def test_production_gateway_builder_uses_private_store_and_public_snapshot_policy(
    tmp_path: Path,
) -> None:
    sessions = cast(async_sessionmaker[AsyncSession], _Sessions())
    _, keyring = _prepared()

    gateway = build_production_model_gateway(
        sessions,
        keyring=keyring,
        input_hmac_key=SensitiveValue(b"h" * 32),
        media_root=tmp_path / "provider-media",
        resolver=_Resolver(),
    )

    assert isinstance(gateway, ProductionModelGateway)
    assert (tmp_path / "provider-media").is_dir()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_bounds_sensitive_prepared_run_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
        max_prepared_runs=2,
    )

    run_ids = (UUID(int=101), UUID(int=102), UUID(int=103))
    for run_id in run_ids:
        await gateway._prepare(run_id)

    assert tuple(gateway._prepared) == run_ids[-2:]
    assert _Repository.calls == 3
    assert len(gateway._prepare_locks) == 2

    # An evicted run is reconstructed from its durable manifest instead of
    # selecting new context or depending on the in-memory LRU for correctness.
    reloaded = await gateway._prepare(run_ids[0])
    assert reloaded.run_id == run_ids[0]
    assert reloaded.orchestration_claim_fingerprint == b"i" * 32
    assert reloaded.canonical_input_fingerprint == b"n" * 32
    assert _Repository.calls == 4

    # A fresh gateway instance models process restart and uses the same durable
    # prepare-or-load contract.
    restarted = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
        max_prepared_runs=2,
    )
    restarted_snapshot = await restarted._prepare(run_ids[0])
    assert restarted_snapshot.run_id == run_ids[0]
    assert restarted_snapshot.orchestration_claim_fingerprint == b"i" * 32
    assert restarted_snapshot.canonical_input_fingerprint == b"n" * 32
    assert _Repository.calls == 1
    with pytest.raises(ValueError, match="cache limit"):
        _gateway(
            monkeypatch,
            image_loader=_ImageLoader((PNG, PNG)),
            transport=transport,
            max_prepared_runs=0,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_concurrent_same_run_uses_one_durable_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
        max_prepared_runs=2,
    )

    first, second = await asyncio.gather(gateway._prepare(RUN), gateway._prepare(RUN))

    assert first is second
    assert _Repository.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_safe_retry_keeps_original_claim_and_cached_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG, PNG, PNG)),
        transport=transport,
    )
    request = ModelRequest(RunId(RUN), "main_ai", (b"i" * 32).hex())

    first = await gateway.generate(request)
    second = await gateway.generate(request)

    assert first.text.reveal_for_use() == second.text.reveal_for_use()
    assert _Repository.calls == 1
    assert len(transport.requests) == 2
    cached = gateway._prepared[RUN]
    assert cached.orchestration_claim_fingerprint == b"i" * 32
    assert cached.canonical_input_fingerprint == b"n" * 32
    assert cached.orchestration_claim_fingerprint != cached.canonical_input_fingerprint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_rejects_membership_claim_drift_after_lru_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    transport = _Transport(sessions)
    gateway = _gateway(
        monkeypatch,
        image_loader=_ImageLoader((PNG, PNG)),
        transport=transport,
        max_prepared_runs=1,
    )
    await gateway._prepare(RUN)
    await gateway._prepare(UUID(int=100))
    _Repository.prepared = replace(
        _Repository.prepared,
        orchestration_claim_fingerprint=b"d" * 32,
    )

    with pytest.raises(ModelGatewayError, match="MODEL_INPUT_FINGERPRINT_MISMATCH"):
        await gateway.generate(ModelRequest(RunId(RUN), "main_ai", (b"i" * 32).hex()))

    assert _Repository.calls == 3
    assert not transport.requests
