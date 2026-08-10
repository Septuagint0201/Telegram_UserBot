from typing import cast
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from telegram_userbot.domain.shared.ids import AccountId, ConversationId, EntityId, PeerId


@pytest.mark.unit
@given(st.uuids())
def test_identifier_round_trip(value: UUID) -> None:
    identifier = AccountId(value)
    assert AccountId.from_string(identifier.to_json()) == identifier
    assert str(identifier) == str(value)


@pytest.mark.unit
def test_concrete_identifier_types_do_not_compare_equal() -> None:
    value = UUID(int=1)
    assert AccountId(value) != cast(object, PeerId(value))
    assert ConversationId(value).kind == "conversation"


@pytest.mark.unit
def test_identifier_rejects_non_uuid_at_runtime() -> None:
    with pytest.raises(TypeError, match="UUID"):
        EntityId("not-a-uuid")  # type: ignore[arg-type]
