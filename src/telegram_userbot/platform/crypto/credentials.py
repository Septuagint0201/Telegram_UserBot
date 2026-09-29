"""AES-256-GCM credential envelope with versioned, deployment-bound AAD."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

AAD_SCHEMA_VERSION = 1
ALGORITHM = "aes_256_gcm"
_KEYRING_SCHEMA_VERSION = 1
_MAX_KEYRING_BYTES = 64 * 1024
_MAX_KEY_COUNT = 16
_KEY_VERSION = re.compile(r"[1-9][0-9]{0,9}\Z")
_KEYRING_FIELDS = frozenset({"schema_version", "deployment_id", "active_key_version", "keys"})


class CredentialCryptoError(RuntimeError):
    """Content-free credential encryption or authentication failure."""


def _keyring_error(code: str) -> CredentialCryptoError:
    return CredentialCryptoError(code)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _keyring_error("CREDENTIAL_KEYRING_JSON_INVALID")
        value[key] = item
    return value


def _reject_json_constant(_: str) -> object:
    raise _keyring_error("CREDENTIAL_KEYRING_JSON_INVALID")


def _load_keyring_payload(source: SensitiveValue[bytes]) -> dict[str, object]:
    if not isinstance(source, SensitiveValue):
        raise _keyring_error("CREDENTIAL_KEYRING_SOURCE_INVALID")
    raw = source.reveal_for_use()
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > _MAX_KEYRING_BYTES
        or b"\x00" in raw
        or b"\r" in raw
        or b"\n" in raw
    ):
        raise _keyring_error("CREDENTIAL_KEYRING_SOURCE_INVALID")
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except CredentialCryptoError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        raise _keyring_error("CREDENTIAL_KEYRING_JSON_INVALID") from None
    if not isinstance(payload, dict) or set(payload) != _KEYRING_FIELDS:
        raise _keyring_error("CREDENTIAL_KEYRING_FIELDS_INVALID")
    return payload


def _parse_key_material(value: object) -> dict[int, SensitiveValue[bytes]]:
    if not isinstance(value, dict) or not 1 <= len(value) <= _MAX_KEY_COUNT:
        raise _keyring_error("CREDENTIAL_KEYRING_KEYS_INVALID")
    keys: dict[int, SensitiveValue[bytes]] = {}
    for version_text, encoded in value.items():
        if (
            not isinstance(version_text, str)
            or _KEY_VERSION.fullmatch(version_text) is None
            or int(version_text) > 2**31 - 1
            or not isinstance(encoded, str)
        ):
            raise _keyring_error("CREDENTIAL_KEYRING_KEY_INVALID")
        try:
            encoded_ascii = encoded.encode("ascii")
            key = base64.b64decode(encoded_ascii, validate=True)
        except UnicodeEncodeError, ValueError:
            raise _keyring_error("CREDENTIAL_KEYRING_KEY_INVALID") from None
        if len(key) != 32 or base64.b64encode(key) != encoded_ascii:
            raise _keyring_error("CREDENTIAL_KEYRING_KEY_INVALID")
        keys[int(version_text)] = SensitiveValue(key)
    return keys


def parse_credential_keyring(
    source: SensitiveValue[bytes], *, expected_deployment_id: str
) -> CredentialKeyring:
    """Parse a strict, versioned keyring without exposing rejected material.

    The mounted secret is one UTF-8 JSON line. Key values are canonical Base64
    encodings of exactly 32 bytes; the returned keyring remains deployment-bound.
    """

    payload = _load_keyring_payload(source)
    if payload["schema_version"] != _KEYRING_SCHEMA_VERSION or isinstance(
        payload["schema_version"], bool
    ):
        raise _keyring_error("CREDENTIAL_KEYRING_VERSION_UNSUPPORTED")
    deployment_id = payload["deployment_id"]
    if (
        not isinstance(expected_deployment_id, str)
        or not expected_deployment_id
        or not isinstance(deployment_id, str)
        or deployment_id != expected_deployment_id
    ):
        raise _keyring_error("CREDENTIAL_KEYRING_DEPLOYMENT_MISMATCH")
    active = payload["active_key_version"]
    if type(active) is not int or not 1 <= active <= 2**31 - 1:
        raise _keyring_error("CREDENTIAL_KEYRING_ACTIVE_VERSION_INVALID")
    keys = _parse_key_material(payload["keys"])
    if active not in keys:
        raise _keyring_error("CREDENTIAL_KEYRING_ACTIVE_KEY_MISSING")
    try:
        return CredentialKeyring(
            deployment_id=deployment_id,
            active_key_version=active,
            keys=keys,
        )
    except CredentialCryptoError:
        raise _keyring_error("CREDENTIAL_KEYRING_KEYS_INVALID") from None


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    """Stable identity fields authenticated with a credential ciphertext."""

    logical_role: LogicalRole
    profile_id: UUID
    credential_id: UUID
    version_no: int

    def __post_init__(self) -> None:
        if type(self.version_no) is not int or self.version_no < 1:
            raise CredentialCryptoError("credential version must be positive")


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    key_version: int
    aad_schema_version: int
    secret_fingerprint: bytes = field(repr=False)
    algorithm: str = ALGORITHM

    def __post_init__(self) -> None:
        if self.algorithm != ALGORITHM or self.aad_schema_version != AAD_SCHEMA_VERSION:
            raise CredentialCryptoError("unsupported credential envelope")
        if len(self.nonce) != 12 or len(self.ciphertext) < 16:
            raise CredentialCryptoError("malformed credential envelope")
        if (
            type(self.key_version) is not int
            or self.key_version < 1
            or len(self.secret_fingerprint) != 32
        ):
            raise CredentialCryptoError("malformed credential envelope")


class CredentialKeyring:
    """In-memory keyring; raw keys never appear in repr or serialized state."""

    __slots__ = ("_active_key_version", "_deployment_id", "_keys")

    def __init__(
        self,
        *,
        deployment_id: str,
        active_key_version: int,
        keys: Mapping[int, SensitiveValue[bytes]],
    ) -> None:
        if not deployment_id or len(deployment_id) > 128:
            raise CredentialCryptoError("deployment identity is invalid")
        normalized: dict[int, bytes] = {}
        for version, wrapped in keys.items():
            raw = wrapped.reveal_for_use()
            if type(version) is not int or version < 1 or len(raw) != 32:
                raise CredentialCryptoError("credential master key must be 32 bytes")
            normalized[version] = raw
        if type(active_key_version) is not int:
            raise CredentialCryptoError("active credential key is unavailable")
        if active_key_version not in normalized:
            raise CredentialCryptoError("active credential key is unavailable")
        self._deployment_id = deployment_id
        self._active_key_version = active_key_version
        self._keys = MappingProxyType(normalized)

    def __repr__(self) -> str:
        return (
            "CredentialKeyring(deployment_id=<redacted>, "
            f"active_key_version={self._active_key_version}, key_count={len(self._keys)})"
        )

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    def derive_runtime_key(self, purpose: bytes) -> SensitiveValue[bytes]:
        """Derive a domain-separated runtime secret without exposing key material."""

        if (
            not isinstance(purpose, bytes)
            or not 1 <= len(purpose) <= 128
            or any(value < 0x21 or value > 0x7E for value in purpose)
        ):
            raise CredentialCryptoError("credential runtime key purpose is invalid")
        master = self._keys[self._active_key_version]
        return SensitiveValue(self._derive(master, purpose=b"runtime-key-v1\0" + purpose))

    @staticmethod
    def _derive(master_key: bytes, *, purpose: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"telegram-userbot-model-credential-v1",
            info=purpose,
        ).derive(master_key)

    def _aad(
        self,
        *,
        binding: CredentialBinding,
    ) -> bytes:
        return b"\0".join(
            (
                f"schema={AAD_SCHEMA_VERSION}".encode(),
                f"deployment={self._deployment_id}".encode(),
                f"role={binding.logical_role.value}".encode(),
                f"profile={binding.profile_id}".encode(),
                f"credential={binding.credential_id}".encode(),
                f"version={binding.version_no}".encode(),
            )
        )

    def encrypt(
        self,
        secret: SensitiveValue[str],
        *,
        binding: CredentialBinding,
    ) -> CredentialEnvelope:
        raw_secret = secret.reveal_for_use().encode("utf-8")
        if not raw_secret or len(raw_secret) > 8192 or b"\x00" in raw_secret:
            raise CredentialCryptoError("credential input is invalid")
        master = self._keys[self._active_key_version]
        aad = self._aad(binding=binding)
        nonce = secrets.token_bytes(12)
        encryption_key = self._derive(master, purpose=b"aes-256-gcm")
        fingerprint_key = self._derive(master, purpose=b"secret-fingerprint")
        return CredentialEnvelope(
            ciphertext=AESGCM(encryption_key).encrypt(nonce, raw_secret, aad),
            nonce=nonce,
            key_version=self._active_key_version,
            aad_schema_version=AAD_SCHEMA_VERSION,
            secret_fingerprint=hmac.new(fingerprint_key, raw_secret, hashlib.sha256).digest(),
        )

    def decrypt(
        self,
        envelope: CredentialEnvelope,
        *,
        binding: CredentialBinding,
    ) -> SensitiveValue[str]:
        master = self._keys.get(envelope.key_version)
        if master is None:
            raise CredentialCryptoError("credential key version is unavailable")
        aad = self._aad(binding=binding)
        encryption_key = self._derive(master, purpose=b"aes-256-gcm")
        fingerprint_key = self._derive(master, purpose=b"secret-fingerprint")
        try:
            plaintext = AESGCM(encryption_key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                aad,
            )
        except InvalidTag as error:
            raise CredentialCryptoError("credential authentication failed") from error
        expected_fingerprint = hmac.new(fingerprint_key, plaintext, hashlib.sha256).digest()
        if (
            not plaintext
            or len(plaintext) > 8192
            or b"\x00" in plaintext
            or not hmac.compare_digest(expected_fingerprint, envelope.secret_fingerprint)
        ):
            raise CredentialCryptoError("credential authentication failed")
        try:
            decoded = plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CredentialCryptoError("credential authentication failed") from error
        return SensitiveValue(decoded)

    def rotate(
        self,
        envelope: CredentialEnvelope,
        *,
        old_binding: CredentialBinding,
        new_version_no: int,
    ) -> CredentialEnvelope:
        plaintext = self.decrypt(
            envelope,
            binding=old_binding,
        )
        return self.encrypt(
            plaintext,
            binding=CredentialBinding(
                logical_role=old_binding.logical_role,
                profile_id=old_binding.profile_id,
                credential_id=old_binding.credential_id,
                version_no=new_version_no,
            ),
        )
