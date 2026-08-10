import io
import json

import pytest

from telegram_userbot.processes.bootstrap import run


@pytest.mark.unit
def test_bootstrap_validates_and_exits_without_external_service() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = run({}, stdout=stdout, stderr=stderr)
    assert result == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["event"] == "startup_configuration_valid"
    assert payload["network_enabled"] is False


@pytest.mark.unit
def test_bootstrap_failure_is_sanitized() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    private_value = "SYNTHETIC_REJECTED_VALUE"
    result = run({"TUDT_API_KEY": private_value}, stdout=stdout, stderr=stderr)
    assert result == 2
    assert stdout.getvalue() == ""
    assert private_value not in stderr.getvalue()
    assert json.loads(stderr.getvalue())["error_code"] == "CONFIG_INVALID"
