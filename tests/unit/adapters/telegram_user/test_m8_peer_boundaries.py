from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.peer import (
    TelethonBoundPeerResolver,
    TelethonPeerAdmissionResolver,
    TelethonPeerResolverError,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import TelethonUpdateScope
from telegram_userbot.application.ports.telegram_peer import (
    TelegramMediaBinding,
    TelegramPeerBinding,
    TelegramPrivatePeerObservation,
)
from telegram_userbot.domain.messaging import MediaKind, PeerKind
from telegram_userbot.domain.shared.ids import AccountId, ConversationId

ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000241")
OTHER_ACCOUNT_ID = UUID("01900000-0000-7000-8000-000000000242")
CONVERSATION_ID = UUID("01900000-0000-7000-8000-000000000243")
NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def _observation(**changes: object) -> TelegramPrivatePeerObservation:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "managed_telegram_user_id": 1000,
        "telegram_user_id": 42,
        "access_hash": 99,
        "username": "alice",
        "display_name": "Alice Example",
        "observed_is_contact": True,
        "observed_at": NOW,
    }
    values.update(changes)
    return TelegramPrivatePeerObservation(**values)  # type: ignore[arg-type]


def _binding(**changes: object) -> TelegramPeerBinding:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "telegram_user_id": 42,
        "access_hash": 99,
    }
    values.update(changes)
    return TelegramPeerBinding(**values)  # type: ignore[arg-type]


def _media_binding(**changes: object) -> TelegramMediaBinding:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": UUID("01900000-0000-7000-8000-000000000244"),
        "revision_no": 1,
        "position": 0,
        "kind": MediaKind.PHOTO,
        "opaque_file_reference": "file-ref",
        "declared_mime": "image/jpeg",
        "declared_size": 10,
    }
    values.update(changes)
    return TelegramMediaBinding(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_peer_port_records_accept_valid_values_and_reject_invalid_shapes() -> None:
    assert _observation().telegram_user_id == 42
    assert _binding().access_hash == 99
    assert _media_binding().kind is MediaKind.PHOTO

    observation_cases = (
        ({"account_id": UUID(int=0)}, "account identity"),
        ({"managed_telegram_user_id": 0}, "private peer identity"),
        ({"telegram_user_id": 1000}, "private peer identity"),
        ({"access_hash": True}, "access hash"),
        ({"access_hash": 1 << 63}, "access hash"),
        ({"username": " "}, "username"),
        ({"username": "x" * 65}, "username"),
        ({"username": " alice"}, "username"),
        ({"display_name": " "}, "display name"),
        ({"display_name": "x" * 256}, "display name"),
        ({"display_name": " Alice"}, "display name"),
        ({"observed_is_contact": 1}, "boolean"),
        ({"observed_at": NOW.replace(tzinfo=None)}, "aware"),
    )
    for observation_changes, message in observation_cases:
        with pytest.raises((ValueError, TypeError), match=message):
            _observation(**observation_changes)

    binding_cases = (
        ({"account_id": UUID(int=0)}, "scope"),
        ({"conversation_id": UUID(int=0)}, "scope"),
        ({"telegram_user_id": 0}, "identity"),
        ({"access_hash": True}, "access hash"),
    )
    for binding_changes, message in binding_cases:
        with pytest.raises(ValueError, match=message):
            _binding(**binding_changes)

    media_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"message_id": UUID(int=0)}, "scope"),
        ({"revision_no": 0}, "source"),
        ({"position": -1}, "source"),
        ({"kind": MediaKind.VIDEO}, "source"),
        ({"opaque_file_reference": ""}, "reference"),
        ({"declared_mime": "text/plain"}, "MIME"),
        ({"declared_size": -1}, "declared size"),
    )
    for media_changes, message in media_cases:
        with pytest.raises(ValueError, match=message):
            _media_binding(**media_changes)


def _resolver(
    *,
    client: Any | None = None,
    existing: Any | None = None,
    deleted: Any | None = None,
    admit: Any | None = None,
) -> TelethonPeerAdmissionResolver:
    return TelethonPeerAdmissionResolver(
        client=client or AsyncMock(),
        account_id=ACCOUNT_ID,
        managed_telegram_user_id=1000,
        admit_private=cast(
            Callable[[TelegramPrivatePeerObservation], Awaitable[TelegramPeerBinding]],
            admit or AsyncMock(return_value=_binding()),
        ),
        lookup_existing=cast(
            Callable[[int], Awaitable[TelegramPeerBinding | None]],
            existing or AsyncMock(return_value=None),
        ),
        lookup_deleted_message=cast(
            Callable[[int], Awaitable[TelegramPeerBinding | None]],
            deleted or AsyncMock(return_value=None),
        ),
        now=lambda: NOW,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admission_resolver_handles_non_private_deleted_and_special_peers() -> None:
    resolver = _resolver()
    assert (
        await resolver(TelethonUpdateScope(42, 1000, 1, PeerKind.SELF))
    ).peer_kind is PeerKind.SELF
    assert (
        await resolver(TelethonUpdateScope(-100, -100, 2, PeerKind.GROUP))
    ).peer_kind is PeerKind.GROUP
    assert (
        await resolver(TelethonUpdateScope(-200, -200, 3, PeerKind.CHANNEL))
    ).peer_kind is PeerKind.CHANNEL
    assert (
        await resolver(TelethonUpdateScope(None, None, 4, PeerKind.UNKNOWN))
    ).peer_kind is PeerKind.UNKNOWN

    deleted = AsyncMock(return_value=_binding())
    deleted_resolver = _resolver(deleted=deleted)
    deleted_admission = await deleted_resolver(TelethonUpdateScope(None, None, 5, PeerKind.UNKNOWN))
    assert deleted_admission.conversation_id == CONVERSATION_ID
    deleted.assert_awaited_once_with(5)

    unsupported = await resolver(TelethonUpdateScope(42, 42, 6, PeerKind.UNKNOWN))
    assert unsupported.peer_kind is PeerKind.UNKNOWN
    assert (
        await resolver(TelethonUpdateScope(0, 0, 7, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.UNKNOWN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admission_resolver_validates_telethon_entity_before_persisting() -> None:
    entity_client = AsyncMock()
    admit = AsyncMock(return_value=_binding())
    resolver = _resolver(client=entity_client, admit=admit)

    entity_client.get_entity.side_effect = RuntimeError("lookup failed")
    assert (
        await resolver(TelethonUpdateScope(42, 42, 10, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.UNKNOWN

    entity_client.get_entity.side_effect = None
    entity_client.get_entity.return_value = types.User(id=43, access_hash=99)
    assert (
        await resolver(TelethonUpdateScope(42, 42, 11, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.UNKNOWN

    entity_client.get_entity.return_value = types.User(id=42, access_hash=99, bot=True)
    assert (
        await resolver(TelethonUpdateScope(42, 42, 12, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.BOT

    entity_client.get_entity.return_value = types.User(id=42, access_hash=99, is_self=True)
    assert (
        await resolver(TelethonUpdateScope(42, 42, 13, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.SELF

    entity_client.get_entity.return_value = types.User(id=42, access_hash=None)
    assert (
        await resolver(TelethonUpdateScope(42, 42, 14, PeerKind.PRIVATE_USER))
    ).peer_kind is PeerKind.UNKNOWN

    entity_client.get_entity.return_value = types.User(
        id=42,
        access_hash=99,
        username=" alice ",
        first_name=" Alice ",
        last_name=" Example ",
        contact=True,
    )
    admission = await resolver(TelethonUpdateScope(42, 42, 15, PeerKind.PRIVATE_USER))
    assert admission.peer_kind is PeerKind.PRIVATE_USER
    admit.assert_awaited_once()
    assert admit.await_args is not None
    observed = admit.await_args.args[0]
    assert observed.username == "alice"
    assert observed.display_name == "Alice Example"
    assert observed.observed_is_contact


@pytest.mark.unit
@pytest.mark.asyncio
async def test_binding_scope_errors_and_outbound_bound_resolver_are_fail_closed() -> None:
    resolver = _resolver(existing=AsyncMock(return_value=_binding(account_id=OTHER_ACCOUNT_ID)))
    with pytest.raises(TelethonPeerResolverError, match="ACCOUNT_MISMATCH"):
        await resolver(TelethonUpdateScope(42, 42, 20, PeerKind.PRIVATE_USER))

    resolver = _resolver(existing=AsyncMock(return_value=_binding(telegram_user_id=43)))
    with pytest.raises(TelethonPeerResolverError, match="IDENTITY_MISMATCH"):
        await resolver(TelethonUpdateScope(42, 42, 21, PeerKind.PRIVATE_USER))

    missing = TelethonBoundPeerResolver(
        cast(
            Callable[[UUID, UUID], Awaitable[TelegramPeerBinding | None]],
            AsyncMock(return_value=None),
        )
    )
    with pytest.raises(TelethonPeerResolverError, match="NOT_BOUND"):
        await missing(AccountId(ACCOUNT_ID), ConversationId(CONVERSATION_ID))

    wrong_scope = TelethonBoundPeerResolver(
        cast(
            Callable[[UUID, UUID], Awaitable[TelegramPeerBinding | None]],
            AsyncMock(return_value=_binding(account_id=OTHER_ACCOUNT_ID)),
        )
    )
    with pytest.raises(TelethonPeerResolverError, match="SCOPE_MISMATCH"):
        await wrong_scope(AccountId(ACCOUNT_ID), ConversationId(CONVERSATION_ID))

    bound = TelethonBoundPeerResolver(
        cast(
            Callable[[UUID, UUID], Awaitable[TelegramPeerBinding | None]],
            AsyncMock(return_value=_binding()),
        )
    )
    peer = await bound(AccountId(ACCOUNT_ID), ConversationId(CONVERSATION_ID))
    assert isinstance(peer, types.InputPeerUser)
    assert peer.user_id == 42
    assert peer.access_hash == 99
