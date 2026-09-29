"""The server derives visual review requirements even when the model omits them."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from telegram_userbot.adapters.persistence import memory_results
from telegram_userbot.domain.memory.models import (
    Evidence,
    MemoryOperation,
    MemoryProposal,
    MemoryType,
    ProposalState,
)
from tests.unit.processes.test_memory_pipeline import NOW, prepared

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["text", "pixels", "historical_visual", "declared_visual"])
async def test_visual_proposals_cannot_be_automatically_promoted(
    mode: str, monkeypatch: Any
) -> None:
    value, _ = prepared()
    source = replace(value.manifest.sources[0], visual_only=mode == "historical_visual")
    value = replace(
        value,
        manifest=replace(value.manifest, sources=(source,), image_count=int(mode == "pixels")),
    )
    proposal = MemoryProposal(
        UUID(int=90),
        value.manifest.account_id,
        value.manifest.conversation_id,
        MemoryOperation.CREATE,
        MemoryType.FACT,
        "synthetic:visual",
        {"value": "synthetic"},
        0.99,
        0.5,
        (Evidence(source.source_id, source.revision, source.content_sha256),),
        visual_only=mode == "declared_visual",
    )
    repository = SimpleNamespace(
        record_proposal=AsyncMock(return_value=UUID(int=91)),
        accept_validated_proposal=AsyncMock(
            return_value=SimpleNamespace(memory_version_id=UUID(int=92))
        ),
    )
    monkeypatch.setattr(memory_results, "MemoryRepository", MagicMock(return_value=repository))
    session = AsyncMock(scalar=AsyncMock(return_value=None))
    await memory_results.MemoryResultRepository(session)._proposals(value, (proposal,), now=NOW)
    staged = repository.record_proposal.await_args.args[0]
    assert staged.state is (ProposalState.VALIDATING if mode == "text" else ProposalState.CANDIDATE)
    assert staged.proposal.visual_only == (mode != "text")
    assert staged.proposal.evidence[0].visual_only == source.visual_only
    assert repository.accept_validated_proposal.await_count == int(mode == "text")
