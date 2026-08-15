"""Provider fakes for timeout, malformed, and duplicate-output tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from telegram_userbot.domain.memory.validation import (
    ProposalValidationError,
    validate_response_json,
)


@dataclass(frozen=True, slots=True)
class FakeMemoryAgent:
    response: object
    delay_seconds: float = 0
    fail_with: Exception | None = None

    async def extract(self) -> object:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_with is not None:
            raise self.fail_with
        return self.response

    async def extract_validated(self, *, account_id: Any, conversation_id: Any) -> tuple[Any, ...]:
        response = await self.extract()
        if isinstance(response, str):
            return validate_response_json(
                response,
                account_id=account_id,
                conversation_id=conversation_id,
            )
        raise ProposalValidationError("malformed_response", "fake response must be JSON text")


@dataclass(frozen=True, slots=True)
class FakeSummaryProvider:
    response: str
    delay_seconds: float = 0

    async def summarize(self, _content: str) -> str:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.response
