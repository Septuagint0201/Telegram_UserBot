from __future__ import annotations

import pytest

from telegram_userbot.platform.health.disk import DiskPressureBand, disk_admission

GIB = 1024**3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("available", "band", "expected"),
    [
        (31 * GIB, DiskPressureBand.NORMAL, (True, True, True, True)),
        (30 * GIB, DiskPressureBand.NOTICE, (True, True, True, True)),
        (20 * GIB, DiskPressureBand.DEGRADED, (True, True, True, True)),
        (10 * GIB, DiskPressureBand.CRITICAL, (False, False, False, True)),
        (5 * GIB, DiskPressureBand.BLOCKED, (False, False, False, False)),
    ],
)
def test_disk_admission_enters_each_exact_policy_band(
    available: int,
    band: DiskPressureBand,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    decision = disk_admission(total_bytes=100 * GIB, available_bytes=available)

    assert decision.band is band
    assert decision.used_percent == 100 - available // GIB
    assert (
        decision.allow_low_priority_work,
        decision.allow_proactive_work,
        decision.allow_media_download,
        decision.operational,
    ) == expected


@pytest.mark.unit
def test_media_quota_closes_only_media_admission_at_exact_limit() -> None:
    decision = disk_admission(
        total_bytes=100 * GIB,
        available_bytes=100 * GIB,
        media_bytes=10,
        media_quota_bytes=10,
    )

    assert decision.band is DiskPressureBand.NORMAL
    assert decision.media_quota_reached
    assert not decision.allow_media_download
    assert decision.allow_low_priority_work
    assert decision.allow_proactive_work
    assert decision.operational


@pytest.mark.unit
def test_less_than_one_gib_free_is_blocked_even_below_95_percent() -> None:
    below_floor = disk_admission(
        total_bytes=10 * GIB,
        available_bytes=GIB - 1,
    )
    exact_floor = disk_admission(
        total_bytes=10 * GIB,
        available_bytes=GIB,
    )

    assert below_floor.band is DiskPressureBand.BLOCKED
    assert not below_floor.operational
    assert exact_floor.band is DiskPressureBand.CRITICAL
    assert exact_floor.operational


@pytest.mark.unit
@pytest.mark.parametrize(
    ("total", "available", "media", "quota"),
    [(0, 0, 0, 1), (100, -1, 0, 1), (100, 101, 0, 1), (True, 1, 0, 1), (1, 1, -1, 1)],
)
def test_disk_admission_rejects_noncanonical_capacity_values(
    total: int,
    available: int,
    media: int,
    quota: int,
) -> None:
    with pytest.raises(ValueError, match="capacity"):
        disk_admission(
            total_bytes=total,
            available_bytes=available,
            media_bytes=media,
            media_quota_bytes=quota,
        )
