"""Generation and embedding ports; provider adapters start in M2."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from telegram_userbot.domain.shared.ids import RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: RunId
    profile: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: SensitiveValue[str]
    output_hash: str


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
