"""Closed generation-output schema registry for production provider calls."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from types import MappingProxyType
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_userbot.domain.memory import validate_response_json
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.proactive.models import DECISION_CODES, ProactiveAction


class ModelOutputSchemaError(ValueError):
    """Content-free output-schema resolution or validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProactiveOutputScope:
    candidate_id: UUID
    occurrence_ids: frozenset[UUID]
    window_end_at: datetime
    timezone_name: str
    absolute_no_send_start_local: time
    absolute_no_send_end_local: time


@dataclass(frozen=True, slots=True)
class ModelOutputParseContext:
    account_id: UUID
    conversation_id: UUID
    now: datetime
    proactive: ProactiveOutputScope | None = None


OutputParser = Callable[[str, ModelOutputParseContext], object]


@dataclass(frozen=True, slots=True)
class ModelOutputSchemaSpec:
    logical_role: LogicalRole
    version: int
    parser: OutputParser
    response_schema: Mapping[str, object] | None = None
    allowed_purposes: frozenset[str] = frozenset()

    def parse(self, raw: str, *, context: ModelOutputParseContext) -> object:
        try:
            return self.parser(raw, context)
        except ModelOutputSchemaError:
            raise
        except Exception:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID") from None


class ModelOutputSchemaRegistry:
    def __init__(self, specs: tuple[ModelOutputSchemaSpec, ...]) -> None:
        by_key = {(item.logical_role, item.version): item for item in specs}
        if len(by_key) != len(specs) or any(item.version <= 0 for item in specs):
            raise ValueError("model output schema registry is invalid")
        self._specs = MappingProxyType(by_key)

    def resolve(
        self, logical_role: LogicalRole, version: int, *, purpose: str
    ) -> ModelOutputSchemaSpec:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_UNSUPPORTED")
        spec = self._specs.get((logical_role, version))
        if spec is None or purpose not in spec.allowed_purposes:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_UNSUPPORTED")
        return spec


def _parse_main_text(raw: str, _: ModelOutputParseContext) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 2_000_000 or "\x00" in raw:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    return raw


def _parse_proactive_final(raw: str, _: ModelOutputParseContext) -> str:
    """Accept exactly one Telegram-sized, non-empty final message."""

    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4_096 or "\x00" in raw:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    return raw


def _parse_memory(raw: str, context: ModelOutputParseContext) -> object:
    # ``validate_response_json`` owns the typed Memory Agent contract, while
    # this first pass closes Python json's duplicate-key/NaN extensions.
    _object(raw)
    return validate_response_json(
        raw,
        account_id=context.account_id,
        conversation_id=context.conversation_id,
    )


def _parse_memory_summary(raw: str, _: ModelOutputParseContext) -> Mapping[str, Any]:
    payload = _object(raw)
    if set(payload) != {"schema_version", "summary_text", "no_change_reason"}:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    if payload["schema_version"] != 2:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    summary = payload["summary_text"]
    reason = payload["no_change_reason"]
    if summary is not None and (
        not isinstance(summary, str) or not summary.strip() or len(summary) > 100_000
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    if reason is not None and (
        not isinstance(reason, str) or not reason.strip() or len(reason) > 120
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    if (summary is None) == (reason is None):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    return payload


def _parse_memory_consolidation(raw: str, context: ModelOutputParseContext) -> object:
    payload = dict(_object(raw))
    if payload.get("schema_version") != 3:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    payload["schema_version"] = 1
    return validate_response_json(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        account_id=context.account_id,
        conversation_id=context.conversation_id,
    )


def _object(raw: str) -> Mapping[str, Any]:
    if not isinstance(raw, str) or len(raw) > 2_000_000 or "\x00" in raw:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError, RecursionError:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID") from None
    if not isinstance(value, Mapping):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    return value


def _parse_proactive(  # noqa: PLR0912, PLR0915 - closed schema branches stay explicit
    raw: str, context: ModelOutputParseContext
) -> Mapping[str, Any]:
    scope = context.proactive
    if scope is None:
        raise ModelOutputSchemaError("MODEL_OUTPUT_CONTEXT_INVALID")
    payload = _object(raw)
    fields = {
        "schema_version",
        "action",
        "decision_code",
        "selected_occurrence_ids",
        "topic",
        "priority",
        "defer_until",
    }
    if set(payload) != fields or payload["schema_version"] != 1:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    try:
        action = ProactiveAction(payload["action"])
    except TypeError, ValueError:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID") from None
    if payload["decision_code"] not in DECISION_CODES:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    raw_ids = payload["selected_occurrence_ids"]
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    try:
        selected = tuple(UUID(item) for item in raw_ids)
    except ValueError:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID") from None
    if len(selected) != len(set(selected)) or any(
        item not in scope.occurrence_ids for item in selected
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    topic = payload["topic"]
    if topic is not None and (
        not isinstance(topic, str)
        or not topic.strip()
        or len(topic) > 120
        or "\n" in topic
        or "\r" in topic
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    priority = payload["priority"]
    if (
        isinstance(priority, bool)
        or not isinstance(priority, (int, float))
        or not math.isfinite(float(priority))
        or not 0 <= float(priority) <= 1
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    defer_until: datetime | None = None
    if payload["defer_until"] is not None:
        if not isinstance(payload["defer_until"], str):
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
        try:
            parsed_defer = datetime.fromisoformat(payload["defer_until"])
        except ValueError, TypeError:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID") from None
        if parsed_defer.tzinfo is None or parsed_defer.utcoffset() is None:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
        defer_until = parsed_defer.astimezone(UTC)
    if action is ProactiveAction.NONE:
        if selected or topic is not None or defer_until is not None or float(priority) != 0:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    elif (
        not selected
        or topic is None
        or (action is ProactiveAction.SEND_NOW and defer_until is not None)
    ):
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    elif action is ProactiveAction.DEFER_ONCE:
        if defer_until is None or not context.now < defer_until < scope.window_end_at:
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
        try:
            local = (
                defer_until.astimezone(ZoneInfo(scope.timezone_name)).time().replace(tzinfo=None)
            )
        except ZoneInfoNotFoundError:
            raise ModelOutputSchemaError("MODEL_OUTPUT_CONTEXT_INVALID") from None
        if _time_in_window(
            local,
            scope.absolute_no_send_start_local,
            scope.absolute_no_send_end_local,
        ):
            raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    elif defer_until is not None:
        raise ModelOutputSchemaError("MODEL_OUTPUT_SCHEMA_INVALID")
    return payload


def _time_in_window(value: time, start: time, end: time) -> bool:
    return start <= value < end if start < end else value >= start or value < end


_PROACTIVE_RESPONSE_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "action",
            "decision_code",
            "selected_occurrence_ids",
            "topic",
            "priority",
            "defer_until",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "action": {"type": "string", "enum": [item.value for item in ProactiveAction]},
            "decision_code": {"type": "string", "enum": sorted(DECISION_CODES)},
            "selected_occurrence_ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "uniqueItems": True,
            },
            "topic": {"type": ["string", "null"], "maxLength": 120},
            "priority": {"type": "number", "minimum": 0, "maximum": 1},
            "defer_until": {"type": ["string", "null"]},
        },
    }
)


DEFAULT_MODEL_OUTPUT_SCHEMAS = ModelOutputSchemaRegistry(
    (
        ModelOutputSchemaSpec(
            LogicalRole.MAIN_AI,
            1,
            _parse_main_text,
            allowed_purposes=frozenset({"conversation_reply", "copilot_reactive_draft"}),
        ),
        ModelOutputSchemaSpec(
            LogicalRole.MAIN_AI,
            2,
            _parse_proactive_final,
            allowed_purposes=frozenset({"proactive_final"}),
        ),
        ModelOutputSchemaSpec(
            LogicalRole.MEMORY_AGENT,
            1,
            _parse_memory,
            allowed_purposes=frozenset({"memory_episode", "memory_reconciliation"}),
        ),
        ModelOutputSchemaSpec(
            LogicalRole.MEMORY_AGENT,
            2,
            _parse_memory_summary,
            allowed_purposes=frozenset({"memory_rolling_summary", "memory_consolidation"}),
        ),
        ModelOutputSchemaSpec(
            LogicalRole.MEMORY_AGENT,
            3,
            _parse_memory_consolidation,
            allowed_purposes=frozenset({"memory_consolidation"}),
        ),
        ModelOutputSchemaSpec(
            LogicalRole.PROACTIVE_AGENT,
            1,
            _parse_proactive,
            _PROACTIVE_RESPONSE_SCHEMA,
            frozenset({"proactive_decision"}),
        ),
    )
)


__all__ = [
    "DEFAULT_MODEL_OUTPUT_SCHEMAS",
    "ModelOutputParseContext",
    "ModelOutputSchemaError",
    "ModelOutputSchemaRegistry",
    "ModelOutputSchemaSpec",
    "ProactiveOutputScope",
]
