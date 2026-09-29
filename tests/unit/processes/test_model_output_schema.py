from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import pytest

from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.proactive.models import ProactiveAction
from telegram_userbot.processes import model_output_schema as schemas

ACCOUNT = UUID(int=1)
CONVERSATION = UUID(int=2)
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
OCCURRENCE = UUID(int=3)
OTHER_OCCURRENCE = UUID(int=4)


def _context(
    *, proactive: schemas.ProactiveOutputScope | None = None
) -> schemas.ModelOutputParseContext:
    return schemas.ModelOutputParseContext(ACCOUNT, CONVERSATION, NOW, proactive)


def _scope(
    *,
    window_end_at: datetime | None = None,
    timezone_name: str = "Europe/Berlin",
    start: time = time(0, 0),
    end: time = time(7, 0),
) -> schemas.ProactiveOutputScope:
    return schemas.ProactiveOutputScope(
        candidate_id=UUID(int=5),
        occurrence_ids=frozenset({OCCURRENCE, OTHER_OCCURRENCE}),
        window_end_at=window_end_at or NOW + timedelta(hours=4),
        timezone_name=timezone_name,
        absolute_no_send_start_local=start,
        absolute_no_send_end_local=end,
    )


def _proactive(  # noqa: PLR0913 - compact fixture builder mirrors the closed output contract
    *,
    action: str = "send_now",
    decision_code: str = "timely_support",
    selected: list[UUID] | None = None,
    topic: str | None = "check in",
    priority: float = 0.7,
    defer_until: datetime | str | None = None,
) -> str:
    value = {
        "schema_version": 1,
        "action": action,
        "decision_code": decision_code,
        "selected_occurrence_ids": [
            str(item) for item in ([OCCURRENCE] if selected is None else selected)
        ],
        "topic": topic,
        "priority": priority,
        "defer_until": (
            defer_until.isoformat() if isinstance(defer_until, datetime) else defer_until
        ),
    }
    return json.dumps(value)


def _memory(*, schema_version: int = 1) -> str:
    return json.dumps(
        {
            "schema_version": schema_version,
            "proposals": [
                {
                    "operation": "create",
                    "memory_type": "preference",
                    "semantic_key": "beverage preference",
                    "payload": {"value": "tea"},
                    "confidence": 0.9,
                    "importance": 0.7,
                    "evidence": [
                        {
                            "source_id": str(UUID(int=10)),
                            "source_revision": "revision-1",
                            "source_content_sha256": "a" * 64,
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.unit
def test_registry_resolves_allowed_purpose_and_rejects_invalid_specs() -> None:
    spec = schemas.ModelOutputSchemaSpec(
        LogicalRole.MAIN_AI,
        1,
        lambda raw, _: raw,
        allowed_purposes=frozenset({"test"}),
    )
    registry = schemas.ModelOutputSchemaRegistry((spec,))
    assert registry.resolve(LogicalRole.MAIN_AI, 1, purpose="test") is spec
    with pytest.raises(schemas.ModelOutputSchemaError, match="UNSUPPORTED"):
        registry.resolve(LogicalRole.MAIN_AI, 1, purpose="other")
    with pytest.raises(schemas.ModelOutputSchemaError, match="UNSUPPORTED"):
        registry.resolve(LogicalRole.MAIN_AI, True, purpose="test")
    with pytest.raises(schemas.ModelOutputSchemaError, match="UNSUPPORTED"):
        registry.resolve(LogicalRole.MAIN_AI, "1", purpose="test")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="registry is invalid"):
        schemas.ModelOutputSchemaRegistry((spec, spec))
    with pytest.raises(ValueError, match="registry is invalid"):
        schemas.ModelOutputSchemaRegistry((spec.__class__(LogicalRole.MAIN_AI, 0, lambda r, c: r),))


@pytest.mark.unit
def test_schema_spec_wraps_unexpected_parser_errors_but_preserves_schema_errors() -> None:
    spec = schemas.ModelOutputSchemaSpec(LogicalRole.MAIN_AI, 1, lambda _raw, _ctx: 1 / 0)
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        spec.parse("x", context=_context())

    def fail(_raw: str, _ctx: schemas.ModelOutputParseContext) -> object:
        raise schemas.ModelOutputSchemaError("specific")

    with pytest.raises(schemas.ModelOutputSchemaError, match="specific"):
        schemas.ModelOutputSchemaSpec(LogicalRole.MAIN_AI, 1, fail).parse("x", context=_context())


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "   ", "x\x00y"])
def test_main_and_proactive_final_text_parsers_reject_invalid_lengths(raw: str) -> None:
    for purpose, version in (("conversation_reply", 1), ("proactive_final", 2)):
        spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
            LogicalRole.MAIN_AI, version, purpose=purpose
        )
        with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
            spec.parse(raw, context=_context())


@pytest.mark.unit
def test_main_text_parser_rejects_oversized_output() -> None:
    raw = "x" * 2_000_001
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MAIN_AI, 1, purpose="conversation_reply"
    )
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        spec.parse(raw, context=_context())


@pytest.mark.unit
def test_main_and_proactive_final_text_parsers_accept_bounded_text() -> None:
    text = "  hello\n"
    assert (
        schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
            LogicalRole.MAIN_AI, 1, purpose="conversation_reply"
        ).parse(text, context=_context())
        == text
    )
    assert (
        schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
            LogicalRole.MAIN_AI, 2, purpose="proactive_final"
        ).parse("final", context=_context())
        == "final"
    )
    with pytest.raises(schemas.ModelOutputSchemaError):
        schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
            LogicalRole.MAIN_AI, 1, purpose="proactive_final"
        )


@pytest.mark.unit
def test_memory_output_and_consolidation_are_strict_and_context_bound() -> None:
    memory = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 1, purpose="memory_episode"
    )
    parsed = memory.parse(_memory(), context=_context())
    assert isinstance(parsed, tuple)
    assert len(parsed) == 1

    consolidation = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 3, purpose="memory_consolidation"
    )
    consolidated = consolidation.parse(_memory(schema_version=3), context=_context())
    assert isinstance(consolidated, tuple)
    assert len(consolidated) == 1

    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        memory.parse('{"proposals": []}', context=_context())
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        memory.parse('{"schema_version":1,"schema_version":1,"proposals":[]}', context=_context())
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        memory.parse('{"schema_version":1,"proposals":[NaN]}', context=_context())
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        consolidation.parse(_memory(schema_version=1), context=_context())


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "summary_text": "summary", "no_change_reason": None},
        {"schema_version": 2, "summary_text": None, "no_change_reason": "unchanged"},
    ],
)
def test_memory_summary_accepts_exact_one_nonempty_field(payload: dict[str, object]) -> None:
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 2, purpose="memory_rolling_summary"
    )
    assert spec.parse(json.dumps(payload), context=_context()) == payload


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "summary_text": "summary", "no_change_reason": None},
        {"schema_version": 2, "summary_text": "summary", "no_change_reason": "reason"},
        {"schema_version": 2, "summary_text": None, "no_change_reason": None},
        {"schema_version": 2, "summary_text": " ", "no_change_reason": None},
        {"schema_version": 2, "summary_text": "summary", "no_change_reason": " "},
        {"schema_version": 2, "summary_text": 1, "no_change_reason": None},
        {"schema_version": 2, "summary_text": None, "no_change_reason": 1},
        {"schema_version": 2, "summary_text": "x" * 100_001, "no_change_reason": None},
    ],
)
def test_memory_summary_rejects_wrong_shape(payload: dict[str, object]) -> None:
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.MEMORY_AGENT, 2, purpose="memory_rolling_summary"
    )
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        spec.parse(json.dumps(payload), context=_context())


@pytest.mark.unit
def test_proactive_none_send_and_defer_decisions_validate_scope() -> None:
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.PROACTIVE_AGENT, 1, purpose="proactive_decision"
    )
    context = _context(proactive=_scope())
    none = json.loads(
        _proactive(action=ProactiveAction.NONE.value, topic=None, priority=0, selected=[])
    )
    assert spec.parse(json.dumps(none), context=context) == none
    send = json.loads(_proactive())
    assert spec.parse(json.dumps(send), context=context) == send
    defer_at = NOW + timedelta(hours=1)
    deferred = json.loads(
        _proactive(
            action=ProactiveAction.DEFER_ONCE.value,
            decision_code="better_later_in_window",
            defer_until=defer_at,
        )
    )
    assert spec.parse(json.dumps(deferred), context=context) == deferred


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p.update(action="bad"),
        lambda p: p.update(decision_code="bad"),
        lambda p: p.update(selected_occurrence_ids=[str(UUID(int=99))]),
        lambda p: p.update(selected_occurrence_ids=[str(OCCURRENCE), str(OCCURRENCE)]),
        lambda p: p.update(selected_occurrence_ids=["not-a-uuid"]),
        lambda p: p.update(topic=" "),
        lambda p: p.update(topic="x\n"),
        lambda p: p.update(topic="x" * 121),
        lambda p: p.update(priority=True),
        lambda p: p.update(priority=2),
        lambda p: p.update(priority=float("nan")),
        lambda p: p.update(defer_until="2030-01-02T12:00:00"),
    ],
)
def test_proactive_rejects_invalid_fields(mutate: object) -> None:
    payload = json.loads(_proactive())
    mutate(payload)  # type: ignore[operator]
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.PROACTIVE_AGENT, 1, purpose="proactive_decision"
    )
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        spec.parse(json.dumps(payload), context=_context(proactive=_scope()))


@pytest.mark.unit
def test_proactive_rejects_context_absence_and_invalid_defer_windows() -> None:
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.PROACTIVE_AGENT, 1, purpose="proactive_decision"
    )
    with pytest.raises(schemas.ModelOutputSchemaError, match="CONTEXT_INVALID"):
        spec.parse(_proactive(), context=_context())

    scope = _scope()
    for defer_until in (
        NOW,
        scope.window_end_at,
        NOW - timedelta(minutes=1),
    ):
        payload = _proactive(
            action="defer_once", decision_code="better_later_in_window", defer_until=defer_until
        )
        with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
            spec.parse(payload, context=_context(proactive=scope))

    # A local deferred time in the absolute no-send interval is invalid.
    in_quiet = datetime(2030, 1, 2, 1, 0, tzinfo=UTC)
    with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
        spec.parse(
            _proactive(
                action="defer_once",
                decision_code="better_later_in_window",
                defer_until=in_quiet,
            ),
            context=_context(proactive=_scope(window_end_at=NOW + timedelta(days=1))),
        )

    with pytest.raises(schemas.ModelOutputSchemaError, match="CONTEXT_INVALID"):
        spec.parse(
            _proactive(
                action="defer_once",
                decision_code="better_later_in_window",
                defer_until=NOW + timedelta(hours=1),
            ),
            context=_context(proactive=_scope(timezone_name="Not/AZone")),
        )


@pytest.mark.unit
def test_proactive_action_invariants_and_time_window_wraparound() -> None:
    spec = schemas.DEFAULT_MODEL_OUTPUT_SCHEMAS.resolve(
        LogicalRole.PROACTIVE_AGENT, 1, purpose="proactive_decision"
    )
    scope = _scope(start=time(22), end=time(7))
    context = _context(proactive=scope)
    invalid = [
        _proactive(action="none", topic="topic", selected=[], priority=0),
        _proactive(action="none", topic=None, selected=[OCCURRENCE], priority=0),
        _proactive(action="send_now", defer_until=NOW + timedelta(hours=1)),
        _proactive(action="defer_once", decision_code="better_later_in_window"),
        _proactive(action="send_now", selected=[]),
        _proactive(action="send_now", topic=None),
    ]
    for raw in invalid:
        with pytest.raises(schemas.ModelOutputSchemaError, match="SCHEMA_INVALID"):
            spec.parse(raw, context=context)
    # Exercise the wraparound branch directly through a valid deferred decision.
    at_day = NOW + timedelta(hours=1)
    valid = _proactive(
        action="defer_once",
        decision_code="better_later_in_window",
        defer_until=at_day,
    )
    assert spec.parse(valid, context=_context(proactive=_scope(start=time(22), end=time(7))))
