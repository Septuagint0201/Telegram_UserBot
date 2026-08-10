"""Global deterministic and no-network test policy."""

import os
import socket
from collections.abc import Collection
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import pytest
from _pytest.monkeypatch import MonkeyPatch
from hypothesis import settings

settings.register_profile("deterministic", derandomize=True, deadline=None, max_examples=50)
settings.register_profile("ci", derandomize=True, deadline=None, max_examples=100)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "deterministic"))


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host.split("%", maxsplit=1)[0]).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _configured_service_addresses() -> frozenset[tuple[str, int]]:
    addresses: set[tuple[str, int]] = set()
    for variable, default_port in (
        ("TEST_POSTGRES_DSN", 5432),
        ("TEST_REDIS_URL", 6379),
    ):
        value = os.environ.get(variable)
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.hostname is None:
            raise pytest.UsageError(f"{variable} must contain a hostname")
        port = parsed.port or default_port
        addresses.add((parsed.hostname, port))
        for result in socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        ):
            sockaddr = result[4]
            addresses.add((str(sockaddr[0]), int(sockaddr[1])))
    return frozenset(addresses)


def _is_allowed_address(
    address: Any,
    allowed: Collection[tuple[str, int]],
) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = str(address[0]), address[1]
    if not isinstance(port, int):
        return False
    return _is_loopback(host) or (host, port) in allowed


@pytest.fixture(autouse=True)
def deny_external_network(
    monkeypatch: MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    allows_services = any(
        request.node.get_closest_marker(marker) is not None
        for marker in ("integration", "recovery")
    )
    allowed = _configured_service_addresses() if allows_services else frozenset()

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if _is_allowed_address(address, allowed):
            return original_create_connection(address, *args, **kwargs)
        raise AssertionError("tests must not access external network addresses")

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if _is_allowed_address(address, allowed):
            original_connect(sock, address)
            return
        raise AssertionError("tests must not access external network addresses")

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
