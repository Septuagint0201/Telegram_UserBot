"""Deterministic disk and media-quota admission bands for runtime gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_MEDIA_QUOTA_BYTES = 10 * 1024**3
MIN_OPERATIONAL_FREE_BYTES = 1024**3


class DiskPressureBand(StrEnum):
    NORMAL = "normal"
    NOTICE = "notice"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DiskAdmission:
    """Content-free policy result for the documented 70/80/90/95 bands."""

    band: DiskPressureBand
    used_percent: int
    media_quota_reached: bool
    allow_low_priority_work: bool
    allow_proactive_work: bool
    allow_media_download: bool
    operational: bool

    @property
    def status_metadata_band(self) -> str:
        """Map the richer runtime policy onto the bounded status vocabulary."""

        if self.band in {DiskPressureBand.NORMAL, DiskPressureBand.NOTICE}:
            return "normal" if self.band is DiskPressureBand.NORMAL else "warning"
        if self.band is DiskPressureBand.DEGRADED:
            return "warning"
        return "critical"


def disk_admission(
    *,
    total_bytes: int,
    available_bytes: int,
    media_bytes: int = 0,
    media_quota_bytes: int = DEFAULT_MEDIA_QUOTA_BYTES,
) -> DiskAdmission:
    """Evaluate capacity without floating-point boundary drift.

    The rounded-down percentage is diagnostic only. Admission comparisons use
    exact integer products so exactly 70/80/90/95 percent enters the new band.
    """

    if (
        type(total_bytes) is not int
        or type(available_bytes) is not int
        or type(media_bytes) is not int
        or type(media_quota_bytes) is not int
        or total_bytes <= 0
        or not 0 <= available_bytes <= total_bytes
        or media_bytes < 0
        or media_quota_bytes <= 0
    ):
        raise ValueError("disk capacity values are invalid")
    used_bytes = total_bytes - available_bytes
    if available_bytes < MIN_OPERATIONAL_FREE_BYTES or used_bytes * 100 >= total_bytes * 95:
        band = DiskPressureBand.BLOCKED
    elif used_bytes * 100 >= total_bytes * 90:
        band = DiskPressureBand.CRITICAL
    elif used_bytes * 100 >= total_bytes * 80:
        band = DiskPressureBand.DEGRADED
    elif used_bytes * 100 >= total_bytes * 70:
        band = DiskPressureBand.NOTICE
    else:
        band = DiskPressureBand.NORMAL

    media_quota_reached = media_bytes >= media_quota_bytes
    allow_low_priority = band in {
        DiskPressureBand.NORMAL,
        DiskPressureBand.NOTICE,
        DiskPressureBand.DEGRADED,
    }
    allow_proactive = allow_low_priority
    allow_media = band not in {DiskPressureBand.CRITICAL, DiskPressureBand.BLOCKED}
    allow_media = allow_media and not media_quota_reached
    return DiskAdmission(
        band=band,
        used_percent=(used_bytes * 100) // total_bytes,
        media_quota_reached=media_quota_reached,
        allow_low_priority_work=allow_low_priority,
        allow_proactive_work=allow_proactive,
        allow_media_download=allow_media,
        operational=band is not DiskPressureBand.BLOCKED,
    )


__all__ = [
    "DEFAULT_MEDIA_QUOTA_BYTES",
    "MIN_OPERATIONAL_FREE_BYTES",
    "DiskAdmission",
    "DiskPressureBand",
    "disk_admission",
]
