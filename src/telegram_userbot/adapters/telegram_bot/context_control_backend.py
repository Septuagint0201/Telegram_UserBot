"""Durable context-control backend with injectable reconstruction and Bot side effects."""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.context_repository import (
    ContextRepository,
    ContextSummaryRecord,
    PreviewChallenge,
    PreviewRequestRecord,
)
from telegram_userbot.adapters.telegram_bot.context_control import PreviewDeliveryResult
from telegram_userbot.adapters.telegram_bot.conversation_control_backend import (
    ConversationTargetTokenCodec,
)
from telegram_userbot.domain.shared.hashing import JsonValue, stable_json_bytes
from telegram_userbot.domain.shared.redaction import SensitiveValue


class PreviewSendUnknownError(RuntimeError):
    pass


class PreviewSendRejectedError(RuntimeError):
    """The gateway rejected the request before any Telegram side effect."""


class PreviewGateway(Protocol):
    async def send_text(self, *, bot_chat_id: int, text: SensitiveValue[str]) -> int: ...

    async def delete_message(self, *, bot_chat_id: int, bot_message_id: int) -> None: ...


class ContextPreviewRebuilder(Protocol):
    async def rebuild_redacted(
        self, *, request: PreviewRequestRecord
    ) -> tuple[SensitiveValue[str], ...]: ...


class ExactManifestPreviewRebuilder:
    """Read an exact confirmed manifest through the control-only DB function."""

    def __init__(self, session: AsyncSession, *, max_chunk_chars: int = 3_500) -> None:
        if max_chunk_chars <= 0:
            raise ValueError("context preview chunk size must be positive")
        self._session = session
        self._max_chunk_chars = max_chunk_chars

    async def rebuild_redacted(
        self, *, request: PreviewRequestRecord
    ) -> tuple[SensitiveValue[str], ...]:
        rows = (
            (
                await self._session.execute(
                    text(
                        "SELECT * FROM public.context_preview_sources("
                        ":request_id, :admin_id, :bot_chat_id, :bot_identity)"
                    ),
                    {
                        "request_id": request.request_id,
                        "admin_id": request.admin_user_id,
                        "bot_chat_id": request.bot_chat_id,
                        "bot_identity": request.bot_identity,
                    },
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ValueError("context_source_unavailable")
        vector: list[dict[str, str]] = []
        rendered: list[str] = []
        for ordinal, row in enumerate(rows, 1):
            if row["ordinal"] != ordinal or not row["source_eligible"]:
                raise ValueError("context_source_revision_changed")
            if cast(bytes, row["source_revision_vector_sha256"]) != (
                request.source_revision_vector_sha256
            ):
                raise ValueError("context_source_revision_changed")
            source_content = cast(str | None, row["source_content"])
            if not source_content:
                raise ValueError("context_source_unavailable")
            if hashlib.sha256(source_content.encode()).digest() != cast(
                bytes, row["content_sha256"]
            ):
                raise ValueError("context_source_revision_changed")
            source_id = str(row["source_id"])
            source_revision = cast(str, row["source_revision"])
            vector.append(
                {
                    "source_type": cast(str, row["source_type"]),
                    "source_id": source_id,
                    "revision": source_revision,
                }
            )
            rendered_part = (
                source_content
                if row["layer"] == "instruction"
                else (
                    f"[CONTEXT_DATA layer={row['layer']} source={source_id} "
                    f"trust={row['trust_level']}]\n{source_content}\n[/CONTEXT_DATA]"
                )
            )
            if hashlib.sha256(rendered_part.encode()).digest() != cast(
                bytes, row["rendered_part_sha256"]
            ):
                raise ValueError("context_source_revision_changed")
            rendered.append(f"[{row['canonical_role']}]\n{rendered_part}")
        vector_hash = hashlib.sha256(stable_json_bytes(cast(JsonValue, vector))).digest()
        if vector_hash != request.source_revision_vector_sha256:
            raise ValueError("context_source_revision_changed")
        preview = "\n\n".join(rendered)
        return tuple(
            SensitiveValue(preview[offset : offset + self._max_chunk_chars])
            for offset in range(0, len(preview), self._max_chunk_chars)
        )


class DurableContextControlBackend:
    def __init__(  # noqa: PLR0913 - security boundaries are explicit
        self,
        *,
        repository: ContextRepository,
        target_tokens: ConversationTargetTokenCodec,
        rebuilder: ContextPreviewRebuilder,
        gateway: PreviewGateway,
        bot_identity: str = "control-bot",
        max_chunk_chars: int = 3_500,
        max_chunks: int = 8,
        delete_after: timedelta = timedelta(minutes=10),
        max_rejected_retries: int = 1,
        on_delete_failure: Callable[[str], None] | None = None,
    ) -> None:
        if (
            not bot_identity
            or bot_identity != bot_identity.strip()
            or not 1 <= max_chunk_chars <= 4_096
            or not 1 <= max_chunks <= 8
            or delete_after <= timedelta(0)
            or delete_after > timedelta(minutes=10)
            or not 0 <= max_rejected_retries <= 3
        ):
            raise ValueError("context control settings are invalid")
        self._repository = repository
        self._target_tokens = target_tokens
        self._rebuilder = rebuilder
        self._gateway = gateway
        self._bot_identity = bot_identity
        self._max_chunk_chars = max_chunk_chars
        self._max_chunks = max_chunks
        self._delete_after = delete_after
        self._max_rejected_retries = max_rejected_retries
        self._on_delete_failure = on_delete_failure

    async def summary(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> ContextSummaryRecord | None:
        target = self._target_tokens.resolve(
            target_token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now
        )
        return await self._repository.latest_summary(
            account_id=target.account_id, conversation_id=target.conversation_id
        )

    async def issue_preview(
        self, *, admin_id: int, bot_chat_id: int, target_token: str, now: datetime
    ) -> PreviewChallenge:
        target = self._target_tokens.resolve(
            target_token, admin_id=admin_id, bot_chat_id=bot_chat_id, now=now
        )
        summary = await self._repository.latest_summary(
            account_id=target.account_id, conversation_id=target.conversation_id
        )
        if summary is None:
            raise ValueError("context_manifest_unavailable")
        return await self._repository.issue_preview(
            account_id=target.account_id,
            conversation_id=target.conversation_id,
            manifest_id=summary.manifest_id,
            admin_user_id=admin_id,
            bot_chat_id=bot_chat_id,
            bot_identity=self._bot_identity,
            now=now,
        )

    async def confirm_preview(
        self,
        *,
        admin_id: int,
        bot_chat_id: int,
        confirmation_token: SensitiveValue[str],
        now: datetime,
    ) -> tuple[PreviewRequestRecord, tuple[SensitiveValue[str], ...]] | None:
        request = await self._repository.consume_preview(
            token=confirmation_token,
            admin_user_id=admin_id,
            bot_chat_id=bot_chat_id,
            bot_identity=self._bot_identity,
            now=now,
        )
        if request is None:
            return None
        chunks = await self._rebuilder.rebuild_redacted(request=request)
        if not chunks or len(chunks) > self._max_chunks:
            raise ValueError("context_preview_chunk_overflow")
        if any(
            not item.reveal_for_use() or len(item.reveal_for_use()) > self._max_chunk_chars
            for item in chunks
        ):
            raise ValueError("context_preview_chunk_overflow")
        return request, chunks

    async def deliver_preview(  # noqa: PLR0912 - explicit durable RPC states
        self,
        *,
        request: PreviewRequestRecord,
        chunks: tuple[SensitiveValue[str], ...],
        now: datetime,
    ) -> PreviewDeliveryResult:
        delete_after = now + self._delete_after
        deliveries = await self._repository.begin_preview_delivery(
            request=request,
            chunk_count=len(chunks),
            now=now,
            delete_after=delete_after,
        )
        if not deliveries:
            raise RuntimeError("context_preview_delivery_conflict")
        persisted_delete_after = deliveries[0].delete_after
        if any(delivery.delete_after != persisted_delete_after for delivery in deliveries):
            raise RuntimeError("context_preview_delivery_conflict")
        await self._repository.commit_preview_boundary()
        for delivery, chunk in zip(deliveries, chunks, strict=True):
            if delivery.state == "sent":
                continue
            if delivery.state in {"sending", "send_unknown"}:
                if delivery.state == "sending":
                    await self._repository.record_preview_delivery_chunk(
                        request=request,
                        ordinal=delivery.ordinal,
                        state="send_unknown",
                        message_id=None,
                        now=now,
                        delete_after=persisted_delete_after,
                    )
                    await self._repository.commit_preview_boundary()
                return await self._finish_preview_delivery(request=request, now=now)
            if delivery.state != "pending":
                raise RuntimeError("context_preview_delivery_conflict")
            rejected_retries = 0
            while True:
                if not await self._repository.claim_preview_delivery_chunk(
                    request=request, ordinal=delivery.ordinal
                ):
                    raise RuntimeError("context_preview_delivery_conflict")
                await self._repository.commit_preview_boundary()
                try:
                    message_id = await self._gateway.send_text(
                        bot_chat_id=request.bot_chat_id, text=chunk
                    )
                except PreviewSendRejectedError as error:
                    await self._repository.retry_preview_delivery_chunk(
                        request=request, ordinal=delivery.ordinal
                    )
                    await self._repository.commit_preview_boundary()
                    if rejected_retries >= self._max_rejected_retries:
                        failed = await self._repository.fail_preview_delivery(
                            request=request,
                            now=now,
                            error_code="preview_send_rejected",
                        )
                        await self._repository.commit_preview_boundary()
                        if not failed:
                            raise RuntimeError("context_preview_delivery_conflict") from error
                        raise
                    rejected_retries += 1
                    continue
                except PreviewSendUnknownError:
                    message_id = None
                except Exception:
                    message_id = None
                break
            await self._repository.record_preview_delivery_chunk(
                request=request,
                ordinal=delivery.ordinal,
                state="sent" if message_id is not None else "send_unknown",
                message_id=message_id,
                now=now,
                delete_after=persisted_delete_after,
            )
            await self._repository.commit_preview_boundary()
            if message_id is None:
                return await self._finish_preview_delivery(request=request, now=now)
        return await self._finish_preview_delivery(request=request, now=now)

    async def _finish_preview_delivery(
        self, *, request: PreviewRequestRecord, now: datetime
    ) -> PreviewDeliveryResult:
        state, delivered, total = await self._repository.finish_preview_delivery(
            request=request, now=now
        )
        await self._repository.commit_preview_boundary()
        return PreviewDeliveryResult(state, delivered, total)

    async def delete_due(self, *, now: datetime) -> int:
        deleted = 0
        due = await self._repository.due_preview_deletions(bot_identity=self._bot_identity, now=now)
        if due:
            await self._repository.commit_preview_boundary()
        for item in due:
            try:
                await self._gateway.delete_message(
                    bot_chat_id=item.bot_chat_id,
                    bot_message_id=item.bot_message_id,
                )
            except Exception:
                critical_alert = await self._repository.finish_preview_deletion(
                    deletion=item,
                    deleted=False,
                    now=now,
                    error_code="preview_delete_failed",
                )
                await self._repository.commit_preview_boundary()
                if self._on_delete_failure is not None:
                    self._on_delete_failure(
                        "preview_delete_failed_critical"
                        if critical_alert
                        else "preview_delete_failed"
                    )
            else:
                await self._repository.finish_preview_deletion(
                    deletion=item,
                    deleted=True,
                    now=now,
                )
                await self._repository.commit_preview_boundary()
                deleted += 1
        return deleted
