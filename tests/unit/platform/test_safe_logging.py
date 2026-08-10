import io
import json

import pytest

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.logging.safe import SafeLogger, UnsafeLogFieldError
from telegram_userbot.platform.logging.sentinel import scan_text_for_sentinels
from tests.support.fakes import fixed_utc


@pytest.mark.unit
def test_safe_logger_emits_allowlisted_json() -> None:
    sink = io.StringIO()
    logger = SafeLogger("unit", fixed_utc, sink)
    logger.event("operation_completed", {"status": "ok", "count": 2})
    payload = json.loads(sink.getvalue())
    assert payload == {
        "component": "unit",
        "count": 2,
        "event": "operation_completed",
        "level": "INFO",
        "status": "ok",
        "timestamp": "2030-01-02T03:04:05.000000Z",
    }


@pytest.mark.unit
@pytest.mark.parametrize("field", ["message", "api_key", "prompt", "content_hash"])
def test_safe_logger_rejects_non_allowlisted_or_sensitive_fields(field: str) -> None:
    logger = SafeLogger("unit", fixed_utc, io.StringIO())
    with pytest.raises(UnsafeLogFieldError):
        logger.event("operation_failed", {field: "SYNTHETIC_VALUE"})


@pytest.mark.unit
def test_safe_logger_rejects_wrapped_values_and_sentinel_without_echo() -> None:
    prefix = "TEST_" + "SECRET_" + "DO_NOT_" + "LOG_"
    sentinel = prefix + "UNIT_001"
    logger = SafeLogger("unit", fixed_utc, io.StringIO())
    with pytest.raises(UnsafeLogFieldError) as wrapped_error:
        logger.event("operation_failed", {"status": SensitiveValue("PRIVATE")})
    with pytest.raises(UnsafeLogFieldError) as sentinel_error:
        logger.event("operation_failed", {"status": sentinel})
    assert "PRIVATE" not in str(wrapped_error.value)
    assert sentinel not in str(sentinel_error.value)


@pytest.mark.unit
def test_exception_logs_type_not_exception_message() -> None:
    sink = io.StringIO()
    logger = SafeLogger("unit", fixed_utc, sink)
    private_detail = "SYNTHETIC_PRIVATE_EXCEPTION_DETAIL"
    logger.failure("operation_failed", RuntimeError(private_detail), error_code="FAILED")
    assert private_detail not in sink.getvalue()
    assert json.loads(sink.getvalue())["error_type"] == "RuntimeError"


@pytest.mark.unit
def test_sentinel_scanner_reports_only_location_and_fingerprint() -> None:
    sentinel = "TEST_" + "SECRET_" + "DO_NOT_" + "LOG_SCAN_001"
    findings = scan_text_for_sentinels(
        f"safe\n{sentinel}\n",
        source="artifact.log",
        sentinels=(sentinel,),
    )
    assert len(findings) == 1
    assert findings[0].source == "artifact.log"
    assert findings[0].line == 2
    assert sentinel not in repr(findings[0])


@pytest.mark.unit
def test_sentinel_scanner_rejects_empty_sentinel() -> None:
    with pytest.raises(ValueError, match="empty"):
        scan_text_for_sentinels("safe", source="x", sentinels=("",))
