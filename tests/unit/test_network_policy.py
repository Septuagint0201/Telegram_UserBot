import socket

import pytest


@pytest.mark.unit
def test_external_socket_connect_is_blocked() -> None:
    with socket.socket() as candidate, pytest.raises(AssertionError, match="external network"):
        candidate.connect(("192.0.2.1", 443))
