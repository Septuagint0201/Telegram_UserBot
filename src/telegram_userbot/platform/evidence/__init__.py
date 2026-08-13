"""Acceptance evidence model."""

from telegram_userbot.platform.evidence.junit import (
    JUnitStatus,
    evidence_status,
    load_junit_results,
)
from telegram_userbot.platform.evidence.manifest import validate_manifest_semantics

__all__ = [
    "JUnitStatus",
    "evidence_status",
    "load_junit_results",
    "validate_manifest_semantics",
]
