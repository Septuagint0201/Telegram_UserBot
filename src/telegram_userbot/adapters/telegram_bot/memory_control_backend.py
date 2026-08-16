"""Durable metadata-only Memory Control Bot backend."""

from __future__ import annotations

import base64
import secrets
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.memory_repository import MemoryRepository
from telegram_userbot.adapters.persistence.schema import (
    embedding_spaces,
    memories,
    memory_jobs,
    memory_proposal_evidence,
    memory_proposals,
)
from telegram_userbot.adapters.telegram_bot.memory_control import (
    MemoryCandidateSummary,
    MemoryItemSummary,
    MemoryReviewChallenge,
    MemoryStatusSummary,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue

_TARGET_PREFIXES = {"candidate": "mc_", "memory": "mm_"}
_TARGET_CODES = {"candidate": b"C", "memory": b"M"}
_TARGET_PLAINTEXT_LENGTH = 81
_TARGET_RAW_LENGTH = 12 + _TARGET_PLAINTEXT_LENGTH + 16


@dataclass(frozen=True, slots=True)
class MemoryControlTarget:
    kind: str
    account_id: UUID
    conversation_id: UUID
    target_id: UUID
    version: int


class MemoryControlTargetTokenCodec:
    """Encrypt short-lived candidate/memory selectors bound to one admin chat."""

    def __init__(
        self,
        signing_key: SensitiveValue[bytes],
        *,
        deployment_id: str,
        nonce_source: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        key = signing_key.reveal_for_use()
        if not deployment_id or len(key) < 32:
            raise ValueError("memory target token settings are invalid")
        self._key = sha256(b"memory-control-target-key-v1\0" + key).digest()
        self._deployment_id = deployment_id
        self._nonce_source = nonce_source

    def issue(  # noqa: PLR0913 - token principal and target binding is explicit
        self,
        *,
        kind: str,
        account_id: UUID,
        conversation_id: UUID,
        target_id: UUID,
        version: int,
        admin_id: int,
        bot_chat_id: int,
        expires_at: datetime,
    ) -> str:
        if kind not in _TARGET_PREFIXES or version < 0 or bot_chat_id != admin_id:
            raise ValueError("memory target token input is invalid")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("memory target token expiry must be timezone-aware")
        plaintext = (
            _TARGET_CODES[kind]
            + account_id.bytes
            + conversation_id.bytes
            + target_id.bytes
            + struct.pack(">qqqq", version, admin_id, bot_chat_id, int(expires_at.timestamp()))
        )
        nonce = self._nonce_source(12)
        if len(nonce) != 12:
            raise ValueError("memory target nonce source must return 12 bytes")
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, self._aad())
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode("ascii")
        return _TARGET_PREFIXES[kind] + encoded

    def resolve(
        self, token: str, *, admin_id: int, bot_chat_id: int, now: datetime
    ) -> MemoryControlTarget:
        kind = next(
            (name for name, prefix in _TARGET_PREFIXES.items() if token.startswith(prefix)), None
        )
        if kind is None:
            raise ValueError("invalid memory target token")
        encoded = token.removeprefix(_TARGET_PREFIXES[kind])
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise ValueError("invalid memory target token") from error
        if len(raw) != _TARGET_RAW_LENGTH:
            raise ValueError("invalid memory target token")
        try:
            plaintext = AESGCM(self._key).decrypt(raw[:12], raw[12:], self._aad())
        except InvalidTag as error:
            raise ValueError("invalid memory target token") from error
        if len(plaintext) != _TARGET_PLAINTEXT_LENGTH or plaintext[:1] != _TARGET_CODES[kind]:
            raise ValueError("invalid memory target token")
        account_id = UUID(bytes=plaintext[1:17])
        conversation_id = UUID(bytes=plaintext[17:33])
        target_id = UUID(bytes=plaintext[33:49])
        version, token_admin, token_chat, expiry = struct.unpack(">qqqq", plaintext[49:])
        if token_admin != admin_id or token_chat != bot_chat_id or expiry <= int(now.timestamp()):
            raise ValueError("expired or wrong-principal memory target token")
        return MemoryControlTarget(kind, account_id, conversation_id, target_id, version)

    def _aad(self) -> bytes:
        return b"memory-control-target-v1\0" + self._deployment_id.encode()


class DurableMemoryControlBackend:
    """Read bounded metadata and enqueue version-bound review actions."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        target_tokens: MemoryControlTargetTokenCodec,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
        target_ttl: timedelta = timedelta(minutes=5),
        confirmation_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if target_ttl <= timedelta(0) or confirmation_ttl <= timedelta(0):
            raise ValueError("memory control TTLs must be positive")
        self._session = session
        self._repository = MemoryRepository(session)
        self._target_tokens = target_tokens
        self._entropy = entropy
        self._target_ttl = target_ttl
        self._confirmation_ttl = confirmation_ttl

    async def status(
        self, *, account_id: object, conversation_id: object, now: datetime
    ) -> MemoryStatusSummary:
        account = _uuid_value(account_id)
        conversation = _uuid_value(conversation_id)
        job_rows = (
            (
                await self._session.execute(
                    select(
                        memory_jobs.c.state,
                        memory_jobs.c.hard_due_at,
                        memory_jobs.c.eligible_revision_count,
                        memory_jobs.c.estimated_input_tokens,
                    ).where(
                        memory_jobs.c.account_id == account,
                        memory_jobs.c.conversation_id == conversation,
                        memory_jobs.c.state.in_(
                            ("pending", "leased", "running", "retry_wait", "dead_letter")
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        freshness = "fresh"
        if any(row["state"] == "dead_letter" for row in job_rows):
            freshness = "blocked"
        elif any(row["state"] in {"leased", "running"} for row in job_rows):
            freshness = "rebuilding"
        elif any(
            row["hard_due_at"] <= now
            or row["eligible_revision_count"] >= 20
            or row["estimated_input_tokens"] >= 6_000
            for row in job_rows
        ):
            freshness = "stale"
        elif job_rows:
            freshness = "degraded"
        candidate_count = cast(
            int,
            await self._session.scalar(
                select(func.count())
                .select_from(memory_proposals)
                .where(
                    memory_proposals.c.account_id == account,
                    memory_proposals.c.conversation_id == conversation,
                    memory_proposals.c.state == "candidate",
                )
            ),
        )
        active_count = cast(
            int,
            await self._session.scalar(
                select(func.count())
                .select_from(memories)
                .where(
                    memories.c.account_id == account,
                    memories.c.conversation_id == conversation,
                    memories.c.status == "active",
                )
            ),
        )
        embedding_state = (
            await self._session.scalar(
                select(embedding_spaces.c.state)
                .where(embedding_spaces.c.account_id == account)
                .order_by(
                    (embedding_spaces.c.state == "active").desc(),
                    embedding_spaces.c.generation.desc(),
                )
                .limit(1)
            )
            or "unconfigured"
        )
        return MemoryStatusSummary(
            freshness,
            len(job_rows),
            candidate_count,
            active_count,
            cast(str, embedding_state),
        )

    async def candidates(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryCandidateSummary, ...]:
        account = _uuid_value(account_id)
        conversation = _uuid_value(conversation_id)
        evidence_count = func.count(memory_proposal_evidence.c.message_revision_id)
        rows = (
            (
                await self._session.execute(
                    select(
                        memory_proposals.c.id,
                        memory_proposals.c.review_version,
                        memory_proposals.c.operation,
                        memory_proposals.c.memory_type,
                        memory_proposals.c.proposed_confidence,
                        memory_proposals.c.visual_only,
                        evidence_count.label("evidence_count"),
                    )
                    .outerjoin(
                        memory_proposal_evidence,
                        memory_proposal_evidence.c.proposal_id == memory_proposals.c.id,
                    )
                    .where(
                        memory_proposals.c.account_id == account,
                        memory_proposals.c.conversation_id == conversation,
                        memory_proposals.c.state == "candidate",
                    )
                    .group_by(memory_proposals.c.id)
                    .order_by(memory_proposals.c.created_at, memory_proposals.c.id)
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        expires_at = now + self._target_ttl
        return tuple(
            MemoryCandidateSummary(
                self._target_tokens.issue(
                    kind="candidate",
                    account_id=account,
                    conversation_id=conversation,
                    target_id=cast(UUID, row["id"]),
                    version=cast(int, row["review_version"]),
                    admin_id=admin_id,
                    bot_chat_id=bot_chat_id,
                    expires_at=expires_at,
                ),
                cast(str, row["operation"]),
                cast(str, row["memory_type"]),
                _confidence_band(float(row["proposed_confidence"])),
                cast(int, row["evidence_count"]),
                cast(bool, row["visual_only"]),
            )
            for row in rows
        )

    async def active(
        self,
        *,
        account_id: object,
        conversation_id: object,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> tuple[MemoryItemSummary, ...]:
        account = _uuid_value(account_id)
        conversation = _uuid_value(conversation_id)
        rows = (
            (
                await self._session.execute(
                    select(
                        memories.c.id,
                        memories.c.memory_type,
                        memories.c.status,
                        memories.c.current_version_no,
                    )
                    .where(
                        memories.c.account_id == account,
                        memories.c.conversation_id == conversation,
                        memories.c.status == "active",
                    )
                    .order_by(memories.c.created_at, memories.c.id)
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        expires_at = now + self._target_ttl
        return tuple(
            MemoryItemSummary(
                self._target_tokens.issue(
                    kind="memory",
                    account_id=account,
                    conversation_id=conversation,
                    target_id=cast(UUID, row["id"]),
                    version=cast(int, row["current_version_no"]),
                    admin_id=admin_id,
                    bot_chat_id=bot_chat_id,
                    expires_at=expires_at,
                ),
                cast(str, row["memory_type"]),
                cast(str, row["status"]),
                cast(int, row["current_version_no"]),
            )
            for row in rows
        )

    async def issue_action(
        self,
        *,
        action: str,
        target_token: str,
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> MemoryReviewChallenge:
        target = self._target_tokens.resolve(
            target_token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now
        )
        proposal_id: UUID | None = None
        memory_id: UUID | None = None
        expected_proposal_version: int | None = None
        expected_memory_version: int | None = None
        if action in {"accept", "reject"} and target.kind == "candidate":
            row = (
                (
                    await self._session.execute(
                        select(
                            memory_proposals.c.id,
                            memory_proposals.c.review_version,
                        ).where(
                            memory_proposals.c.id == target.target_id,
                            memory_proposals.c.account_id == target.account_id,
                            memory_proposals.c.conversation_id == target.conversation_id,
                            memory_proposals.c.state == "candidate",
                            memory_proposals.c.review_version == target.version,
                            (memory_proposals.c.expires_at.is_(None))
                            | (memory_proposals.c.expires_at > now),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ValueError("memory candidate is no longer reviewable")
            proposal_id = cast(UUID, row["id"])
            expected_proposal_version = cast(int, row["review_version"])
        elif action == "forget" and target.kind == "memory":
            row = (
                (
                    await self._session.execute(
                        select(memories.c.id, memories.c.current_version_no).where(
                            memories.c.id == target.target_id,
                            memories.c.account_id == target.account_id,
                            memories.c.conversation_id == target.conversation_id,
                            memories.c.status == "active",
                            memories.c.current_version_no == target.version,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ValueError("memory target is no longer current")
            memory_id = cast(UUID, row["id"])
            expected_memory_version = cast(int, row["current_version_no"])
        else:
            raise ValueError("memory action does not match its target")

        entropy = self._entropy(32)
        if len(entropy) != 32:
            raise ValueError("memory confirmation entropy must contain 32 bytes")
        callback_value = "ma_" + base64.urlsafe_b64encode(entropy).rstrip(b"=").decode("ascii")
        expires_at = now + self._confirmation_ttl
        await self._repository.issue_review_action(
            account_id=target.account_id,
            conversation_id=target.conversation_id,
            admin_actor_id=admin_id,
            bot_chat_id=bot_chat_id,
            action=action,
            proposal_id=proposal_id,
            memory_id=memory_id,
            token=callback_value.encode(),
            expires_at=expires_at,
            expected_proposal_version=expected_proposal_version,
            expected_memory_version=expected_memory_version,
        )
        return MemoryReviewChallenge(action, SensitiveValue(callback_value), expires_at)

    async def confirm_action(
        self,
        *,
        callback_token: SensitiveValue[str],
        admin_id: int,
        bot_chat_id: int,
        now: datetime,
    ) -> bool:
        token = callback_token.reveal_for_use()
        if not token.startswith("ma_"):
            return False
        action = await self._repository.consume_review_action(
            token=token.encode(),
            admin_actor_id=admin_id,
            bot_chat_id=bot_chat_id,
            now=now,
        )
        return action is not None


def _uuid_value(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError("memory scope is invalid")
    return value


def _confidence_band(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.60:
        return "medium"
    return "low"
