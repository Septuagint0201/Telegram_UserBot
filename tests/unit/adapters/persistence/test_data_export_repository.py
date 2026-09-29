from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.data_export_repository import DataExportRepository

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
REQUEST_ID = UUID("01900000-0000-7000-8000-000000000211")
OWNER_ID = UUID("01900000-0000-7000-8000-000000000212")


class _Mappings:
    def mappings(self) -> _Mappings:
        return self

    def one_or_none(self) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_data_export_renewal_is_owner_version_and_expiry_fenced() -> None:
    session = AsyncMock()
    session.execute.return_value = _Mappings()
    repository = DataExportRepository(cast(AsyncSession, session))

    assert (
        await repository.renew(
            request_id=REQUEST_ID,
            owner_instance_id=OWNER_ID,
            expected_version=7,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=30),
        )
        is None
    )

    compiled = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    statement = str(compiled)
    assert "data_export_requests.state =" in statement
    assert "data_export_requests.owner_instance_id =" in statement
    assert "data_export_requests.version =" in statement
    assert "data_export_requests.lease_expires_at >" in statement
    assert "data_export_requests.expires_at >" in statement
    assert "version=(data_export_requests.version +" in statement
    assert 7 in compiled.params.values()
