from io import StringIO
from unittest.mock import AsyncMock

import pytest

from telegram_userbot.platform.config.production import ProductionSettings
from telegram_userbot.processes import erasure_reconcile


@pytest.mark.unit
def test_offline_erasure_entrypoint_is_bounded_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = object()
    monkeypatch.setattr(ProductionSettings, "load", lambda *_: settings)
    reconcile = AsyncMock(return_value=3)
    monkeypatch.setattr(erasure_reconcile, "reconcile_once", reconcile)
    stdout, stderr = StringIO(), StringIO()
    assert erasure_reconcile.run([], {}, stdout=stdout, stderr=stderr) == 0
    reconcile.assert_awaited_once_with(settings)
    assert stdout.getvalue() == "advanced=3\n"
    reconcile.side_effect = RuntimeError("private database detail")
    assert erasure_reconcile.run([], {}, stdout=stdout, stderr=stderr) == 1
    assert erasure_reconcile.run(["--all"], {}, stdout=stdout, stderr=stderr) == 2
    assert stderr.getvalue() == "ERASURE_RECONCILIATION_FAILED\nERASURE_ARGUMENT_INVALID\n"
