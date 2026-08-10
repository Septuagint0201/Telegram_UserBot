"""AES-256-GCM credential envelope with versioned, deployment-bound AAD."""

import hashlib
import hmac
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


class CredentialCryptoError(RuntimeError):
    """Content-free credential encryption or authentication failure."""


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    """Stable identity fields authenticated with a credential ciphertext."""

    logical_role: LogicalRole
    profile_id: UUID
    credential_id: UUID
    version_no: int

    def __post_init__(self) -> None:
        if self.version_no < 1:
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
        if self.key_version < 1 or len(self.secret_fingerprint) != 32:
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
            if version < 1 or len(raw) != 32:
                raise CredentialCryptoError("credential master key must be 32 bytes")
            normalized[version] = raw
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
        try:
            plaintext = AESGCM(encryption_key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                aad,
            )
            return SensitiveValue(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError) as error:
            raise CredentialCryptoError("credential authentication failed") from error

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
