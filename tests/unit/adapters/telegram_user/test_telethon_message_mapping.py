from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from telethon import types  # type: ignore[import-untyped]

from telegram_userbot.adapters.telegram_user.normalizer import PeerAdmission
from telegram_userbot.adapters.telegram_user.telethon_runtime import (
    PeerAdmissionResolver,
    TelethonSessionRuntime,
    TelethonSessionRuntimeError,
    TelethonSessionSettings,
)
from telegram_userbot.adapters.telegram_user.telethon_updates import TelethonUpdateScope
from telegram_userbot.domain.messaging import NormalizedTelegramEvent
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000001")


def _runtime(
    callback: Callable[[int, int, datetime], Awaitable[None]] | None,
    tmp_path: Path,
) -> TelethonSessionRuntime:
    settings = TelethonSessionSettings(
        account_id=ACCOUNT_ID,
        telegram_user_id=100,
        session_path=tmp_path / "account.session",
        api_id=12345,
        api_hash=SensitiveValue("0123456789abcdef0123456789abcdef"),
    )
    return TelethonSessionRuntime(
        settings,
        resolve_admission=_UnreachableAdmission(),
        ingest=_unreachable_ingest,
        reconcile_outbound_message_id=callback,
        now=lambda: NOW,
    )


class _UnreachableAdmission(PeerAdmissionResolver):
    async def __call__(self, _scope: TelethonUpdateScope) -> PeerAdmission:
        raise AssertionError("unexpected admission resolution")


async def _unreachable_ingest(_event: NormalizedTelegramEvent) -> object:
    raise AssertionError("unexpected event ingestion")


@pytest.mark.unit
async def test_update_message_id_is_reconciled_without_message_projection(tmp_path: Path) -> None:
    calls: list[tuple[int, int, datetime]] = []

    async def reconcile(random_id: int, message_id: int, now: datetime) -> None:
        calls.append((random_id, message_id, now))

    runtime = _runtime(reconcile, tmp_path)
    await runtime._handle_update(types.UpdateMessageID(id=321, random_id=654))
    assert calls == [(654, 321, NOW)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message_id", "random_id"),
    [(0, 1), (1, 0), (True, 1), (1, False)],
)
async def test_update_message_id_rejects_non_positive_or_boolean_ids(
    message_id: int, random_id: int, tmp_path: Path
) -> None:
    runtime = _runtime(None, tmp_path)
    with pytest.raises(TelethonSessionRuntimeError, match="MAPPING_INVALID"):
        await runtime._handle_update(types.UpdateMessageID(id=message_id, random_id=random_id))
