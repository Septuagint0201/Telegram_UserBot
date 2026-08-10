"""Content-free scanner for deliberately invalid test sentinels."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SentinelFinding:
    source: str
    line: int
    fingerprint: str


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def scan_text_for_sentinels(
    text: str,
    *,
    source: str,
    sentinels: Iterable[str],
) -> tuple[SentinelFinding, ...]:
    findings: list[SentinelFinding] = []
    lines = text.splitlines()
    for sentinel in sentinels:
        if not sentinel:
            raise ValueError("sentinel must not be empty")
        for line_number, line in enumerate(lines, start=1):
            if sentinel in line:
                findings.append(
                    SentinelFinding(
                        source=source,
                        line=line_number,
                        fingerprint=_fingerprint(sentinel),
                    )
                )
    return tuple(findings)
