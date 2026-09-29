"""Control application composition that remains independent of the process root."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.persistence.context_repository import ContextRepository
from telegram_userbot.adapters.persistence.model_repository import ModelConfigurationRepository
from telegram_userbot.adapters.persistence.service_status import RestoreGateRepository
from telegram_userbot.adapters.telegram_bot.context_control import ControlBotContextController
from telegram_userbot.adapters.telegram_bot.context_control_backend import (
    DurableContextControlBackend,
    ExactManifestPreviewRebuilder,
    PreviewGateway,
    PreviewSendRejectedError,
    PreviewSendUnknownError,
)
from telegram_userbot.adapters.telegram_bot.conversation_control import (
    ControlBotConversationController,
)
from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationTargetTokenCodec,
    DurableConversationControlBackend,
)
from telegram_userbot.adapters.telegram_bot.dispatcher import (
    ControlBotDispatcher,
    ServerStatusProvider,
)
from telegram_userbot.adapters.telegram_bot.http import (
    BotMutationState,
    KnownBotMessage,
    TelegramBotAPI,
    TelegramBotIdentity,
)
from telegram_userbot.adapters.telegram_bot.memory_control import MemoryControlController
from telegram_userbot.adapters.telegram_bot.memory_control_backend import (
    DurableMemoryControlBackend,
    MemoryControlTargetTokenCodec,
)
from telegram_userbot.adapters.telegram_bot.model_control import ControlBotModelController
from telegram_userbot.adapters.telegram_bot.model_control_backend import (
    DurableModelControlBackend,
    ModelCapabilityProbe,
    PublicEndpointAdmission,
)
from telegram_userbot.adapters.webapp.auth import (
    LaunchTokenCodec,
    TelegramWebIdentity,
)
from telegram_userbot.adapters.webapp.key_service import ModelKeyMutationService
from telegram_userbot.domain.model_config import LogicalRole, ModelConfigurationError
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialKeyring
from telegram_userbot.platform.health.status import RestoreGateState
from telegram_userbot.platform.network import PublicEndpointPolicy, SystemHostResolver


class UnavailableModelCapabilityProbe:
    """Fail closed until a reviewed provider-specific probe is injected."""

    async def probe(self, **_: object) -> object:
        raise ModelConfigurationError("model capability probe is unavailable")


class TelegramPreviewGateway:
    """Translate Bot mutation certainty into the context preview journal vocabulary."""

    def __init__(self, api: TelegramBotAPI) -> None:
        self._api = api

    async def send_text(self, *, bot_chat_id: int, text: SensitiveValue[str]) -> int:
        result = await self._api.send_message(
            chat_id=bot_chat_id,
            text=text.reveal_for_use(),
        )
        if result.state is BotMutationState.UNKNOWN:
            raise PreviewSendUnknownError
        if result.state is BotMutationState.REJECTED or result.message is None:
            raise PreviewSendRejectedError
        return result.message.message_id

    async def delete_message(self, *, bot_chat_id: int, bot_message_id: int) -> None:
        result = await self._api.delete_message(KnownBotMessage(bot_chat_id, bot_message_id))
        if result.state is BotMutationState.UNKNOWN:
            raise PreviewSendUnknownError
        if result.state is BotMutationState.REJECTED:
            raise PreviewSendRejectedError


class ControlDispatcherFactory:
    """Build all four controllers against one caller-owned database session."""

    def __init__(  # noqa: PLR0913 - application/security dependencies are explicit
        self,
        *,
        api: TelegramBotAPI,
        identity: TelegramBotIdentity,
        admin_ids: frozenset[int],
        account_id: UUID,
        deployment_id: str,
        public_origin: str,
        token_key: SensitiveValue[bytes],
        capability_probe: ModelCapabilityProbe,
        status_provider: ServerStatusProvider,
        deployment_version: int = 1,
    ) -> None:
        self._api = api
        self._identity = identity
        self._admin_ids = admin_ids
        self._account_id = account_id
        self._deployment_id = deployment_id
        self._public_origin = public_origin
        self._capability_probe = capability_probe
        self._status_provider = status_provider
        self._deployment_version = deployment_version
        self._launch_tokens = LaunchTokenCodec(_derive_token_key(token_key, b"launch"))
        self._conversation_tokens = ConversationTargetTokenCodec(
            _derive_token_key(token_key, b"conversation"),
            deployment_id=deployment_id,
        )
        self._memory_tokens = MemoryControlTargetTokenCodec(
            _derive_token_key(token_key, b"memory"),
            deployment_id=deployment_id,
        )
        self._preview_gateway: PreviewGateway = TelegramPreviewGateway(api)

    @property
    def launch_tokens(self) -> LaunchTokenCodec:
        return self._launch_tokens

    def context_backend(self, session: AsyncSession) -> DurableContextControlBackend:
        """Build the Bot-owned preview backend for command or cleanup work."""

        return DurableContextControlBackend(
            repository=ContextRepository(session),
            target_tokens=self._conversation_tokens,
            rebuilder=ExactManifestPreviewRebuilder(session),
            gateway=self._preview_gateway,
            bot_identity=self._identity.username,
        )

    def __call__(self, session: AsyncSession) -> ControlBotDispatcher:
        model_repository = ModelConfigurationRepository(session)
        policy_id = uuid5(
            NAMESPACE_URL,
            f"telegram-userbot:{self._deployment_id}:public-model-endpoints",
        )
        model_backend = DurableModelControlBackend(
            repository=model_repository,
            endpoint_admission=PublicEndpointAdmission(
                repository=model_repository,
                policy=PublicEndpointPolicy(policy_id, 1),
                resolver=SystemHostResolver(),
            ),
            capability_probe=self._capability_probe,
            launch_tokens=self._launch_tokens,
            deployment_version=self._deployment_version,
            commit_boundary=session.commit,
        )
        conversation_backend = DurableConversationControlBackend(
            session=session,
            account_id=self._account_id,
            target_tokens=self._conversation_tokens,
            bot_identity=self._identity.username,
        )
        memory_backend = DurableMemoryControlBackend(
            session=session,
            target_tokens=self._memory_tokens,
        )
        context_backend = self.context_backend(session)
        return ControlBotDispatcher(
            identity=self._identity,
            allowed_admin_ids=self._admin_ids,
            api=self._api,
            model=ControlBotModelController(
                allowed_admin_ids=self._admin_ids,
                backend=model_backend,
                web_app_origin=self._public_origin,
            ),
            conversation=ControlBotConversationController(
                allowed_admin_ids=self._admin_ids,
                backend=conversation_backend,
            ),
            memory=MemoryControlController(
                allowed_admin_ids=self._admin_ids,
                target_tokens=self._conversation_tokens,
                backend=memory_backend,
            ),
            context=ControlBotContextController(
                allowed_admin_ids=self._admin_ids,
                backend=context_backend,
            ),
            status_provider=self._status_provider,
        )


class TransactionalModelKeyMutationPort:
    """Open one authorized transaction for a key-only Web App mutation."""

    def __init__(  # noqa: PLR0913 - admission and durable identities are explicit
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        launch_tokens: LaunchTokenCodec,
        keyring: CredentialKeyring,
        deployment_id: str,
        process_accepting: Callable[[], bool],
        deployment_version: int = 1,
    ) -> None:
        self._sessions = sessions
        self._launch_tokens = launch_tokens
        self._keyring = keyring
        self._deployment_id = deployment_id
        self._process_accepting = process_accepting
        self._deployment_version = deployment_version

    async def mutate(  # noqa: PLR0913 - Web App port contract
        self,
        *,
        identity: TelegramWebIdentity,
        launch_token: SensitiveValue[str],
        role: LogicalRole,
        action: str,
        api_key: SensitiveValue[str] | None,
        now: datetime,
    ) -> bool:
        if not self._process_accepting():
            return False
        async with self._sessions() as session, session.begin():
            gate = await RestoreGateRepository(session).get(self._deployment_id)
            if gate is None or gate.state is not RestoreGateState.OPEN:
                return False
            service = ModelKeyMutationService(
                repository=ModelConfigurationRepository(session),
                token_codec=self._launch_tokens,
                keyring=self._keyring,
                deployment_version=self._deployment_version,
            )
            return await service.mutate(
                identity=identity,
                launch_token=launch_token,
                role=role,
                action=action,
                api_key=api_key,
                now=now,
            )


def _derive_token_key(source: SensitiveValue[bytes], domain: bytes) -> SensitiveValue[bytes]:
    raw = source.reveal_for_use()
    if len(raw) < 32:
        raise ValueError("Control token key is invalid")
    return SensitiveValue(hashlib.sha256(b"control-token-v1\0" + domain + b"\0" + raw).digest())


__all__ = [
    "ControlDispatcherFactory",
    "TelegramPreviewGateway",
    "TransactionalModelKeyMutationPort",
    "UnavailableModelCapabilityProbe",
]
