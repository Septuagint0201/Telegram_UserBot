from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationTargetTokenCodec,
)
from telegram_userbot.adapters.telegram_bot.memory_control import (
    MemoryCandidateSummary,
    MemoryControlController,
    MemoryItemSummary,
    MemoryReviewChallenge,
    MemoryStatusSummary,
)
from telegram_userbot.adapters.telegram_bot.memory_control_backend import (
    DurableMemoryControlBackend,
    MemoryControlTargetTokenCodec,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
CONVERSATION = UUID("00000000-0000-0000-0000-000000000002")


@dataclass(slots=True)
class FakeMemoryBackend:
    actions: list[str] = field(default_factory=list)
    confirm_result: bool = True
    empty_candidates: bool = False
    empty_active: bool = False
    raise_confirm: bool = False

    async def status(
        self, *, account_id: object, conversation_id: object, now: datetime
    ) -> MemoryStatusSummary:
        assert account_id == ACCOUNT
        assert conversation_id == CONVERSATION
        assert now == NOW
        return MemoryStatusSummary("fresh", 0, 1, 3, "active")

    async def candidates(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryCandidateSummary, ...]:
        assert account_id == ACCOUNT
        assert conversation_id == CONVERSATION
        assert (admin_id, bot_chat_id, now) == (42, 42, NOW)
        if self.empty_candidates:
            return ()
        return (MemoryCandidateSummary("mc_opaque", "create", "preference", "medium", 2, True),)

    async def active(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryItemSummary, ...]:
        assert account_id == ACCOUNT
        assert conversation_id == CONVERSATION
        assert (admin_id, bot_chat_id, now) == (42, 42, NOW)
        if self.empty_active:
            return ()
        return (MemoryItemSummary("mm_opaque", "preference", "active", 1),)

    async def issue_action(
        self,
        *,
        action: str,
        target_token: str,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> MemoryReviewChallenge:
        assert (admin_id, bot_chat_id) == (42, 42)
        assert target_token == "mm_opaque"  # noqa: S105 - opaque test selector, not a secret
        self.actions.append(action)
        return MemoryReviewChallenge(action, SensitiveValue("ma_once"), now + timedelta(minutes=5))

    async def confirm_action(
        self,
        *,
        callback_token: SensitiveValue[str],
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> bool:
        assert callback_token.reveal_for_use() == "ma_once"
        if self.raise_confirm:
            raise ValueError("synthetic failure")
        return self.confirm_result and admin_id == bot_chat_id == 42 and now == NOW


def _controller() -> tuple[MemoryControlController, FakeMemoryBackend, str]:
    codec = ConversationTargetTokenCodec(
        SensitiveValue(b"k" * 32),
        deployment_id="test",
        nonce_source=lambda size: b"n" * size,
    )
    token = codec.issue(
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        admin_id=42,
        bot_chat_id=42,
        expires_at=NOW + timedelta(minutes=5),
    )
    backend = FakeMemoryBackend()
    return (
        MemoryControlController(
            allowed_admin_ids=frozenset({42}), target_tokens=codec, backend=backend
        ),
        backend,
        token,
    )


@pytest.mark.asyncio
async def test_memory_control_is_private_metadata_only_and_uses_second_confirmation() -> None:
    controller, backend, token = _controller()
    rejected = await controller.handle(
        admin_id=7, bot_chat_id=7, message_text=f"/memory {token}", now=NOW
    )
    assert rejected.text == "Request rejected."
    candidates = await controller.handle(
        admin_id=42,
        bot_chat_id=42,
        message_text=f"/memory_candidates {token}",
        now=NOW,
    )
    assert "mc_opaque" in candidates.text
    assert "SYNTHETIC_PRIVATE_CONTEXT_BODY" not in candidates.text
    challenge = await controller.handle(
        admin_id=42,
        bot_chat_id=42,
        message_text="/forget mm_opaque",
        now=NOW,
    )
    assert backend.actions == ["forget"]
    assert challenge.callback_token is not None
    confirmed = await controller.confirm_callback(
        admin_id=42,
        bot_chat_id=42,
        callback_token=challenge.callback_token,
        now=NOW,
    )
    assert confirmed.text == "Memory action confirmed and queued."


@pytest.mark.asyncio
async def test_memory_control_rejects_bad_commands_and_reports_metadata_only() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        MemoryControlController(
            allowed_admin_ids=frozenset(),
            target_tokens=_controller()[0]._target_tokens,
            backend=FakeMemoryBackend(),
        )
    controller, backend, token = _controller()
    invalid = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text='"unterminated', now=NOW
    )
    assert invalid.text == "Invalid command syntax."
    usage = await controller.handle(admin_id=42, bot_chat_id=42, message_text="/memory", now=NOW)
    assert usage.text.startswith("Usage:")
    status = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text=f"/memory_status {token}", now=NOW
    )
    assert "freshness=fresh" in status.text
    active = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text=f"/memory {token}", now=NOW
    )
    assert "mm_opaque" in active.text
    assert "SYNTHETIC_PRIVATE_CONTEXT_BODY" not in active.text
    unknown = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text=f"/unknown {token}", now=NOW
    )
    assert unknown.text == "Unknown memory command."
    rejected = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text="/memory invalid-token", now=NOW
    )
    assert rejected.text == "Request rejected; state unchanged."
    backend.empty_candidates = True
    empty = await controller.handle(
        admin_id=42,
        bot_chat_id=42,
        message_text=f"/memory_candidates {token}",
        now=NOW,
    )
    assert empty.text == "No memory candidates."
    backend.empty_active = True
    empty_memory = await controller.handle(
        admin_id=42, bot_chat_id=42, message_text=f"/memory {token}", now=NOW
    )
    assert empty_memory.text == "No active memories."


@pytest.mark.asyncio
async def test_memory_control_callback_is_private_and_fail_closed() -> None:
    controller, backend, _token = _controller()
    private = await controller.confirm_callback(
        admin_id=42,
        bot_chat_id=7,
        callback_token=SensitiveValue("ma_once"),
        now=NOW,
    )
    assert private.text == "Request rejected."
    backend.confirm_result = False
    failed = await controller.confirm_callback(
        admin_id=42,
        bot_chat_id=42,
        callback_token=SensitiveValue("ma_once"),
        now=NOW,
    )
    assert "invalid, expired" in failed.text
    backend.raise_confirm = True
    raised = await controller.confirm_callback(
        admin_id=42,
        bot_chat_id=42,
        callback_token=SensitiveValue("ma_once"),
        now=NOW,
    )
    assert "invalid, expired" in raised.text


def test_memory_target_tokens_are_private_principal_bound_and_expiring() -> None:
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32),
        deployment_id="test",
        nonce_source=lambda size: b"n" * size,
    )
    target_id = uuid4()
    token = codec.issue(
        kind="memory",
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        target_id=target_id,
        version=3,
        admin_id=42,
        bot_chat_id=42,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert token.startswith("mm_")
    assert str(target_id) not in token
    resolved = codec.resolve(token, admin_id=42, bot_chat_id=42, now=NOW)
    assert (resolved.kind, resolved.target_id, resolved.version) == ("memory", target_id, 3)
    with pytest.raises(ValueError, match="wrong-principal"):
        codec.resolve(token, admin_id=7, bot_chat_id=7, now=NOW)
    with pytest.raises(ValueError, match="wrong-principal"):
        codec.resolve(token, admin_id=42, bot_chat_id=42, now=NOW + timedelta(minutes=5))
    replacement = "A" if token[12] != "A" else "B"
    tampered = token[:12] + replacement + token[13:]
    with pytest.raises(ValueError, match="invalid memory target"):
        codec.resolve(tampered, admin_id=42, bot_chat_id=42, now=NOW)
    with pytest.raises(ValueError, match="input is invalid"):
        codec.issue(
            kind="candidate",
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            target_id=target_id,
            version=0,
            admin_id=42,
            bot_chat_id=7,
            expires_at=NOW + timedelta(minutes=5),
        )
    candidate = codec.issue(
        kind="candidate",
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        target_id=target_id,
        version=0,
        admin_id=42,
        bot_chat_id=42,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert codec.resolve(candidate, admin_id=42, bot_chat_id=42, now=NOW).kind == "candidate"


def test_memory_target_token_codec_rejects_bad_keys_nonce_time_and_shape() -> None:
    with pytest.raises(ValueError, match="settings"):
        MemoryControlTargetTokenCodec(SensitiveValue(b"short"), deployment_id="test")
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32),
        deployment_id="test",
        nonce_source=lambda _size: b"short",
    )
    with pytest.raises(ValueError, match="12 bytes"):
        codec.issue(
            kind="memory",
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            target_id=uuid4(),
            version=1,
            admin_id=42,
            bot_chat_id=42,
            expires_at=NOW + timedelta(minutes=5),
        )
    valid_codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32),
        deployment_id="test",
        nonce_source=lambda size: b"n" * size,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        valid_codec.issue(
            kind="memory",
            account_id=ACCOUNT,
            conversation_id=CONVERSATION,
            target_id=uuid4(),
            version=1,
            admin_id=42,
            bot_chat_id=42,
            expires_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="invalid memory target"):
        valid_codec.resolve("unknown", admin_id=42, bot_chat_id=42, now=NOW)
    with pytest.raises(ValueError, match="invalid memory target"):
        valid_codec.resolve("mm_AA", admin_id=42, bot_chat_id=42, now=NOW)


def test_durable_memory_backend_rejects_invalid_security_settings() -> None:
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32), deployment_id="test", nonce_source=lambda size: b"n" * size
    )
    with pytest.raises(ValueError, match="TTLs"):
        DurableMemoryControlBackend(
            session=cast(AsyncSession, object()),
            target_tokens=codec,
            target_ttl=timedelta(0),
        )


@pytest.mark.asyncio
async def test_durable_memory_backend_rejects_bad_scope_and_callback_shape() -> None:
    codec = MemoryControlTargetTokenCodec(
        SensitiveValue(b"m" * 32), deployment_id="test", nonce_source=lambda size: b"n" * size
    )
    backend = DurableMemoryControlBackend(session=cast(AsyncSession, object()), target_tokens=codec)
    with pytest.raises(TypeError, match="scope"):
        await backend.status(account_id="invalid", conversation_id=CONVERSATION, now=NOW)
    assert not await backend.confirm_action(
        callback_token=SensitiveValue("invalid"),
        admin_id=42,
        bot_chat_id=42,
        now=NOW,
    )
