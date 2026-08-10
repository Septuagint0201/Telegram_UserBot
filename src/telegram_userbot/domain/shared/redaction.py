"""Wrappers that are safe by default when formatted or inspected."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SensitiveValue[T]:
    _value: T

    def reveal_for_use(self) -> T:
        """Return the value only at an explicitly reviewed use boundary."""
        return self._value

    def __str__(self) -> str:
        return "<redacted>"

    def __repr__(self) -> str:
        return "SensitiveValue(<redacted>)"
