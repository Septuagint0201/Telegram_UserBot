"""Civil-time regression for the maintained, bundled IANA data."""

from datetime import datetime, timedelta
from importlib.resources import files
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.unit
def test_bundled_timezone_keeps_manitoba_on_minus_five_after_october_2026() -> None:
    # IANA 2026e: Manitoba stays at -05 after 2026-10-31. Read the wheel's
    # database explicitly so the test does not inherit the host's tzdata.
    with files("tzdata.zoneinfo").joinpath("America/Winnipeg").open("rb") as handle:
        zone = ZoneInfo.from_file(handle, key="America/Winnipeg")
    assert datetime(2026, 11, 2, 12, tzinfo=zone).utcoffset() == timedelta(hours=-5)
    assert datetime(2027, 1, 15, 12, tzinfo=zone).utcoffset() == timedelta(hours=-5)
