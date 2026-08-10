"""Explicit success/error and evidence status types."""

from dataclasses import dataclass
from enum import StrEnum


class EvidenceStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - evidence state, not a password
    FAIL = "FAIL"
    NOT_RUN = "NOT RUN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class AppError:
    code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if (
            not self.code
            or not self.code.replace("_", "").isalnum()
            or self.code.upper() != self.code
        ):
            raise ValueError("error code must use upper-case letters, digits, and underscores")


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E


type Result[T, E] = Ok[T] | Err[E]
