"""Allowlist-only JSON event logging."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TextIO

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import UtcTimestamp


class UnsafeLogFieldError(ValueError):
    pass


_ALLOWED_FIELDS = frozenset(
    {
        "attempt",
        "component",
        "correlation_id",
        "count",
        "duration_ms",
        "environment",
        "error_code",
        "error_type",
        "instance_id",
        "network_enabled",
        "operation",
        "status",
        "version",
    }
)
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "body",
    "caption",
    "content",
    "cookie",
    "credential",
    "key",
    "message",
    "password",
    "prompt",
    "secret",
    "session",
    "text",
    "token",
)
_SENTINEL_PREFIX = "TEST_" + "SECRET_" + "DO_NOT_" + "LOG_"


@dataclass(slots=True)
class SafeLogger:
    component: str
    clock: Callable[[], UtcTimestamp]
    sink: TextIO

    def event(self, event: str, fields: Mapping[str, object] | None = None) -> None:
        if not event or not event.replace("_", "").isalnum() or event.lower() != event:
            raise UnsafeLogFieldError("event name must be lower_snake_case")
        safe_fields = dict(fields or {})
        for key, value in safe_fields.items():
            normalized_key = key.lower()
            if key not in _ALLOWED_FIELDS or any(
                part in normalized_key for part in _FORBIDDEN_KEY_PARTS
            ):
                raise UnsafeLogFieldError(f"log field is not allowlisted: {key}")
            if isinstance(value, SensitiveValue):
                raise UnsafeLogFieldError(f"sensitive value rejected for field: {key}")
            if isinstance(value, str) and _SENTINEL_PREFIX in value:
                raise UnsafeLogFieldError(f"sentinel rejected for field: {key}")
            if not isinstance(value, str | int | float | bool | type(None)):
                raise UnsafeLogFieldError(f"unsupported log value type for field: {key}")

        payload: dict[str, object] = {
            "timestamp": self.clock().to_iso(),
            "level": "INFO",
            "component": self.component,
            "event": event,
        }
        payload.update(safe_fields)
        self.sink.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def failure(self, event: str, error: BaseException, *, error_code: str) -> None:
        self.event(
            event,
            {
                "error_code": error_code,
                "error_type": type(error).__name__,
                "status": "failed",
            },
        )
