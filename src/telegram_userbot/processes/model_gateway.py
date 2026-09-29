"""Production model composition with durable snapshots and no transaction over HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid7

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_userbot.adapters.llm import (
    CanonicalContent,
    CanonicalGenerationRequest,
    CanonicalMessage,
    CanonicalProtocolClient,
    ContentKind,
    ProviderHTTPTransport,
    ProviderProtocolError,
    ProviderTransport,
)
from telegram_userbot.adapters.media import PrivateMediaStore
from telegram_userbot.adapters.persistence.model_runtime import (
    ModelRuntimeRepository,
    ModelRuntimeSnapshotError,
    PreparedModelGeneration,
    RuntimeEndpointSnapshot,
    RuntimeImageSnapshot,
)
from telegram_userbot.application.ports.model import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)
from telegram_userbot.domain.context import render_data_boundary
from telegram_userbot.domain.model_config import GENERATION_ROLES, LogicalRole, ModelCapabilities
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.crypto import CredentialCryptoError, CredentialKeyring
from telegram_userbot.platform.network import (
    EndpointPolicyError,
    HostResolver,
    PublicEndpointPolicy,
    SystemHostResolver,
    ValidatedEndpoint,
    validate_endpoint,
)
from telegram_userbot.platform.network.endpoint_policy import EndpointPolicy


class EndpointPolicyResolver(Protocol):
    def resolve(self, endpoint: RuntimeEndpointSnapshot) -> EndpointPolicy: ...


class ProviderTransportFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: ValidatedEndpoint,
        policy: EndpointPolicy,
        resolver: HostResolver,
    ) -> ProviderTransport: ...


class RuntimeImageLoader(Protocol):
    async def load(self, image: RuntimeImageSnapshot) -> SensitiveValue[bytes]: ...


class SnapshotEndpointPolicyResolver:
    """Rebuild the immutable public policy captured with the model run."""

    def resolve(self, endpoint: RuntimeEndpointSnapshot) -> EndpointPolicy:
        if endpoint.network_category != "public":
            raise EndpointPolicyError("MODEL_ENDPOINT_POLICY_UNSUPPORTED")
        return PublicEndpointPolicy(
            endpoint.network_policy_id,
            endpoint.network_policy_version,
        )


class PrivateMediaRuntimeImageLoader:
    """Read a provider copy through the private media store's path boundary."""

    def __init__(self, store: PrivateMediaStore | Path) -> None:
        self._store = store

    def _read(self, image: RuntimeImageSnapshot) -> bytes:
        # Worker has a read-only mount. Resolve lazily without mkdir/chmod or a
        # quota lock; the app remains the owner of media initialization.
        store = (
            PrivateMediaStore(self._store, initialize=False)
            if isinstance(self._store, Path)
            else self._store
        )
        return store.read_verified(
            storage_key=image.storage_key, expected_sha256=image.sha256, max_bytes=image.byte_size
        )

    async def load(self, image: RuntimeImageSnapshot) -> SensitiveValue[bytes]:
        try:
            payload = await asyncio.to_thread(self._read, image)
        except OSError, ValueError:
            raise ModelGatewayError("MODEL_IMAGE_SNAPSHOT_UNAVAILABLE") from None
        return SensitiveValue(payload)


def default_transport_factory(
    *, endpoint: ValidatedEndpoint, policy: EndpointPolicy, resolver: HostResolver
) -> ProviderTransport:
    return ProviderHTTPTransport(endpoint=endpoint, policy=policy, resolver=resolver)


def build_production_model_gateway(  # noqa: PLR0913 - production seams are explicit
    sessions: async_sessionmaker[AsyncSession],
    *,
    keyring: CredentialKeyring,
    input_hmac_key: SensitiveValue[bytes],
    media_root: Path,
    resolver: HostResolver | None = None,
    transport_factory: ProviderTransportFactory = default_transport_factory,
) -> ProductionModelGateway:
    """Build the reviewed public-provider gateway used by app and worker code."""

    return ProductionModelGateway(
        sessions,
        keyring=keyring,
        input_hmac_key=input_hmac_key,
        endpoint_policies=SnapshotEndpointPolicyResolver(),
        resolver=resolver or SystemHostResolver(),
        image_loader=PrivateMediaRuntimeImageLoader(PrivateMediaStore(media_root)),
        new_uuid=uuid7,
        transport_factory=transport_factory,
    )


class ProductionModelGateway:
    """Prepare exactly once, then retry the same canonical request outside SQL."""

    def __init__(  # noqa: PLR0913 - explicit security boundaries are injectable
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        keyring: CredentialKeyring,
        input_hmac_key: SensitiveValue[bytes],
        endpoint_policies: EndpointPolicyResolver,
        resolver: HostResolver,
        image_loader: RuntimeImageLoader,
        new_uuid: Callable[[], UUID],
        transport_factory: ProviderTransportFactory = default_transport_factory,
        max_prepared_runs: int = 256,
    ) -> None:
        if len(input_hmac_key.reveal_for_use()) < 32:
            raise ValueError("model input HMAC key must contain at least 32 bytes")
        if type(max_prepared_runs) is not int or not 1 <= max_prepared_runs <= 4_096:
            raise ValueError("prepared model-run cache limit is invalid")
        self._sessions = sessions
        self._keyring = keyring
        self._input_hmac_key = input_hmac_key
        self._endpoint_policies = endpoint_policies
        self._resolver = resolver
        self._image_loader = image_loader
        self._new_uuid = new_uuid
        self._transport_factory = transport_factory
        self._max_prepared_runs = max_prepared_runs
        self._prepared: OrderedDict[UUID, PreparedModelGeneration] = OrderedDict()
        # Fixed lock striping bounds synchronization state even when hostile or
        # corrupted queue input presents many distinct run IDs.  Equal run IDs
        # always select the same stripe and therefore share one durable prepare.
        self._prepare_locks = tuple(asyncio.Lock() for _ in range(min(max_prepared_runs, 64)))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            run_id = UUID(str(request.run_id))
            expected_role = LogicalRole(request.profile)
        except TypeError, ValueError:
            raise ModelGatewayError("MODEL_PROFILE_MISMATCH") from None
        if expected_role not in GENERATION_ROLES:
            raise ModelGatewayError("MODEL_PROFILE_MISMATCH")
        prepared = await self._prepare(run_id, expected_role=expected_role)
        if (
            prepared.config.logical_role is not expected_role
            or prepared.credential_binding.logical_role is not expected_role
        ):
            raise ModelGatewayError("MODEL_PROFILE_MISMATCH")
        if not isinstance(request.input_hash, str) or not hmac.compare_digest(
            request.input_hash, prepared.orchestration_claim_fingerprint.hex()
        ):
            raise ModelGatewayError("MODEL_INPUT_FINGERPRINT_MISMATCH")
        try:
            policy = self._endpoint_policies.resolve(prepared.endpoint)
            endpoint = validate_endpoint(
                prepared.endpoint.base_url,
                policy=policy,
                resolver=self._resolver,
            )
            self._validate_endpoint_snapshot(prepared.endpoint, endpoint)
            api_key = self._keyring.decrypt(
                prepared.credential_envelope,
                binding=prepared.credential_binding,
            )
            canonical = await self._canonical_request(prepared)
            transport = self._transport_factory(
                endpoint=endpoint,
                policy=policy,
                resolver=self._resolver,
            )
            normalized = await CanonicalProtocolClient(transport).generate(
                config=prepared.config,
                request=canonical,
                api_key=api_key,
                capabilities=prepared.capabilities,
            )
        except ProviderProtocolError as error:
            raise ModelGatewayError(
                error.code,
                retryable=error.retryable,
                http_status=error.http_status,
                retry_after_seconds=error.retry_after_seconds,
                provider_request_id=error.provider_request_id,
                request_may_have_been_sent=error.request_may_have_been_sent,
            ) from None
        except CredentialCryptoError:
            raise ModelGatewayError("MODEL_CREDENTIAL_UNAVAILABLE") from None
        except EndpointPolicyError:
            raise ModelGatewayError("MODEL_ENDPOINT_REJECTED") from None
        except ModelGatewayError:
            raise
        except Exception:
            raise ModelGatewayError("MODEL_PROVIDER_COMPOSITION_FAILED") from None
        text = normalized.text.reveal_for_use()
        return ModelResponse(
            normalized.text,
            hashlib.sha256(text.encode()).hexdigest(),
            normalized.usage.input_tokens,
            normalized.usage.output_tokens,
            normalized.finish_reason,
            normalized.provider_request_id,
            normalized.http_status,
            normalized.retry_after_seconds,
        )

    def invalidate_profile(self, profile_id: UUID, *, minimum_version: int | None = None) -> int:
        """Drop prepared secrets/requests for one changed profile, idempotently.

        ``minimum_version`` is validated for the typed event contract.  Prepared
        snapshots intentionally do not infer ordering from a control-plane
        aggregate version, so any accepted marker evicts every entry for the
        profile; durable manifests remain the retry source of truth.
        """

        if not isinstance(profile_id, UUID) or (
            minimum_version is not None
            and (
                isinstance(minimum_version, bool)
                or not isinstance(minimum_version, int)
                or minimum_version <= 0
            )
        ):
            raise ValueError("model profile invalidation marker is invalid")
        evicted = tuple(
            run_id
            for run_id, prepared in self._prepared.items()
            if prepared.config.profile_id == profile_id
        )
        for run_id in evicted:
            del self._prepared[run_id]
        return len(evicted)

    async def _prepare(
        self, run_id: UUID, *, expected_role: LogicalRole = LogicalRole.MAIN_AI
    ) -> PreparedModelGeneration:
        cached = self._prepared.get(run_id)
        if cached is not None:
            if cached.config.logical_role is not expected_role:
                raise ModelGatewayError("MODEL_PROFILE_MISMATCH")
            self._prepared.move_to_end(run_id)
            return cached
        lock = self._prepare_locks[run_id.int % len(self._prepare_locks)]
        async with lock:
            cached = self._prepared.get(run_id)
            if cached is not None:
                if cached.config.logical_role is not expected_role:
                    raise ModelGatewayError("MODEL_PROFILE_MISMATCH")
                self._prepared.move_to_end(run_id)
                return cached
            try:
                async with self._sessions() as session, session.begin():
                    prepared = await ModelRuntimeRepository(
                        session, new_uuid=self._new_uuid
                    ).prepare_or_load_generation(
                        run_id=run_id,
                        expected_logical_role=expected_role,
                        input_hmac_key=self._input_hmac_key,
                        now=_utc_now(),
                    )
            except ModelRuntimeSnapshotError as error:
                raise ModelGatewayError(error.code) from None
            if (
                prepared.config.logical_role is not expected_role
                or prepared.credential_binding.logical_role is not expected_role
            ):
                raise ModelGatewayError("MODEL_PROFILE_MISMATCH")
            self._prepared[run_id] = prepared
            self._prepared.move_to_end(run_id)
            while len(self._prepared) > self._max_prepared_runs:
                self._prepared.popitem(last=False)
            return prepared

    async def _canonical_request(
        self, prepared: PreparedModelGeneration
    ) -> CanonicalGenerationRequest:
        image_by_id = {image.object_id: image for image in prepared.images}
        messages: list[CanonicalMessage] = []
        for source in prepared.ordered_sources:
            if source.image_detail is None:
                content = CanonicalContent(
                    ContentKind.TEXT,
                    SensitiveValue(render_data_boundary(source)),
                )
            else:
                image = image_by_id.get(source.candidate.source_id)
                if (
                    image is None
                    or source.candidate.source_revision != f"sha256-{image.sha256.hex()}"
                ):
                    raise ModelGatewayError("MODEL_IMAGE_SNAPSHOT_UNAVAILABLE")
                loaded = await self._load_verified_image(prepared, image)
                content = CanonicalContent(
                    ContentKind.IMAGE,
                    source.content,
                    image_detail="auto",
                    image_bytes=loaded,
                    image_mime=image.mime_type,
                )
            messages.append(CanonicalMessage(source.canonical_role, (content,)))
        return CanonicalGenerationRequest(tuple(messages), stream=False)

    async def _load_verified_image(
        self,
        prepared: PreparedModelGeneration,
        image: RuntimeImageSnapshot,
    ) -> SensitiveValue[bytes]:
        """Read around the request build and reject storage TOCTOU or metadata drift."""

        return await load_verified_runtime_image(self._image_loader, image, prepared.capabilities)

    @staticmethod
    def _validate_image_bytes(
        prepared: PreparedModelGeneration,
        image: RuntimeImageSnapshot,
        raw: object,
    ) -> None:
        validate_runtime_image_bytes(image, raw, prepared.capabilities)

    @staticmethod
    def _validate_endpoint_snapshot(
        expected: RuntimeEndpointSnapshot, current: ValidatedEndpoint
    ) -> None:
        if (
            current.policy_id != expected.network_policy_id
            or current.policy_version != expected.network_policy_version
            or current.category != expected.network_category
            or current.base_url != expected.base_url
        ):
            raise ModelGatewayError("MODEL_ENDPOINT_SNAPSHOT_DRIFT")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def validate_runtime_image_bytes(
    image: RuntimeImageSnapshot, raw: object, capabilities: ModelCapabilities
) -> None:
    if (
        not isinstance(raw, bytes)
        or len(raw) != image.byte_size
        or hashlib.sha256(raw).digest() != image.sha256
        or _detected_image_mime(raw) != image.mime_type
        or len(raw) > capabilities.max_image_bytes_per_request
    ):
        raise ModelGatewayError("MODEL_IMAGE_SNAPSHOT_INVALID")


async def load_verified_runtime_image(
    loader: RuntimeImageLoader, image: RuntimeImageSnapshot, capabilities: ModelCapabilities
) -> SensitiveValue[bytes]:
    before = await loader.load(image)
    before_raw = before.reveal_for_use()
    validate_runtime_image_bytes(image, before_raw, capabilities)
    after = await loader.load(image)
    after_raw = after.reveal_for_use()
    validate_runtime_image_bytes(image, after_raw, capabilities)
    if not hmac.compare_digest(before_raw, after_raw):
        raise ModelGatewayError("MODEL_IMAGE_SNAPSHOT_CHANGED")
    return after


def _detected_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = [
    "EndpointPolicyResolver",
    "PrivateMediaRuntimeImageLoader",
    "ProductionModelGateway",
    "ProviderTransportFactory",
    "RuntimeImageLoader",
    "SnapshotEndpointPolicyResolver",
    "build_production_model_gateway",
    "default_transport_factory",
]
