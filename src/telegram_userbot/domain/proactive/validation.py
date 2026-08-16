"""Fail-closed validation for the Proactive Agent structured response."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from telegram_userbot.domain.proactive.models import (
    DECISION_CODES,
    AgentDecision,
    Candidate,
    ProactiveAction,
    ProactivePolicy,
)
from telegram_userbot.domain.proactive.time import quiet_decision
from telegram_userbot.domain.shared.time import require_aware


class ProactiveValidationError(ValueError):
    """A provider payload cannot be accepted as a decision."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "decision_code",
        "selected_occurrence_ids",
        "topic",
        "priority",
        "defer_until",
    }
)


def _object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProactiveValidationError("invalid_type", "expected object", path=path)
    return cast(Mapping[str, Any], value)


def _strict_fields(raw: Mapping[str, Any]) -> None:
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ProactiveValidationError(
            "unknown_field", "unknown decision field", path="$." + min(unknown)
        )
    missing = _FIELDS - set(raw)
    if missing:
        raise ProactiveValidationError(
            "missing_field", "decision field is missing", path="$." + min(missing)
        )


def _utc_datetime(value: object, path: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProactiveValidationError("invalid_time", "timestamp must be ISO text", path=path)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProactiveValidationError("invalid_time", "timestamp is malformed", path=path) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProactiveValidationError("invalid_time", "timestamp needs an offset", path=path)
    return result.astimezone(UTC)


def parse_agent_decision(  # noqa: PLR0912 - strict schema branches fail closed
    payload: object,
    *,
    candidate: Candidate,
    now: datetime,
    policy: ProactivePolicy,
) -> AgentDecision:
    current_time = require_aware(now, "now")
    raw = _object(payload, "$")
    _strict_fields(raw)
    if raw["schema_version"] != 1:
        raise ProactiveValidationError(
            "unsupported_schema", "schema_version must be 1", path="$.schema_version"
        )
    try:
        action = ProactiveAction(raw["action"])
    except (TypeError, ValueError) as exc:
        raise ProactiveValidationError(
            "invalid_action", "action is not allowlisted", path="$.action"
        ) from exc
    decision_code = raw["decision_code"]
    if not isinstance(decision_code, str) or decision_code not in DECISION_CODES:
        raise ProactiveValidationError(
            "invalid_decision_code", "decision code is not allowlisted", path="$.decision_code"
        )
    ids = raw["selected_occurrence_ids"]
    if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
        raise ProactiveValidationError(
            "invalid_selection",
            "selected occurrence IDs must be strings",
            path="$.selected_occurrence_ids",
        )
    try:
        selected = tuple(UUID(value) for value in ids)
    except ValueError as exc:
        raise ProactiveValidationError(
            "invalid_selection",
            "selected occurrence ID is malformed",
            path="$.selected_occurrence_ids",
        ) from exc
    candidate_ids = {item.id for item in candidate.occurrences}
    if len(selected) != len(set(selected)) or any(value not in candidate_ids for value in selected):
        raise ProactiveValidationError(
            "selection_out_of_scope",
            "selection is outside sealed candidate",
            path="$.selected_occurrence_ids",
        )
    topic = raw["topic"]
    if topic is not None and (
        not isinstance(topic, str)
        or not topic.strip()
        or len(topic) > 120
        or "\n" in topic
        or "\r" in topic
    ):
        raise ProactiveValidationError(
            "invalid_topic", "topic must be a short single-line code", path="$.topic"
        )
    priority = raw["priority"]
    if (
        isinstance(priority, bool)
        or not isinstance(priority, (int, float))
        or not math.isfinite(float(priority))
        or not 0 <= float(priority) <= 1
    ):
        raise ProactiveValidationError(
            "invalid_priority", "priority must be finite and bounded", path="$.priority"
        )
    defer_until = _utc_datetime(raw["defer_until"], "$.defer_until")
    if action is ProactiveAction.NONE:
        if selected or topic is not None or defer_until is not None or float(priority) != 0:
            raise ProactiveValidationError(
                "none_has_payload", "none cannot carry a selection or topic"
            )
        return AgentDecision(candidate.id, action, decision_code, (), None, 0.0)
    if not selected or topic is None:
        raise ProactiveValidationError(
            "send_missing_payload", "send/defer needs selection and topic"
        )
    if action is ProactiveAction.SEND_NOW and defer_until is not None:
        raise ProactiveValidationError("defer_field_forbidden", "send_now cannot set defer_until")
    if action is ProactiveAction.DEFER_ONCE:
        if defer_until is None or not current_time < defer_until < candidate.window_end_at:
            raise ProactiveValidationError(
                "defer_out_of_window", "defer must remain in candidate window"
            )
        quiet = quiet_decision(
            defer_until,
            timezone_name=candidate.timezone_name,
            policy=policy,
            occurrence=None,
        )
        if quiet.in_absolute_quiet:
            raise ProactiveValidationError(
                "defer_absolute_quiet", "defer cannot target absolute quiet"
            )
    elif defer_until is not None:
        raise ProactiveValidationError(
            "defer_field_forbidden", "defer_until is only valid for defer_once"
        )
    return AgentDecision(
        candidate.id,
        action,
        decision_code,
        selected,
        topic.strip(),
        float(priority),
        defer_until,
        1 if action is ProactiveAction.DEFER_ONCE else 0,
    )


def parse_agent_response_json(
    raw_json: str,
    *,
    candidate: Candidate,
    now: datetime,
    policy: ProactivePolicy,
) -> AgentDecision:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ProactiveValidationError("malformed_json", "provider output is not JSON") from exc
    return parse_agent_decision(payload, candidate=candidate, now=now, policy=policy)
