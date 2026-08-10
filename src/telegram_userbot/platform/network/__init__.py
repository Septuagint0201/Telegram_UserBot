"""Validated outbound network policy."""

from telegram_userbot.platform.network.endpoint_policy import (
    EndpointPolicyError,
    HostResolver,
    PrivateEndpointPolicy,
    PublicEndpointPolicy,
    SystemHostResolver,
    TransportSecurityContract,
    ValidatedEndpoint,
    build_transport_security_contract,
    reject_provider_redirect,
    revalidate_endpoint,
    validate_endpoint,
)

__all__ = [
    "EndpointPolicyError",
    "HostResolver",
    "PrivateEndpointPolicy",
    "PublicEndpointPolicy",
    "SystemHostResolver",
    "TransportSecurityContract",
    "ValidatedEndpoint",
    "build_transport_security_contract",
    "reject_provider_redirect",
    "revalidate_endpoint",
    "validate_endpoint",
]
