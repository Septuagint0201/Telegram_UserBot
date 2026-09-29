import asyncio
import ipaddress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import telegram_userbot.adapters.llm.capability_probe as probe_module
from telegram_userbot.adapters.llm import (
    ProductionModelCapabilityProbe,
    ProviderTransport,
    ProviderWireRequest,
    ProviderWireResponse,
    SyntheticCapabilityCeilings,
    SyntheticCapabilityProbeError,
)
from telegram_userbot.adapters.llm.capability_probe import (
    _resolved_probe_config,
    _strict_probe_json,
)
from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelConfigurationError,
    ModelProtocol,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialBinding, CredentialKeyring
from telegram_userbot.platform.network import ValidatedEndpoint

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
PROFILE = UUID(int=1)
ENDPOINT = UUID(int=2)
CREDENTIAL = UUID(int=3)
POLICY = UUID(int=4)


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _Transaction:
    def __init__(self, owner: _Sessions) -> None:
        self._owner = owner

    async def __aenter__(self) -> _Transaction:
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

    async def execute(self, statement: object) -> _Result:
        del statement
        return _Result(self._owner.row)


class _Sessions:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.transaction_active = False

    def __call__(self) -> _Session:
        return _Session(self)


class _Resolver:
    def resolve(self, hostname: str, port: int) -> frozenset[ipaddress.IPv4Address]:
        assert hostname == "api.example.test"
        assert port == 443
        return frozenset({cast(ipaddress.IPv4Address, ipaddress.ip_address("8.8.8.8"))})


class _Transport:
    def __init__(self, sessions: _Sessions, *, embedding: bool = False) -> None:
        self.sessions = sessions
        self.embedding = embedding
        self.requests: list[ProviderWireRequest] = []

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        assert not self.sessions.transaction_active
        self.requests.append(request)
        if self.embedding:
            body: dict[str, Any] = {
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"input_tokens": 3, "output_tokens": 0},
            }
        else:
            body = {
                "output_text": "SYNTHETIC_PROBE_OK_V1",
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        return ProviderWireResponse(
            200,
            SensitiveValue(body),
            provider_request_id="synthetic-request-1",
        )


class _GenerationOutputTransport(_Transport):
    def __init__(self, sessions: _Sessions, output: str) -> None:
        super().__init__(sessions)
        self.output = output

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        response = await super().send(request)
        body = dict(response.body.reveal_for_use())
        body["output_text"] = self.output
        return replace(response, body=SensitiveValue(body))


class _EmbeddingVectorTransport(_Transport):
    def __init__(self, sessions: _Sessions, vectors: list[list[float]]) -> None:
        super().__init__(sessions, embedding=True)
        self.vectors = vectors

    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        response = await super().send(request)
        body = dict(response.body.reveal_for_use())
        body["data"] = [
            {"index": index, "embedding": vector} for index, vector in enumerate(self.vectors)
        ]
        return replace(response, body=SensitiveValue(body))


class _DirectEmbeddingClient:
    def __init__(self, vectors: tuple[tuple[float, ...], ...]) -> None:
        self.vectors = vectors

    async def embed(self, **_: object) -> Any:
        return type("NormalizedEmbedding", (), {"vectors": self.vectors})()


class _HangingTransport:
    async def send(self, request: ProviderWireRequest) -> ProviderWireResponse:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _config(protocol: ModelProtocol = ModelProtocol.OPENAI_RESPONSES) -> CanonicalModelConfig:
    role = LogicalRole.EMBEDDING if protocol is ModelProtocol.EMBEDDING else LogicalRole.MAIN_AI
    return CanonicalModelConfig(
        PROFILE,
        role,
        ENDPOINT,
        CREDENTIAL,
        protocol,
        "synthetic-model",
        None if protocol is ModelProtocol.EMBEDDING else 0.2,
        None if protocol is ModelProtocol.EMBEDDING else 512,
        10,
        True,
        {"dimensions": 3} if protocol is ModelProtocol.EMBEDDING else {},
    )


def _fixture() -> tuple[CredentialKeyring, dict[str, object]]:
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"k" * 32)},
    )
    binding = CredentialBinding(LogicalRole.MAIN_AI, PROFILE, CREDENTIAL, 1)
    envelope = keyring.encrypt(SensitiveValue("SYNTHETIC_API_KEY"), binding=binding)
    return keyring, {
        "base_url": "https://api.example.test/v1",
        "network_policy_id": POLICY,
        "network_policy_version": 1,
        "network_category": "public",
        "version_no": 1,
        "algorithm": envelope.algorithm,
        "key_version": envelope.key_version,
        "aad_schema_version": envelope.aad_schema_version,
        "nonce": envelope.nonce,
        "ciphertext": envelope.ciphertext,
        "secret_fingerprint": envelope.secret_fingerprint,
    }


def _row_for_role(
    keyring: CredentialKeyring,
    row: dict[str, object],
    role: LogicalRole,
) -> dict[str, object]:
    envelope = keyring.encrypt(
        SensitiveValue("SYNTHETIC_API_KEY"),
        binding=CredentialBinding(role, PROFILE, CREDENTIAL, 1),
    )
    return {
        **row,
        "algorithm": envelope.algorithm,
        "key_version": envelope.key_version,
        "aad_schema_version": envelope.aad_schema_version,
        "nonce": envelope.nonce,
        "ciphertext": envelope.ciphertext,
        "secret_fingerprint": envelope.secret_fingerprint,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_probe_closes_db_before_synthetic_http_and_proves_image() -> None:
    keyring, row = _fixture()
    sessions = _Sessions(row)
    transport = _Transport(sessions)
    probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: cast(ProviderTransport, transport),
    )

    capabilities = await probe.probe(config=_config(), now=NOW)

    assert capabilities.supports_images
    assert capabilities.supports_temperature
    assert capabilities.max_context_tokens == 32_000
    assert len(transport.requests) == 1
    body = transport.requests[0].body.reveal_for_use()
    assert body["model"] == "synthetic-model"
    assert [item["role"] for item in body["input"]] == ["system", "assistant", "user"]
    assert "data:image/png;base64," in str(body)
    assert "SYNTHETIC_API_KEY" not in repr(transport.requests[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embedding_probe_binds_exact_dimension_and_credential_role() -> None:
    keyring, row = _fixture()
    embedding_binding = CredentialBinding(LogicalRole.EMBEDDING, PROFILE, CREDENTIAL, 1)
    envelope = keyring.encrypt(SensitiveValue("SYNTHETIC_API_KEY"), binding=embedding_binding)
    row.update(
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
        secret_fingerprint=envelope.secret_fingerprint,
    )
    sessions = _Sessions(row)
    transport = _Transport(sessions, embedding=True)
    probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: cast(ProviderTransport, transport),
    )

    capabilities = await probe.probe(config=_config(ModelProtocol.EMBEDDING), now=NOW)

    assert capabilities.embedding_dimensions == frozenset({3})
    assert capabilities.max_output_tokens_limit is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_has_hard_deadline_and_private_endpoint_fails_closed() -> None:
    keyring, row = _fixture()
    sessions = _Sessions(row)
    timeout_probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: _HangingTransport(),
        total_timeout_seconds=0.01,
    )
    with pytest.raises(SyntheticCapabilityProbeError, match="MODEL_CAPABILITY_PROBE_TIMEOUT"):
        await timeout_probe.probe(config=_config(), now=NOW)

    row["network_category"] = "private"
    private_probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
    )
    with pytest.raises(SyntheticCapabilityProbeError, match="MODEL_CAPABILITY_PROBE_FAILED"):
        await private_probe.probe(config=_config(), now=NOW)

    with pytest.raises(ValueError, match="deadline"):
        ProductionModelCapabilityProbe(
            cast(async_sessionmaker[AsyncSession], sessions),
            keyring=keyring,
            resolver=_Resolver(),
            total_timeout_seconds=31,
        )
    with pytest.raises(ValueError, match="deadline"):
        ProductionModelCapabilityProbe(
            cast(async_sessionmaker[AsyncSession], sessions),
            keyring=keyring,
            resolver=_Resolver(),
            total_timeout_seconds=True,
        )


@pytest.mark.unit
def test_generation_config_cannot_exceed_the_global_output_ceiling() -> None:
    with pytest.raises(ModelConfigurationError, match="output limit"):
        replace(_config(), max_output_tokens=8_193)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_probe_never_lets_config_raise_the_local_output_ceiling() -> None:
    keyring, row = _fixture()
    sessions = _Sessions(row)
    transport = _Transport(sessions)
    ceilings = SyntheticCapabilityCeilings(generation_output_tokens=1_024)
    probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        ceilings=ceilings,
        transport_factory=lambda **_: cast(ProviderTransport, transport),
    )

    admitted = await probe.probe(config=replace(_config(), max_output_tokens=1_024), now=NOW)
    assert admitted.max_output_tokens_limit == 1_024

    with pytest.raises(SyntheticCapabilityProbeError, match="MODEL_CAPABILITY_PROBE_FAILED"):
        await probe.probe(config=replace(_config(), max_output_tokens=1_025), now=NOW)
    assert len(transport.requests) == 1


@pytest.mark.unit
def test_synthetic_generation_output_ceiling_must_fit_the_context_ceiling() -> None:
    with pytest.raises(ValueError, match="output ceiling"):
        SyntheticCapabilityCeilings(
            generation_context_tokens=4_096,
            generation_output_tokens=2_049,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_images_per_request": 0},
        {"auto_image_tokens": True},
    ],
)
def test_synthetic_capability_ceilings_require_positive_exact_integer_limits(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="ceilings must be positive"):
        SyntheticCapabilityCeilings(**cast(dict[str, int], kwargs))


@pytest.mark.unit
def test_synthetic_capability_ceilings_require_context_for_probe_input() -> None:
    with pytest.raises(ValueError, match="context ceiling is too small"):
        SyntheticCapabilityCeilings(generation_context_tokens=2_048)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_rejects_naive_time_and_missing_or_drifting_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring, row = _fixture()
    sessions = _Sessions(row)
    probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
    )

    with pytest.raises(SyntheticCapabilityProbeError, match="TIME_INVALID"):
        await probe.probe(config=_config(), now=NOW.replace(tzinfo=None))

    sessions.row = cast(Any, None)
    with pytest.raises(SyntheticCapabilityProbeError, match="SNAPSHOT_UNAVAILABLE"):
        await probe._snapshot(_config())

    sessions.row = row
    monkeypatch.setattr(
        probe_module,
        "validate_endpoint",
        lambda *_args, **_kwargs: ValidatedEndpoint(
            POLICY,
            1,
            "https://drift.example.test/v1",
            "https",
            "drift.example.test",
            443,
            "/v1",
            "public",
            ("8.8.8.8",),
        ),
    )
    with pytest.raises(SyntheticCapabilityProbeError, match="ENDPOINT_DRIFT"):
        probe._snapshot_from_row(_config(), cast(Any, row))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "output", "expected"),
    [
        (LogicalRole.MEMORY_AGENT, '{"status":"SYNTHETIC_PROBE_OK_V1"}', None),
        (LogicalRole.MEMORY_AGENT, '{"status":"wrong"}', "MODEL_CAPABILITY_PROBE_FAILED"),
        (LogicalRole.MAIN_AI, "wrong", "MODEL_CAPABILITY_PROBE_FAILED"),
    ],
)
async def test_generation_probe_handles_structured_and_plain_outputs(
    role: LogicalRole,
    output: str,
    expected: str | None,
) -> None:
    keyring, source_row = _fixture()
    row = _row_for_role(keyring, source_row, role)
    sessions = _Sessions(row)
    transport = _GenerationOutputTransport(sessions, output)
    probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: cast(ProviderTransport, transport),
    )

    if expected is None:
        capabilities = await probe.probe(
            config=replace(_config(), logical_role=role),
            now=NOW,
        )
        assert capabilities.supports_structured_output
        assert not capabilities.supports_images
    else:
        with pytest.raises(SyntheticCapabilityProbeError, match=expected):
            await probe.probe(config=replace(_config(), logical_role=role), now=NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embedding_probe_rejects_vector_count_and_configured_dimension_mismatch() -> None:
    keyring, source_row = _fixture()
    row = _row_for_role(keyring, source_row, LogicalRole.EMBEDDING)
    sessions = _Sessions(row)

    count_transport = _EmbeddingVectorTransport(sessions, [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    count_probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: cast(ProviderTransport, count_transport),
    )
    with pytest.raises(SyntheticCapabilityProbeError, match="MODEL_CAPABILITY_PROBE_FAILED"):
        await count_probe.probe(config=_config(ModelProtocol.EMBEDDING), now=NOW)

    dimension_transport = _EmbeddingVectorTransport(sessions, [[0.1, 0.2, 0.3]])
    dimension_probe = ProductionModelCapabilityProbe(
        cast(async_sessionmaker[AsyncSession], sessions),
        keyring=keyring,
        resolver=_Resolver(),
        transport_factory=lambda **_: cast(ProviderTransport, dimension_transport),
    )
    with pytest.raises(SyntheticCapabilityProbeError, match="MODEL_CAPABILITY_PROBE_FAILED"):
        await dimension_probe.probe(
            config=replace(
                _config(ModelProtocol.EMBEDDING),
                protocol_options={"dimensions": 2},
            ),
            now=NOW,
        )

    with pytest.raises(SyntheticCapabilityProbeError, match="OUTPUT_INVALID"):
        await count_probe._probe_embedding(
            cast(Any, _DirectEmbeddingClient(((0.1, 0.2, 0.3), (0.4, 0.5, 0.6)))),
            _config(ModelProtocol.EMBEDDING),
            SensitiveValue("SYNTHETIC_API_KEY"),
        )
    with pytest.raises(SyntheticCapabilityProbeError, match="OUTPUT_INVALID"):
        await dimension_probe._probe_embedding(
            cast(Any, _DirectEmbeddingClient(((0.1, 0.2, 0.3),))),
            replace(
                _config(ModelProtocol.EMBEDDING),
                protocol_options={"dimensions": 2},
            ),
            SensitiveValue("SYNTHETIC_API_KEY"),
        )


@pytest.mark.unit
def test_probe_helpers_normalize_chat_field_and_strict_json() -> None:
    chat = replace(
        _config(ModelProtocol.OPENAI_CHAT_COMPLETIONS),
        protocol_options={"token_limit_field": "auto"},
    )
    resolved, field = _resolved_probe_config(chat)
    assert field == "max_completion_tokens"
    assert resolved.protocol_options["token_limit_field"] == field

    assert _strict_probe_json('{"status":"SYNTHETIC_PROBE_OK_V1"}') == {
        "status": "SYNTHETIC_PROBE_OK_V1"
    }
    with pytest.raises(ValueError, match="duplicate"):
        _strict_probe_json('{"status":1,"status":2}')
    with pytest.raises(TypeError, match="not an object"):
        _strict_probe_json("[]")
