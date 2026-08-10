"""Model endpoint canonicalization and fail-closed SSRF policy."""

import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class EndpointPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HostResolver(Protocol):
    def resolve(self, hostname: str, port: int) -> frozenset[IPAddress]: ...


class SystemHostResolver:
    def resolve(self, hostname: str, port: int) -> frozenset[IPAddress]:
        try:
            results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise EndpointPolicyError("ENDPOINT_DNS_FAILED") from error
        addresses = frozenset(
            ipaddress.ip_address(
                cast(Sequence[str | int], item[4])[0].__str__().split("%", maxsplit=1)[0]
            )
            for item in results
        )
        if not addresses:
            raise EndpointPolicyError("ENDPOINT_DNS_EMPTY")
        return addresses


@dataclass(frozen=True, slots=True)
class PublicEndpointPolicy:
    policy_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class PrivateEndpointPolicy:
    policy_id: UUID
    version: int
    scheme: str
    hostname: str
    port: int
    allowed_cidrs: tuple[IPNetwork, ...]

    def __post_init__(self) -> None:
        if self.scheme not in {"http", "https"} or not 1 <= self.port <= 65535:
            raise EndpointPolicyError("PRIVATE_POLICY_INVALID")
        if not self.allowed_cidrs:
            raise EndpointPolicyError("PRIVATE_POLICY_EMPTY")


EndpointPolicy = PublicEndpointPolicy | PrivateEndpointPolicy


@dataclass(frozen=True, slots=True)
class ValidatedEndpoint:
    policy_id: UUID
    policy_version: int
    base_url: str
    scheme: str
    hostname: str
    port: int
    base_path: str
    category: str
    resolved_addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransportSecurityContract:
    """Mandatory settings for a future production provider HTTP transport."""

    verify_tls: bool
    server_hostname: str
    approved_addresses: tuple[str, ...]
    follow_redirects: bool = False
    trust_environment: bool = False


def _canonical_host(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if not candidate or candidate == "localhost" or candidate.endswith(".localhost"):
        raise EndpointPolicyError("ENDPOINT_HOST_FORBIDDEN")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise EndpointPolicyError("ENDPOINT_HOST_INVALID") from error


def _canonical_path(raw_path: str) -> str:
    lower = raw_path.lower()
    if "%2f" in lower or "%5c" in lower or "\\" in raw_path or "//" in raw_path:
        raise EndpointPolicyError("ENDPOINT_PATH_INVALID")
    try:
        decoded = unquote(raw_path, errors="strict")
    except UnicodeDecodeError as error:
        raise EndpointPolicyError("ENDPOINT_PATH_INVALID") from error
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise EndpointPolicyError("ENDPOINT_PATH_INVALID")
    normalized = quote(decoded or "", safe="/-._~")
    if normalized == "/":
        return ""
    return normalized.rstrip("/")


def _is_forbidden_even_when_private(address: IPAddress) -> bool:
    return (
        address in METADATA_ADDRESSES
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _validate_addresses(addresses: frozenset[IPAddress], policy: EndpointPolicy) -> str:
    if not addresses:
        raise EndpointPolicyError("ENDPOINT_DNS_EMPTY")
    if isinstance(policy, PublicEndpointPolicy):
        if any(not address.is_global or address in METADATA_ADDRESSES for address in addresses):
            raise EndpointPolicyError("ENDPOINT_ADDRESS_FORBIDDEN")
        return "public"
    for address in addresses:
        if _is_forbidden_even_when_private(address):
            raise EndpointPolicyError("ENDPOINT_ADDRESS_FORBIDDEN")
        if not any(
            address.version == network.version and address in network
            for network in policy.allowed_cidrs
        ):
            raise EndpointPolicyError("ENDPOINT_PRIVATE_CIDR_MISMATCH")
    return "private"


def validate_endpoint(
    raw_url: str,
    *,
    policy: EndpointPolicy,
    resolver: HostResolver,
) -> ValidatedEndpoint:
    if len(raw_url) > 2048 or any(ord(character) < 32 for character in raw_url):
        raise EndpointPolicyError("ENDPOINT_URL_INVALID")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise EndpointPolicyError("ENDPOINT_URL_INVALID") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointPolicyError("ENDPOINT_SCHEME_FORBIDDEN")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EndpointPolicyError("ENDPOINT_URL_COMPONENT_FORBIDDEN")
    hostname = _canonical_host(parsed.hostname)
    if isinstance(policy, PublicEndpointPolicy):
        if scheme != "https":
            raise EndpointPolicyError("ENDPOINT_PUBLIC_HTTPS_REQUIRED")
    elif (scheme, hostname, port) != (policy.scheme, _canonical_host(policy.hostname), policy.port):
        raise EndpointPolicyError("ENDPOINT_PRIVATE_TUPLE_MISMATCH")
    path = _canonical_path(parsed.path)
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = frozenset({literal})
    except ValueError:
        addresses = resolver.resolve(hostname, port)
    category = _validate_addresses(addresses, policy)
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    authority = display_host if port == default_port else f"{display_host}:{port}"
    return ValidatedEndpoint(
        policy_id=policy.policy_id,
        policy_version=policy.version,
        base_url=f"{scheme}://{authority}{path}",
        scheme=scheme,
        hostname=hostname,
        port=port,
        base_path=path,
        category=category,
        resolved_addresses=tuple(sorted(str(address) for address in addresses)),
    )


def revalidate_endpoint(
    endpoint: ValidatedEndpoint,
    *,
    policy: EndpointPolicy,
    resolver: HostResolver,
) -> ValidatedEndpoint:
    current = validate_endpoint(endpoint.base_url, policy=policy, resolver=resolver)
    if current.policy_id != endpoint.policy_id or current.policy_version != endpoint.policy_version:
        raise EndpointPolicyError("ENDPOINT_POLICY_DRIFT")
    return current


def build_transport_security_contract(
    endpoint: ValidatedEndpoint,
    *,
    policy: EndpointPolicy,
    resolver: HostResolver,
) -> TransportSecurityContract:
    current = revalidate_endpoint(endpoint, policy=policy, resolver=resolver)
    return TransportSecurityContract(
        verify_tls=current.scheme == "https",
        server_hostname=current.hostname,
        approved_addresses=current.resolved_addresses,
    )


def reject_provider_redirect(status_code: int) -> None:
    if 300 <= status_code < 400:
        raise EndpointPolicyError("ENDPOINT_REDIRECT_FORBIDDEN")
