from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest

from telegram_userbot.domain.shared.time import UtcTimestamp
from telegram_userbot.platform.health import ReadinessPolicy, ServiceName
from telegram_userbot.processes.synthetic_ready import (
    VALIDATION_ACK,
    VALIDATION_ACK_ENV,
    run,
    synthetic_ready_state,
)

NOW = UtcTimestamp(datetime(2026, 8, 23, 12, 0, tzinfo=UTC))


@pytest.mark.unit
@pytest.mark.parametrize("service", list(ServiceName))
def test_synthetic_state_is_ready_but_explicitly_separate_from_production(
    service: ServiceName,
) -> None:
    state = synthetic_ready_state(service, NOW)

    decision = ReadinessPolicy().readiness(state, now=NOW)

    assert decision.healthy is True
    assert state.maintenance is False
    assert VALIDATION_ACK == "explicit-loopback-non-production"


@pytest.mark.unit
def test_synthetic_process_refuses_without_exact_nonproduction_acknowledgement() -> None:
    output = StringIO()
    errors = StringIO()

    result = run(
        ["--service", "app"],
        values={VALIDATION_ACK_ENV: "production"},
        stdout=output,
        stderr=errors,
    )

    assert result == 2
    assert output.getvalue() == ""
    assert errors.getvalue() == "SYNTHETIC_READY_REFUSED\n"
