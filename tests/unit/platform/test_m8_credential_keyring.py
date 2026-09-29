import base64
import json

import pytest

from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialCryptoError, parse_credential_keyring


def _key(value: bytes = b"a" * 32) -> str:
    return base64.b64encode(value).decode("ascii")


def _payload(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "synthetic-deployment",
        "active_key_version": 2,
        "keys": {"1": _key(b"a" * 32), "2": _key(b"b" * 32)},
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.mark.unit
def test_keyring_parser_accepts_versioned_deployment_bound_secret() -> None:
    parsed = parse_credential_keyring(
        SensitiveValue(_payload()), expected_deployment_id="synthetic-deployment"
    )

    assert parsed.active_key_version == 2
    first_runtime_key = parsed.derive_runtime_key(b"control-tokens").reveal_for_use()
    assert len(first_runtime_key) == 32
    assert first_runtime_key == parsed.derive_runtime_key(b"control-tokens").reveal_for_use()
    assert first_runtime_key != parsed.derive_runtime_key(b"other-domain").reveal_for_use()
    assert "synthetic-deployment" not in repr(parsed)
    assert _key(b"b" * 32) not in repr(parsed)
    assert _key(b"b" * 32) not in str(parsed)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "CREDENTIAL_KEYRING_SOURCE_INVALID"),
        (_payload() + b"\n", "CREDENTIAL_KEYRING_SOURCE_INVALID"),
        (b"{", "CREDENTIAL_KEYRING_JSON_INVALID"),
        (
            b'{"schema_version":1,"schema_version":1,"deployment_id":"synthetic-deployment",'
            b'"active_key_version":1,"keys":{"1":"' + _key().encode() + b'"}}',
            "CREDENTIAL_KEYRING_JSON_INVALID",
        ),
        (
            b'{"schema_version":1,"deployment_id":"synthetic-deployment",'
            b'"active_key_version":1,"keys":{"1":"'
            + _key().encode()
            + b'","1":"'
            + _key(b"b" * 32).encode()
            + b'"}}',
            "CREDENTIAL_KEYRING_JSON_INVALID",
        ),
        (
            _payload(extra=True),
            "CREDENTIAL_KEYRING_FIELDS_INVALID",
        ),
        (
            _payload(schema_version=2),
            "CREDENTIAL_KEYRING_VERSION_UNSUPPORTED",
        ),
        (
            _payload(active_key_version=0),
            "CREDENTIAL_KEYRING_ACTIVE_VERSION_INVALID",
        ),
        (
            _payload(active_key_version=3),
            "CREDENTIAL_KEYRING_ACTIVE_KEY_MISSING",
        ),
        (
            _payload(keys={}),
            "CREDENTIAL_KEYRING_KEYS_INVALID",
        ),
        (
            _payload(keys={str(index): _key(bytes([index]) * 32) for index in range(1, 18)}),
            "CREDENTIAL_KEYRING_KEYS_INVALID",
        ),
        (
            _payload(keys={"01": _key()}),
            "CREDENTIAL_KEYRING_KEY_INVALID",
        ),
        (
            _payload(keys={"1": "not-base64"}),
            "CREDENTIAL_KEYRING_KEY_INVALID",
        ),
        (
            _payload(keys={"1": _key(b"short")}),
            "CREDENTIAL_KEYRING_KEY_INVALID",
        ),
    ],
)
def test_keyring_parser_rejects_malformed_secret_content_free(raw: bytes, code: str) -> None:
    with pytest.raises(CredentialCryptoError, match=f"^{code}$") as raised:
        parse_credential_keyring(SensitiveValue(raw), expected_deployment_id="synthetic-deployment")

    rejected_text = raw.decode("utf-8", errors="ignore")
    assert not rejected_text or rejected_text not in str(raised.value)


@pytest.mark.unit
def test_keyring_parser_rejects_deployment_mismatch_without_echoing_identity() -> None:
    with pytest.raises(
        CredentialCryptoError, match=r"^CREDENTIAL_KEYRING_DEPLOYMENT_MISMATCH$"
    ) as raised:
        parse_credential_keyring(
            SensitiveValue(_payload(deployment_id="other-private-deployment")),
            expected_deployment_id="synthetic-deployment",
        )

    assert "other-private-deployment" not in str(raised.value)
    assert "synthetic-deployment" not in str(raised.value)
