"""Calendar policy boundaries and recovery decisions independent of SQL transport."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence.memory_inputs import MemoryPipelineError, message_source
from telegram_userbot.adapters.persistence.memory_periods import (
    MemoryPeriodRepository,
    sources_digest,
)
from telegram_userbot.domain.memory.models import SummaryKind
from telegram_userbot.domain.memory.periods import SummaryPeriod, summary_timezone
from telegram_userbot.processes import memory_periods as runtime
from tests.unit.processes.test_memory_pipeline import NOW, prepared, source_row

pytestmark = pytest.mark.unit
ACCOUNT, CONVERSATION = UUID(int=1), UUID(int=2)
DAY = SummaryPeriod.at(SummaryKind.DAILY, NOW - timedelta(days=7), "UTC")
SOURCE = message_source(source_row())


def result(rows: list[Any]) -> Any:
    value = MagicMock()
    value.mappings.return_value = value
    value.scalars.return_value = value
    value.all.return_value = rows
    value.one_or_none.return_value = rows[0] if rows else None
    return value


@pytest.mark.parametrize(
    ("when", "hours"), [("2026-03-08T12:00:00+00:00", 23), ("2026-11-01T12:00:00+00:00", 25)]
)
def test_dst_snapshot_roundtrip(when: str, hours: int) -> None:
    day = SummaryPeriod.at(SummaryKind.DAILY, datetime.fromisoformat(when), "America/New_York")
    assert day.end + timedelta(microseconds=1) - day.start == timedelta(hours=hours)
    assert SummaryPeriod.parse(day.document()) == day
    assert day.contains(day.start)
    assert not day.contains(day.end + timedelta(microseconds=1))


def test_timezone_precedence_and_iso_year_week() -> None:
    assert summary_timezone("Asia/Tokyo", "UTC", "America/New_York") == "Asia/Tokyo"
    assert summary_timezone(None, "UTC", "Asia/Tokyo") == "UTC"
    assert summary_timezone(None, None, "Asia/Tokyo") == "Asia/Tokyo"
    week = SummaryPeriod.at(SummaryKind.WEEKLY, datetime(2021, 1, 1, tzinfo=UTC), "UTC")
    assert week.key == "2020-12-28"
    assert week.end.date().isoformat() == "2021-01-03"
    with pytest.raises(ValueError, match="calendar summary"):
        SummaryPeriod.at(SummaryKind.ROLLING, NOW, "UTC")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {**DAY.document(), "end": NOW.isoformat()},
        {**DAY.document(), "start": "2026-01-01"},
        {**DAY.document(), "timezone": 1},
    ],
)
def test_snapshot_tampering_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="calendar summary"):
        SummaryPeriod.parse(value)


@pytest.mark.parametrize(
    ("pending", "origin", "latest", "ready"),
    [
        (None, None, NOW - timedelta(minutes=15), True),
        (None, None, None, True),
        (1, None, None, False),
        (None, 1, None, False),
        (None, None, NOW, False),
    ],
)
async def test_quiet_includes_edits_and_unprojected_input(
    pending: Any, origin: Any, latest: Any, ready: bool
) -> None:
    repository = MemoryPeriodRepository(
        AsyncMock(scalar=AsyncMock(side_effect=[pending, origin, latest]))
    )
    assert await repository.ready(CONVERSATION, now=NOW) is ready


async def test_daily_inputs_do_not_expand_to_event_watermark() -> None:
    row = {**source_row(), "source_event_id": 721}
    session = AsyncMock(execute=AsyncMock(return_value=result([row])))
    sources, start, end = await MemoryPeriodRepository(session).daily_sources(
        ACCOUNT, CONVERSATION, DAY
    )
    assert (start, end) == (721, 721)
    assert sources == (SOURCE,)
    statement = str(session.execute.call_args.args[0])
    assert "messages.telegram_created_at BETWEEN" in statement
    # Oversized inputs are partitioned instead of terminating at 1000 rows.
    assert session.execute.call_args.args[0]._limit_clause.value == 33


async def test_week_requires_all_current_nonempty_days() -> None:
    repository = MemoryPeriodRepository(AsyncMock())
    repository.snapshots = AsyncMock(return_value=(DAY,))  # type: ignore[method-assign]
    repository.daily_sources = AsyncMock(return_value=((SOURCE,), 721, 721))  # type: ignore[method-assign]
    repository.current = AsyncMock(return_value=None)  # type: ignore[method-assign]
    week = SummaryPeriod.at(SummaryKind.WEEKLY, DAY.start, "UTC")
    with pytest.raises(MemoryPipelineError, match="MEMORY_DAILY_INCOMPLETE"):
        await repository.inputs(ACCOUNT, CONVERSATION, week)
    repository.current.return_value = {
        "id": UUID(int=8),
        "version_no": 1,
        "content_text": "Synthetic daily",
        "content_sha256": hashlib.sha256(b"Synthetic daily").digest(),
    }
    repository.matches = AsyncMock(return_value=True)  # type: ignore[method-assign]
    repository.daily_sources.side_effect = [((SOURCE,), 721, 721)] + [((), 1, 1)] * 6
    sources, start, end = await repository.inputs(ACCOUNT, CONVERSATION, week)
    assert len(sources) == 1
    assert sources[0].source_type == "summary_version"
    assert (start, end) == (721, 721)


@pytest.mark.parametrize("change", ["missing", "bounds", "digest", "range", "quiet", "ok"])
async def test_job_snapshot_rechecks_membership_and_readiness(change: str) -> None:
    payload = {"summary_period": DAY.document(), "period_sources_sha256": sources_digest((SOURCE,))}
    if change == "bounds":
        payload["summary_period"] = {**DAY.document(), "end": NOW.isoformat()}
    if change == "digest":
        payload["period_sources_sha256"] = "0" * 64
    repository = MemoryPeriodRepository(
        AsyncMock(scalar=AsyncMock(return_value=None if change == "missing" else payload))
    )
    repository.ready = AsyncMock(return_value=change != "quiet")  # type: ignore[method-assign]
    repository.inputs = AsyncMock(return_value=((SOURCE,), 721, 721))  # type: ignore[method-assign]
    row = {
        "background_job_id": UUID(int=4),
        "account_id": ACCOUNT,
        "conversation_id": CONVERSATION,
        "range_start_event_id": 1 if change == "range" else 721,
        "range_end_event_id": 721,
    }
    if change == "ok":
        assert await repository.job_inputs(row, now=NOW) == (DAY, (SOURCE,))
    else:
        with pytest.raises(MemoryPipelineError):
            await repository.job_inputs(row, now=NOW)


async def test_recursive_invalidation_reaches_grandchildren_once() -> None:
    session = AsyncMock(
        execute=AsyncMock(
            side_effect=[
                result([UUID(int=11)]),
                result([UUID(int=12)]),
                result([]),
                result([]),
                result([]),
                result([]),
            ]
        )
    )
    await MemoryPeriodRepository(session).invalidate(UUID(int=10), now=NOW)
    # Both content visibility and existing embeddings are invalidated atomically.
    updates = session.execute.call_args_list[-3:]
    assert [item.args[0].table.name for item in updates] == [
        "summary_versions",
        "summaries",
        "embedding_records",
    ]
    assert set(updates[0].args[0].compile().params["id_1"]) == {UUID(int=i) for i in (10, 11, 12)}


async def test_publish_period_has_bounds_and_no_global_watermark() -> None:
    session = AsyncMock(execute=AsyncMock(return_value=result([])))
    repository = MemoryPeriodRepository(session)
    repository.current = AsyncMock(return_value=None)  # type: ignore[method-assign]
    value, _ = prepared()
    manifest = replace(value.manifest, output_schema_version=2)
    await repository.publish(manifest, DAY, "Synthetic day.", job_id=value.lease.id, now=NOW)
    assert [call.args[0].table.name for call in session.execute.call_args_list] == [
        "summaries",
        "summary_versions",
        "summary_version_sources",
    ]
    fields = session.execute.call_args_list[1].args[0].compile().params
    assert fields["period_start_at"] == DAY.start
    assert fields["timezone_snapshot"] == "UTC"
    assert fields["prompt_version"] == manifest.prompt_version
    with pytest.raises(MemoryPipelineError, match="MEMORY_PERIOD_SUMMARY_REQUIRED"):
        await repository.publish(manifest, DAY, None, job_id=value.lease.id, now=NOW)


async def test_hourly_sweep_advances_after_commit_and_resumes_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock()
    sessions = MagicMock()
    sessions.return_value.__aenter__ = AsyncMock(return_value=session)
    sessions.return_value.__aexit__ = AsyncMock()
    repository = MagicMock(scan=AsyncMock(side_effect=[(1, CONVERSATION), (0, None)]))
    monkeypatch.setattr(runtime, "MemoryPeriodRepository", lambda _: repository)
    publisher = runtime.MemoryPeriodPublisher(sessions, timezone="Asia/Tokyo")
    assert await publisher.publish(now=NOW) == 1
    assert await publisher.publish(now=NOW + timedelta(minutes=1)) == 0
    assert repository.scan.call_args.kwargs["after"] == CONVERSATION
    assert await publisher.publish(now=NOW + timedelta(minutes=2)) == 0
    assert repository.scan.await_count == 2


@pytest.mark.parametrize("gate", ["ready", "busy", "quiet", "empty", "erased"])
async def test_scan_invalidates_stale_output_even_while_generation_waits(gate: str) -> None:
    session = AsyncMock(
        execute=AsyncMock(return_value=result([DAY.start])),
        scalar=AsyncMock(
            side_effect=[
                UUID(int=3) if gate == "erased" else None,
                UUID(int=4) if gate == "busy" else None,
            ]
        ),
    )
    repository = MemoryPeriodRepository(session)
    repository.snapshots = AsyncMock(return_value=(DAY,))  # type: ignore[method-assign]
    repository.ready = AsyncMock(return_value=gate != "quiet")  # type: ignore[method-assign]
    repository.current = AsyncMock(return_value={"id": UUID(int=5)})  # type: ignore[method-assign]
    repository.inputs = AsyncMock(return_value=(() if gate == "empty" else (SOURCE,), 721, 721))  # type: ignore[method-assign]
    repository.matches = AsyncMock(return_value=False)  # type: ignore[method-assign]
    repository.invalidate = AsyncMock()  # type: ignore[method-assign]
    repository.enqueue = AsyncMock(return_value=1)  # type: ignore[method-assign]
    count = await repository.scan_conversation(
        {"id": CONVERSATION, "account_id": ACCOUNT, "timezone": None, "default_timezone": "UTC"},
        now=NOW,
        deployment_timezone="Asia/Tokyo",
    )
    assert count == int(gate == "ready")
    assert repository.invalidate.await_count == (0 if gate == "erased" else 2)
    assert repository.enqueue.await_count == int(gate == "ready")


async def test_new_period_job_contains_only_snapshot_metadata_and_keeps_attempt_budget() -> None:
    session = AsyncMock(scalar=AsyncMock(side_effect=[None, 6]))
    repository = MemoryPeriodRepository(session)
    repository.current = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await repository.enqueue(ACCOUNT, CONVERSATION, DAY, (SOURCE,), 721, 721, now=NOW) == 1
    payload = session.execute.call_args_list[0].args[0].compile().params["payload"]
    assert payload["summary_period"] == DAY.document()
    assert payload["period_sources_sha256"] == sources_digest((SOURCE,))
    assert SOURCE.content not in str(payload)
    row = session.execute.call_args_list[1].args[0].compile().params
    assert row["generation"] == 7
    session.scalar.side_effect = [row["id"]]
    assert await repository.enqueue(ACCOUNT, CONVERSATION, DAY, (SOURCE,), 721, 721, now=NOW) == 0
    assert session.execute.await_count == 2


async def test_published_boundaries_survive_queue_payload_retention() -> None:
    published = {
        "summary_kind": "daily",
        "period_start_at": DAY.start,
        "period_end_at": DAY.end,
        "period_key": DAY.identity,
        "timezone_snapshot": DAY.timezone,
    }
    session = AsyncMock(execute=AsyncMock(side_effect=[result([published]), result([{}])]))
    assert await MemoryPeriodRepository(session).snapshots(CONVERSATION) == (DAY,)
    session.execute.side_effect = [result([]), result([{"summary_period": DAY.document()}] * 2)]
    assert await MemoryPeriodRepository(session).snapshots(CONVERSATION) == (DAY,)
    published["period_key"] = "Asia/Tokyo:1900-01-01"
    session.execute.side_effect = [result([published])]
    with pytest.raises(MemoryPipelineError, match="MEMORY_PERIOD_INVALID"):
        await MemoryPeriodRepository(session).snapshots(CONVERSATION)
