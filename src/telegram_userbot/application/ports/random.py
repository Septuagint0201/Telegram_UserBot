"""Cryptographic/randomness port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class RandomSource(Protocol):
    def random_bytes(self, size: int) -> bytes: ...

    def random_below(self, upper_bound: int) -> int: ...
