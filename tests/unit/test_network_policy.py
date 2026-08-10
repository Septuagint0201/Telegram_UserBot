import socket

import pytest

from tests.conftest import _is_allowed_address, _is_loopback


@pytest.mark.unit
def test_external_socket_connect_is_blocked() -> None:
    with socket.socket() as candidate, pytest.raises(AssertionError, match="external network"):
        candidate.connect(("192.0.2.1", 443))


@pytest.mark.unit
@pytest.mark.parametrize("host", ["127.0.0.1", "127.12.34.56", "::1", "localhost"])
def test_loopback_addresses_are_recognized(host: str) -> None:
    assert _is_loopback(host)


@pytest.mark.unit
def test_only_exact_configured_service_address_is_allowed() -> None:
    allowed = frozenset({("192.0.2.10", 6379)})

    assert _is_allowed_address(("192.0.2.10", 6379), allowed)
    assert not _is_allowed_address(("192.0.2.10", 6380), allowed)
    assert not _is_allowed_address(("192.0.2.11", 6379), allowed)


@pytest.mark.unit
def test_unconfigured_external_address_is_not_allowed() -> None:
    assert not _is_allowed_address(("192.0.2.10", 6379), frozenset())
