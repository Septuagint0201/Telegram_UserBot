from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

import pytest

from telegram_userbot.adapters.persistence.model_repository import (
    ModelConfigurationRepository,
)
from telegram_userbot.adapters.persistence.records import ClaimedKeyLaunchRecord
from telegram_userbot.adapters.webapp import LaunchTokenCodec, ModelKeyMutationService
from telegram_userbot.adapters.webapp.auth import TelegramWebIdentity
from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialKeyring

NOW = datetime(2030, 1, 2, tzinfo=UTC)


class RepositoryFake:
    def __init__(self) -> None:
        self.allow = True
        self.allow_results: list[bool] = []
        self.claim: ClaimedKeyLaunchRecord | None = ClaimedKeyLaunchRecord(
            uuid7(), "main_ai", uuid7(), "missing", 1
        )
        self.deleted = True
        self.set_calls = 0
        self.audit_calls: list[dict[str, object]] = []

    async def allow_key_attempt(self, **_: object) -> bool:
        return self.allow_results.pop(0) if self.allow_results else self.allow

    async def claim_key_launch(self, **_: object) -> ClaimedKeyLaunchRecord | None:
        return self.claim

    async def delete_credential(self, **_: object) -> bool:
        return self.deleted

    async def set_credential(self, **_: object) -> int:
        self.set_calls += 1
        return 2

    async def audit_credential_mutation(self, **values: object) -> None:
        self.audit_calls.append(values)


def service(fake: RepositoryFake) -> tuple[ModelKeyMutationService, SensitiveValue[str]]:
    codec = LaunchTokenCodec(SensitiveValue(b"p" * 32))
    token = codec.issue().token
    return (
        ModelKeyMutationService(
            cast(ModelConfigurationRepository, fake),
            codec,
            CredentialKeyring(
                deployment_id="synthetic",
                active_key_version=1,
                keys={1: SensitiveValue(b"k" * 32)},
            ),
            1,
        ),
        token,
    )


def identity() -> TelegramWebIdentity:
    return TelegramWebIdentity(42, b"q" * 32, NOW)


@pytest.mark.unit
async def test_model_key_service_set_and_delete_paths() -> None:
    fake = RepositoryFake()
    mutation, token = service(fake)
    assert await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="set",
        api_key=SensitiveValue("SYNTHETIC_KEY"),
        now=NOW,
    )
    assert fake.set_calls == 1
    assert fake.audit_calls[0]["result"] == "success"

    fake.claim = ClaimedKeyLaunchRecord(uuid7(), "main_ai", uuid7(), "active", 2)
    assert await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="delete",
        api_key=None,
        now=NOW,
    )
    assert fake.audit_calls[1]["action"] == "delete"


@pytest.mark.unit
async def test_model_key_service_rejects_rate_token_claim_and_action_mismatch() -> None:
    fake = RepositoryFake()
    mutation, token = service(fake)
    fake.allow = False
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="set",
        api_key=SensitiveValue("SYNTHETIC_KEY"),
        now=NOW,
    )
    fake.allow = True
    fake.allow_results = [True, False]
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="set",
        api_key=SensitiveValue("SYNTHETIC_KEY"),
        now=NOW,
    )
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=SensitiveValue("bad"),
        role=LogicalRole.MAIN_AI,
        action="set",
        api_key=SensitiveValue("SYNTHETIC_KEY"),
        now=NOW,
    )
    fake.claim = None
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="set",
        api_key=SensitiveValue("SYNTHETIC_KEY"),
        now=NOW,
    )
    fake.claim = ClaimedKeyLaunchRecord(uuid7(), "main_ai", uuid7(), "active", 2)
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="delete",
        api_key=SensitiveValue("forbidden"),
        now=NOW,
    )
    assert not await mutation.mutate(
        identity=identity(),
        launch_token=token,
        role=LogicalRole.MAIN_AI,
        action="replace",
        api_key=None,
        now=NOW,
    )
