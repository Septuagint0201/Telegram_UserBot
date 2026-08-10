import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from telegram_userbot.domain.shared.hashing import stable_json_bytes, stable_json_sha256
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.result import AppError, Err, EvidenceStatus, Ok
from telegram_userbot.domain.shared.version import Revision, Version


@pytest.mark.property
@given(st.integers(min_value=0, max_value=2**31))
def test_revision_is_monotonic(value: int) -> None:
    revision = Revision(value)
    assert revision.next().value == value + 1
    assert revision.next() > revision


@pytest.mark.unit
def test_versions_and_revisions_reject_invalid_values() -> None:
    for value in (-1, True):
        with pytest.raises(ValueError, match="revision"):
            Revision(value)
    for value in (0, -1, False):
        with pytest.raises(ValueError, match="version"):
            Version(value)


@pytest.mark.unit
def test_result_and_error_types_are_explicit() -> None:
    assert Ok(3).value == 3
    assert Err(AppError("TEMPORARY_FAILURE", retryable=True)).error.retryable
    assert {status.value for status in EvidenceStatus} == {"PASS", "FAIL", "NOT RUN", "BLOCKED"}
    with pytest.raises(ValueError, match="upper-case"):
        AppError("unsafe code")


@pytest.mark.unit
def test_sensitive_value_is_redacted_by_default() -> None:
    sensitive = SensitiveValue("SYNTHETIC_PRIVATE_VALUE")
    assert str(sensitive) == "<redacted>"
    assert repr(sensitive) == "SensitiveValue(<redacted>)"
    assert sensitive.reveal_for_use() == "SYNTHETIC_PRIVATE_VALUE"


@pytest.mark.property
@given(st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), max_size=8))
def test_stable_json_hash_ignores_mapping_insertion_order(value: dict[str, int]) -> None:
    reversed_value = dict(reversed(tuple(value.items())))
    assert stable_json_bytes(value) == stable_json_bytes(reversed_value)
    assert stable_json_sha256(value) == stable_json_sha256(reversed_value)
    assert json.loads(stable_json_bytes(value)) == value
