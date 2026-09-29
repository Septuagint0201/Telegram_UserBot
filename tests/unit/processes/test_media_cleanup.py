from io import StringIO
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_userbot.adapters.media.cleanup import DurableMediaCleanupReport
from telegram_userbot.platform.config.production import ProductionSettings
from telegram_userbot.processes import media_cleanup


@pytest.mark.unit
@pytest.mark.parametrize("failed", [0, 1])
def test_cleanup_cli_reports_counts_and_failure_without_starting_app(
    monkeypatch: pytest.MonkeyPatch, failed: int
) -> None:
    settings = object()
    monkeypatch.setattr(ProductionSettings, "load", lambda *_: settings)
    cleanup = AsyncMock(return_value=DurableMediaCleanupReport(2, 1, failed))
    monkeypatch.setattr(media_cleanup, "cleanup_once", cleanup)
    stdout, stderr = StringIO(), StringIO()
    assert media_cleanup.run([], {}, stdout=stdout, stderr=stderr) == failed
    cleanup.assert_awaited_once_with(settings)
    assert stdout.getvalue() == f"deleted=2 missing=1 failed={failed}\n"
    assert stderr.getvalue() == ""


@pytest.mark.unit
def test_cleanup_cli_rejects_arguments_and_redacts_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: Any) -> None:
        raise RuntimeError("synthetic-private-database-detail")

    monkeypatch.setattr(ProductionSettings, "load", fail)
    stdout, stderr = StringIO(), StringIO()
    assert media_cleanup.run(["--unrestricted"], {}, stdout=stdout, stderr=stderr) == 2
    assert media_cleanup.run([], {}, stdout=stdout, stderr=stderr) == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "MEDIA_CLEANUP_ARGUMENT_INVALID\nMEDIA_CLEANUP_FAILED\n"
