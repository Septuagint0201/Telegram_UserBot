"""Generation and embedding ports; provider adapters start in M2."""

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.ids import RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue

_SAFE_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_SAFE_FINISH_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


def _validate_attempt_metadata(
    *,
    http_status: int | None,
    retry_after_seconds: int | None,
    provider_request_id: str | None,
) -> None:
    if http_status is not None and (isinstance(http_status, bool) or not 100 <= http_status <= 599):
        raise ValueError("MODEL_HTTP_STATUS_INVALID")
    if retry_after_seconds is not None and (
        isinstance(retry_after_seconds, bool) or not 0 <= retry_after_seconds <= 86_400
    ):
        raise ValueError("MODEL_RETRY_AFTER_INVALID")
    if provider_request_id is not None and (
        _SAFE_PROVIDER_REQUEST_ID.fullmatch(provider_request_id) is None
    ):
        raise ValueError("MODEL_PROVIDER_REQUEST_ID_INVALID")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: RunId
    profile: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: SensitiveValue[str]
    output_hash: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    http_status: int | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.output_hash != sha256(self.text.reveal_for_use().encode()).hexdigest():
            raise ValueError("MODEL_OUTPUT_FINGERPRINT_INVALID")
        if any(
            value is not None and (isinstance(value, bool) or value < 0)
            for value in (self.input_tokens, self.output_tokens)
        ):
            raise ValueError("MODEL_USAGE_INVALID")
        if _SAFE_FINISH_REASON.fullmatch(self.finish_reason) is None:
            raise ValueError("MODEL_FINISH_REASON_INVALID")
        _validate_attempt_metadata(
            http_status=self.http_status,
            retry_after_seconds=self.retry_after_seconds,
            provider_request_id=self.provider_request_id,
        )


class ModelGatewayError(RuntimeError):
    """Content-free provider failure metadata used by durable attempt journals."""

    def __init__(  # noqa: PLR0913 - durable attempt metadata is explicit
        self,
        code: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        provider_request_id: str | None = None,
        request_may_have_been_sent: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = provider_request_id
        self.request_may_have_been_sent = request_may_have_been_sent
        _validate_attempt_metadata(
            http_status=http_status,
            retry_after_seconds=retry_after_seconds,
            provider_request_id=provider_request_id,
        )


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    run_id: RunId
    profile: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    vector: tuple[float, ...]


@runtime_checkable
class ModelGateway(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class EmbeddingGateway(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
