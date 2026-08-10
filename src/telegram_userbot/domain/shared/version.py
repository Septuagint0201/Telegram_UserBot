"""Monotonic revision and immutable version numbers."""

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True, order=True)
class Revision:
    value: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("revision must be a non-negative integer")

    def next(self) -> Self:
        return type(self)(self.value + 1)


@dataclass(frozen=True, slots=True, order=True)
class Version:
    value: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ValueError("version must be a positive integer")

    def next(self) -> Self:
        return type(self)(self.value + 1)
