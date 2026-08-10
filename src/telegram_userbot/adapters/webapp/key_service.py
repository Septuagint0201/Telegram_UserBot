"""Atomic one-time Web App API-key mutation service."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid7

from telegram_userbot.adapters.persistence.model_repository import (
    ModelConfigurationRepository,
)
from telegram_userbot.adapters.webapp.auth import (
    LaunchTokenCodec,
    TelegramWebIdentity,
    WebAppAuthenticationError,
)
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialKeyring


@dataclass(slots=True)
class ModelKeyMutationService:
    repository: ModelConfigurationRepository
    token_codec: LaunchTokenCodec
    keyring: CredentialKeyring
    deployment_version: int

    async def mutate(  # noqa: PLR0911,PLR0913 - Web App authentication contract
        self,
        *,
        identity: TelegramWebIdentity,
        launch_token: SensitiveValue[str],
        role: LogicalRole,
        action: str,
        api_key: SensitiveValue[str] | None,
        now: datetime,
    ) -> bool:
        admin_bytes = identity.user_id.to_bytes(8, "big", signed=True)
        attempt_hash = hashlib.sha256(b"model-key-attempt-v1\0" + admin_bytes).digest()
        if not await self.repository.allow_key_attempt(
            principal_hash=attempt_hash,
            now=now,
            limit=5,
            window=timedelta(minutes=15),
            block=timedelta(minutes=15),
        ):
            return False
        role_hash = hashlib.sha256(
            b"model-key-write-v1\0" + admin_bytes + b"\0" + role.value.encode()
        ).digest()
        if not await self.repository.allow_key_attempt(
            principal_hash=role_hash,
            now=now,
            limit=2,
            window=timedelta(minutes=1),
            block=timedelta(minutes=1),
        ):
            return False
        try:
            token_hash = self.token_codec.digest(launch_token)
        except WebAppAuthenticationError:
            return False
        claimed = await self.repository.claim_key_launch(
            token_hash=token_hash,
            admin_id=identity.user_id,
            logical_role=role,
            action=action,
            deployment_version=self.deployment_version,
            now=now,
        )
        if claimed is None:
            return False
        if action == "delete":
            if api_key is not None:
                return False
            deleted = await self.repository.delete_credential(
                profile_id=claimed.profile_id,
                expected_credential_version=claimed.credential_version,
                now=now,
            )
            await self.repository.audit_credential_mutation(
                admin_id=identity.user_id,
                logical_role=role,
                action=action,
                result="success" if deleted else "rejected",
                credential_cas_version=claimed.credential_version + (1 if deleted else 0),
                request_id=uuid7(),
                now=now,
            )
            return deleted
        if api_key is None:
            return False
        credential_version = await self.repository.set_credential(
            profile_id=claimed.profile_id,
            logical_role=role,
            expected_credential_version=claimed.credential_version,
            secret=api_key,
            keyring=self.keyring,
            now=now,
        )
        await self.repository.audit_credential_mutation(
            admin_id=identity.user_id,
            logical_role=role,
            action=action,
            result="success",
            credential_cas_version=credential_version,
            request_id=uuid7(),
            now=now,
        )
        return True
