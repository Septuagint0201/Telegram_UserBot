"""Identifier factory port."""

from typing import Protocol, TypeVar, runtime_checkable

from telegram_userbot.domain.shared.ids import EntityId

EntityIdT = TypeVar("EntityIdT", bound=EntityId)


@runtime_checkable
class IdFactory(Protocol):
    def new(self, identifier_type: type[EntityIdT]) -> EntityIdT: ...
