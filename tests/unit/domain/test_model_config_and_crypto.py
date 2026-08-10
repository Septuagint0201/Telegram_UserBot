from ipaddress import ip_address, ip_network
from types import MappingProxyType
from uuid import uuid7

import pytest

from telegram_userbot.domain.model_config import (
    CanonicalModelConfig,
    LogicalRole,
    ModelCapabilities,
    ModelConfigurationError,
    ModelProtocol,
    ProfileKind,
    validate_activation,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import (
    CredentialBinding,
    CredentialCryptoError,
    CredentialKeyring,
)
from telegram_userbot.platform.network.endpoint_policy import (
    EndpointPolicyError,
    PrivateEndpointPolicy,
    PublicEndpointPolicy,
    build_transport_security_contract,
    reject_provider_redirect,
    revalidate_endpoint,
    validate_endpoint,
)


class Resolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = frozenset(ip_address(value) for value in addresses)

    def resolve(self, hostname: str, port: int) -> frozenset[object]:
        assert hostname
        assert port
        return self.addresses


def config(
    *,
    role: LogicalRole = LogicalRole.MAIN_AI,
    protocol: ModelProtocol = ModelProtocol.OPENAI_RESPONSES,
    options: dict[str, object] | None = None,
    temperature: float | None = 0.5,
    output: int | None = 512,
) -> CanonicalModelConfig:
    return CanonicalModelConfig(
        uuid7(),
        role,
        uuid7(),
        uuid7(),
        protocol,
        "synthetic-model",
        temperature,
        output,
        30,
        True,
        options or {},
    )


@pytest.mark.unit
def test_canonical_configuration_normalizes_protocol_specific_options() -> None:
    responses = config(options={"reasoning_effort": "medium"})
    chat = config(
        protocol=ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        options={"token_limit_field": "max_completion_tokens"},
    )
    messages = config(
        protocol=ModelProtocol.ANTHROPIC_MESSAGES,
        options={},
    )
    embedding = config(
        role=LogicalRole.EMBEDDING,
        protocol=ModelProtocol.EMBEDDING,
        options={"dimensions": 3},
        temperature=None,
        output=None,
    )
    assert responses.canonical_payload()["endpoint_id"] == str(responses.endpoint_id)
    expected_limit_field = "max_completion_tokens"
    assert chat.protocol_options["token_limit_field"] == expected_limit_field
    assert messages.protocol_options == MappingProxyType(
        {"api_version": "2023-06-01", "auth_scheme": "x_api_key", "request_path": "/messages"}
    )
    assert embedding.protocol_options["batch_size"] == 64
    with pytest.raises(TypeError):
        chat.protocol_options["new"] = "forbidden"  # type: ignore[index]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"role": LogicalRole.EMBEDDING}, "embedding protocol"),
        ({"protocol": ModelProtocol.EMBEDDING}, "generation protocol"),
        ({"temperature": 3.0}, "temperature"),
        ({"output": 0}, "output limit"),
        ({"options": {"unknown": True}}, "Responses"),
        (
            {
                "protocol": ModelProtocol.OPENAI_CHAT_COMPLETIONS,
                "options": {"token_limit_field": "legacy"},
            },
            "Chat Completions",
        ),
        (
            {
                "protocol": ModelProtocol.ANTHROPIC_MESSAGES,
                "options": {"request_path": "/completions"},
            },
            "Messages",
        ),
    ],
)
def test_canonical_configuration_rejects_invalid_cross_protocol_values(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ModelConfigurationError, match=match):
        config(**kwargs)  # type: ignore[arg-type]


def generation_capabilities(**overrides: object) -> ModelCapabilities:
    values: dict[str, object] = {
        "profile_kind": ProfileKind.GENERATION,
        "supported_protocols": frozenset(
            {
                ModelProtocol.OPENAI_RESPONSES,
                ModelProtocol.OPENAI_CHAT_COMPLETIONS,
                ModelProtocol.ANTHROPIC_MESSAGES,
            }
        ),
        "supports_text": True,
        "supports_temperature": True,
        "supports_structured_output": True,
        "supports_streaming": True,
        "supports_images": True,
        "supports_reasoning_effort": True,
        "max_context_tokens": 32_000,
        "max_output_tokens_limit": 4096,
        "supported_input_roles": frozenset({"system", "user", "assistant"}),
        "chat_token_limit_field": "max_completion_tokens",
    }
    values.update(overrides)
    return ModelCapabilities(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_capability_activation_gate_and_payload() -> None:
    capabilities = generation_capabilities()
    validate_activation(config(), capabilities)
    assert capabilities.as_payload()["supports_images"] is True
    chat = config(
        protocol=ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        options={"token_limit_field": "max_completion_tokens"},
    )
    validate_activation(chat, capabilities)
    with pytest.raises(ModelConfigurationError, match="structured"):
        validate_activation(
            config(role=LogicalRole.MEMORY_AGENT),
            generation_capabilities(supports_structured_output=False),
        )


@pytest.mark.unit
def test_capability_snapshot_and_activation_reject_incomplete_or_mismatched_data() -> None:
    invalid_chat_field = "legacy"
    with pytest.raises(ModelConfigurationError, match="incomplete"):
        generation_capabilities(supported_protocols=frozenset())
    with pytest.raises(ModelConfigurationError, match="output capability"):
        generation_capabilities(max_output_tokens_limit=None)
    with pytest.raises(ModelConfigurationError, match="token field"):
        generation_capabilities(chat_token_limit_field=invalid_chat_field)
    with pytest.raises(ModelConfigurationError, match="kind"):
        validate_activation(
            config(),
            ModelCapabilities(
                ProfileKind.EMBEDDING,
                frozenset({ModelProtocol.EMBEDDING}),
                False,
                False,
                False,
                False,
                False,
                8192,
                None,
                frozenset({"user"}),
                embedding_dimensions=frozenset({2}),
            ),
        )
    with pytest.raises(ModelConfigurationError, match="unsupported"):
        validate_activation(
            config(),
            generation_capabilities(
                supported_protocols=frozenset({ModelProtocol.ANTHROPIC_MESSAGES})
            ),
        )
    with pytest.raises(ModelConfigurationError, match="temperature"):
        validate_activation(config(), generation_capabilities(supports_temperature=False))
    with pytest.raises(ModelConfigurationError, match="reasoning"):
        validate_activation(
            config(options={"reasoning_effort": "high"}),
            generation_capabilities(supports_reasoning_effort=False),
        )
    embedding = config(
        role=LogicalRole.EMBEDDING,
        protocol=ModelProtocol.EMBEDDING,
        options={"dimensions": 3},
        temperature=None,
        output=None,
    )
    with pytest.raises(ModelConfigurationError, match="dimensions"):
        validate_activation(
            embedding,
            ModelCapabilities(
                ProfileKind.EMBEDDING,
                frozenset({ModelProtocol.EMBEDDING}),
                False,
                False,
                False,
                False,
                False,
                8192,
                None,
                frozenset({"user"}),
                embedding_dimensions=frozenset({2}),
            ),
        )
    with pytest.raises(ModelConfigurationError, match="output limit"):
        validate_activation(config(output=5000), generation_capabilities())
    chat = config(
        protocol=ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        options={"token_limit_field": "max_completion_tokens"},
    )
    conflicting_limit_field = "max_tokens"
    with pytest.raises(ModelConfigurationError, match="token limit"):
        validate_activation(
            chat,
            generation_capabilities(chat_token_limit_field=conflicting_limit_field),
        )


@pytest.mark.unit
def test_credential_envelope_nonce_aad_rotation_and_redaction() -> None:
    role = LogicalRole.MAIN_AI
    profile_id, credential_id = uuid7(), uuid7()
    keyring = CredentialKeyring(
        deployment_id="synthetic-deployment",
        active_key_version=1,
        keys={1: SensitiveValue(b"a" * 32), 2: SensitiveValue(b"b" * 32)},
    )
    binding = CredentialBinding(role, profile_id, credential_id, 1)
    first = keyring.encrypt(SensitiveValue("SYNTHETIC_API_KEY"), binding=binding)
    second = keyring.encrypt(SensitiveValue("SYNTHETIC_API_KEY"), binding=binding)
    assert first.nonce != second.nonce
    assert first.secret_fingerprint == second.secret_fingerprint
    assert keyring.decrypt(first, binding=binding).reveal_for_use() == "SYNTHETIC_API_KEY"
    assert "SYNTHETIC" not in repr(first)
    assert "aaaaaaaa" not in repr(keyring)
    with pytest.raises(CredentialCryptoError, match="authentication"):
        keyring.decrypt(
            first,
            binding=CredentialBinding(role, profile_id, credential_id, 2),
        )
    rotated = keyring.rotate(first, old_binding=binding, new_version_no=2)
    assert (
        keyring.decrypt(
            rotated,
            binding=CredentialBinding(role, profile_id, credential_id, 2),
        ).reveal_for_use()
        == "SYNTHETIC_API_KEY"
    )


@pytest.mark.unit
def test_credential_keyring_rejects_invalid_material_and_secret() -> None:
    with pytest.raises(CredentialCryptoError, match="32 bytes"):
        CredentialKeyring(
            deployment_id="synthetic",
            active_key_version=1,
            keys={1: SensitiveValue(b"short")},
        )
    keyring = CredentialKeyring(
        deployment_id="synthetic",
        active_key_version=1,
        keys={1: SensitiveValue(b"a" * 32)},
    )
    binding = CredentialBinding(LogicalRole.MAIN_AI, uuid7(), uuid7(), 1)
    with pytest.raises(CredentialCryptoError, match="input"):
        keyring.encrypt(SensitiveValue(""), binding=binding)


@pytest.mark.unit
def test_public_and_private_endpoint_policy_is_fail_closed() -> None:
    public = PublicEndpointPolicy(uuid7(), 1)
    validated = validate_endpoint(
        "HTTPS://API.Example.COM./v1",
        policy=public,
        resolver=Resolver("8.8.8.8"),  # type: ignore[arg-type]
    )
    assert validated.base_url == "https://api.example.com/v1"
    assert validated.category == "public"
    assert (
        revalidate_endpoint(
            validated,
            policy=public,
            resolver=Resolver("8.8.8.8"),  # type: ignore[arg-type]
        )
        == validated
    )
    transport = build_transport_security_contract(
        validated,
        policy=public,
        resolver=Resolver("8.8.8.8"),  # type: ignore[arg-type]
    )
    assert transport.verify_tls
    assert not transport.follow_redirects
    assert not transport.trust_environment
    assert transport.approved_addresses == ("8.8.8.8",)
    with pytest.raises(EndpointPolicyError, match="REDIRECT_FORBIDDEN"):
        reject_provider_redirect(307)
    reject_provider_redirect(200)

    private = PrivateEndpointPolicy(
        uuid7(), 1, "https", "models.internal", 8443, (ip_network("10.0.0.0/8"),)
    )
    internal = validate_endpoint(
        "https://models.internal:8443/v1",
        policy=private,
        resolver=Resolver("10.2.3.4"),  # type: ignore[arg-type]
    )
    assert internal.category == "private"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "address", "code"),
    [
        ("http://api.example.com", "8.8.8.8", "HTTPS_REQUIRED"),
        ("https://api.example.com", "127.0.0.1", "ADDRESS_FORBIDDEN"),
        ("https://169.254.169.254", "169.254.169.254", "ADDRESS_FORBIDDEN"),
        ("https://user:pass@example.com", "8.8.8.8", "COMPONENT_FORBIDDEN"),
        ("https://example.com/v1%2fadmin", "8.8.8.8", "PATH_INVALID"),
    ],
)
def test_endpoint_policy_rejects_malicious_url_corpus(url: str, address: str, code: str) -> None:
    with pytest.raises(EndpointPolicyError, match=code):
        validate_endpoint(
            url,
            policy=PublicEndpointPolicy(uuid7(), 1),
            resolver=Resolver(address),  # type: ignore[arg-type]
        )
