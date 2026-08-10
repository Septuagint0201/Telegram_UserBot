"""Opaque identifiers used across domain boundaries."""

from dataclasses import dataclass
from typing import ClassVar, Self
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class EntityId:
    """A UUID whose concrete class prevents accidental cross-entity use."""

    kind: ClassVar[str] = "entity"
    value: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise TypeError("value must be a UUID")

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    @classmethod
    def from_string(cls, raw: str) -> Self:
        return cls(UUID(raw))

    def to_json(self) -> str:
        return str(self.value)

    def __str__(self) -> str:
        return self.to_json()


class AccountId(EntityId):
    kind = "account"


class PeerId(EntityId):
    kind = "peer"


class ConversationId(EntityId):
    kind = "conversation"


class MessageId(EntityId):
    kind = "message"


class RunId(EntityId):
    kind = "run"


class JobId(EntityId):
    kind = "job"


class CorrelationId(EntityId):
    kind = "correlation"
