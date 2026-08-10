"""Global deterministic and no-network test policy."""

import os
import socket
from typing import Any, Never

import pytest
from _pytest.monkeypatch import MonkeyPatch
from hypothesis import settings

settings.register_profile("deterministic", derandomize=True, deadline=None, max_examples=50)
settings.register_profile("ci", derandomize=True, deadline=None, max_examples=100)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "deterministic"))


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch: MonkeyPatch) -> None:
    original_connect = socket.socket.connect

    def blocked(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("tests must not access the network")

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            original_connect(sock, address)
            return
        raise AssertionError("tests must not access external network addresses")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
