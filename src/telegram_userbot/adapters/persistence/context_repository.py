"""Transactions for immutable context manifests and content-free preview state."""

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence.schema import (
    context_manifest_item_reasons,
    context_manifest_items,
    context_manifest_omissions,
    context_manifests,
    context_policies,
    context_policy_versions,
    context_preview_deliveries,
    context_preview_requests,
    context_preview_tokens,
    retrieval_policies,
    retrieval_policy_versions,
)
from telegram_userbot.domain.context import ContextManifest
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.domain.shared.time import require_aware


@dataclass(frozen=True, slots=True)
class ContextSummaryRecord:
    manifest_id: UUID
    manifest_short_id: str
    purpose: str
    logical_role: str
    effective_input_budget: int
    input_token_estimate: int
    image_count: int
    omission_count: int
    memory_freshness: str
    builder_version: str
    context_policy_version: str
    retrieval_policy_version: str
    token_estimator_version: str
    layer_counts: Mapping[str, int]
    layer_tokens: Mapping[str, int]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreviewChallenge:
    request_id: UUID
    manifest_short_id: str
    expires_at: datetime
    confirmation_token: SensitiveValue[str]


@dataclass(frozen=True, slots=True)
class PreviewRequestRecord:
    request_id: UUID
    manifest_id: UUID
    manifest_sha256: bytes
    source_revision_vector_sha256: bytes
    state: str
    admin_user_id: int
    bot_chat_id: int
    bot_identity: str


@dataclass(frozen=True, slots=True)
class PreviewDeletionRecord:
    delivery_id: int
    request_id: UUID
    bot_identity: str
    bot_chat_id: int
    bot_message_id: int


class ContextRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_manifest(  # noqa: PLR0913 - manifest ownership is explicit
        self,
        *,
        account_id: UUID,
        conversation_id: UUID | None,
        turn_id: UUID | None,
        background_job_id: UUID | None,
        context_policy_version_id: UUID,
        retrieval_policy_version_id: UUID,
        prompt_bundle_sha256: bytes,
        capability_snapshot_sha256: bytes,
        manifest: ContextManifest,
        created_at: datetime,
    ) -> None:
        created_time = require_aware(created_at, "created_at")
        if (turn_id is None) == (background_job_id is None):
            raise ValueError("context_manifest_owner_required")
        if bytes.fromhex(manifest.prompt_bundle_sha256) != prompt_bundle_sha256:
            raise ValueError("context_prompt_snapshot_mismatch")
        if bytes.fromhex(manifest.capability_snapshot_sha256) != capability_snapshot_sha256:
            raise ValueError("context_capability_snapshot_mismatch")
        context_binding = await self._session.scalar(
            select(context_policy_versions.c.id)
            .join(context_policies, context_policies.c.id == context_policy_versions.c.policy_id)
            .where(
                context_policy_versions.c.id == context_policy_version_id,
                context_policies.c.logical_role == manifest.logical_role,
                context_policies.c.purpose == manifest.purpose,
            )
        )
        if context_binding != context_policy_version_id:
            raise ValueError("context_policy_version_binding_mismatch")
        retrieval_binding = await self._session.scalar(
            select(retrieval_policy_versions.c.id)
            .join(
                retrieval_policies,
                retrieval_policies.c.id == retrieval_policy_versions.c.policy_id,
            )
            .where(retrieval_policy_versions.c.id == retrieval_policy_version_id)
        )
        if retrieval_binding != retrieval_policy_version_id:
            raise ValueError("retrieval_policy_version_binding_mismatch")
        owner_kind = "turn" if turn_id is not None else "background_job"
        await self._session.execute(
            insert(context_manifests).values(
                id=manifest.id,
                account_id=account_id,
                conversation_id=conversation_id,
                owner_kind=owner_kind,
                turn_id=turn_id,
                background_job_id=background_job_id,
                purpose=manifest.purpose,
                logical_role=manifest.logical_role,
                builder_version=manifest.builder_version,
                prompt_version=manifest.prompt_version,
                prompt_bundle_sha256=prompt_bundle_sha256,
                context_policy_version_id=context_policy_version_id,
                retrieval_policy_version_id=retrieval_policy_version_id,
                retrieval_policy_version=manifest.retrieval_policy_version,
                token_policy_version=manifest.context_policy_version,
                token_estimator_version=manifest.token_estimator_version,
                capability_snapshot_sha256=capability_snapshot_sha256,
                memory_freshness=manifest.memory_freshness,
                effective_input_budget=manifest.effective_input_budget,
                safety_reserve_tokens=manifest.safety_reserve_tokens,
                estimated_instruction_tokens=manifest.estimated_instruction_tokens,
                estimated_text_tokens=manifest.estimated_text_tokens,
                estimated_image_tokens=manifest.estimated_image_tokens,
                estimated_structural_tokens=manifest.estimated_structural_tokens,
                input_token_estimate=manifest.input_token_estimate,
                image_count=sum(item.image_detail is not None for item in manifest.items),
                omission_count=len(manifest.omissions),
                source_revision_vector_sha256=bytes.fromhex(manifest.source_revision_vector_sha256),
                manifest_sha256=bytes.fromhex(manifest.manifest_sha256),
                created_at=created_time,
            )
        )
        for item in manifest.items:
            inserted_id = await self._session.scalar(
                insert(context_manifest_items)
                .values(
                    manifest_id=manifest.id,
                    account_id=account_id,
                    ordinal=item.ordinal,
                    layer=item.layer,
                    canonical_role=item.canonical_role,
                    source_actor=item.source_actor,
                    source_type=item.source_type,
                    source_id=UUID(item.source_id),
                    source_revision=item.source_revision,
                    prompt_version_id=(
                        UUID(item.source_id) if item.source_type == "trusted_instruction" else None
                    ),
                    message_revision_id=(
                        UUID(item.source_id) if item.source_type == "message_revision" else None
                    ),
                    media_object_id=(
                        UUID(item.source_id) if item.source_type == "media_object" else None
                    ),
                    memory_version_id=(
                        UUID(item.source_id) if item.source_type == "memory_version" else None
                    ),
                    summary_version_id=(
                        UUID(item.source_id) if item.source_type == "summary_version" else None
                    ),
                    trust_level=item.trust_level,
                    rank_position=item.rank_position,
                    base_score=item.base_score,
                    final_score=item.final_score,
                    image_detail=item.image_detail,
                    token_estimate=item.token_estimate,
                    estimated_image_tokens=item.estimated_image_tokens,
                    content_sha256=bytes.fromhex(item.content_sha256),
                    rendered_part_sha256=bytes.fromhex(item.rendered_part_sha256),
                )
                .returning(context_manifest_items.c.id)
            )
            if inserted_id is None:
                raise RuntimeError("context manifest item insert failed")
            for reason_ordinal, reason in enumerate(item.reasons, 1):
                await self._session.execute(
                    insert(context_manifest_item_reasons).values(
                        manifest_item_id=inserted_id,
                        reason_ordinal=reason_ordinal,
                        reason_code=reason,
                    )
                )
        for omission in manifest.omissions:
            layer, reason = omission.split(":", maxsplit=1)
            await self._session.execute(
                insert(context_manifest_omissions).values(
                    manifest_id=manifest.id,
                    layer=layer,
                    reason_code=reason,
                )
            )

    async def latest_summary(
        self, *, account_id: UUID, conversation_id: UUID
    ) -> ContextSummaryRecord | None:
        manifest = (
            (
                await self._session.execute(
                    select(context_manifests)
                    .where(
                        context_manifests.c.account_id == account_id,
                        context_manifests.c.conversation_id == conversation_id,
                        context_manifests.c.logical_role == "main_ai",
                    )
                    .order_by(context_manifests.c.created_at.desc(), context_manifests.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if manifest is None:
            return None
        rows = (
            (
                await self._session.execute(
                    select(
                        context_manifest_items.c.layer,
                        context_manifest_items.c.token_estimate,
                        context_manifest_items.c.estimated_image_tokens,
                    )
                    .where(
                        context_manifest_items.c.manifest_id == manifest["id"],
                        context_manifest_items.c.account_id == manifest["account_id"],
                    )
                    .order_by(context_manifest_items.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        counts: dict[str, int] = {}
        tokens: dict[str, int] = {}
        for row in rows:
            layer = cast(str, row["layer"])
            counts[layer] = counts.get(layer, 0) + 1
            tokens[layer] = (
                tokens.get(layer, 0)
                + cast(int, row["token_estimate"])
                + cast(int, row["estimated_image_tokens"])
            )
        manifest_id = cast(UUID, manifest["id"])
        return ContextSummaryRecord(
            manifest_id,
            str(manifest_id).split("-", maxsplit=1)[0],
            cast(str, manifest["purpose"]),
            cast(str, manifest["logical_role"]),
            cast(int, manifest["effective_input_budget"]),
            cast(int, manifest["input_token_estimate"]),
            cast(int, manifest["image_count"]),
            cast(int, manifest["omission_count"]),
            cast(str, manifest["memory_freshness"]),
            cast(str, manifest["builder_version"]),
            cast(str, manifest["token_policy_version"]),
            cast(str, manifest["retrieval_policy_version"]),
            cast(str, manifest["token_estimator_version"]),
            counts,
            tokens,
            cast(datetime, manifest["created_at"]),
        )

    async def issue_preview(  # noqa: PLR0913 - security bindings are explicit
        self,
        *,
        account_id: UUID,
        conversation_id: UUID,
        manifest_id: UUID,
        admin_user_id: int,
        bot_chat_id: int,
        bot_identity: str,
        now: datetime,
        ttl: timedelta = timedelta(minutes=5),
    ) -> PreviewChallenge:
        current_time = require_aware(now, "now")
        manifest = (
            (
                await self._session.execute(
                    select(
                        context_manifests.c.manifest_sha256,
                        context_manifests.c.source_revision_vector_sha256,
                    ).where(
                        context_manifests.c.id == manifest_id,
                        context_manifests.c.account_id == account_id,
                        context_manifests.c.conversation_id == conversation_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if manifest is None or bot_chat_id != admin_user_id:
            raise ValueError("context_preview_not_allowed")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).digest()
        request_id = uuid7()
        expires_at = current_time + ttl
        await self._session.execute(
            insert(context_preview_requests).values(
                id=request_id,
                bot_identity=bot_identity,
                admin_user_id=admin_user_id,
                bot_chat_id=bot_chat_id,
                account_id=account_id,
                conversation_id=conversation_id,
                context_manifest_id=manifest_id,
                manifest_sha256=manifest["manifest_sha256"],
                source_revision_vector_sha256=manifest["source_revision_vector_sha256"],
                state="pending_confirmation",
                token_expires_at=expires_at,
                created_at=current_time,
            )
        )
        await self._session.execute(
            insert(context_preview_tokens).values(
                id=uuid7(),
                request_id=request_id,
                admin_user_id=admin_user_id,
                bot_chat_id=bot_chat_id,
                purpose="context_preview_confirm",
                token_hash=token_hash,
                expires_at=expires_at,
                created_at=current_time,
            )
        )
        return PreviewChallenge(
            request_id,
            str(manifest_id).split("-", maxsplit=1)[0],
            expires_at,
            SensitiveValue(token),
        )

    async def consume_preview(
        self,
        *,
        token: SensitiveValue[str],
        admin_user_id: int,
        bot_chat_id: int,
        bot_identity: str,
        now: datetime,
    ) -> PreviewRequestRecord | None:
        current_time = require_aware(now, "now")
        token_hash = hashlib.sha256(token.reveal_for_use().encode()).digest()
        row = (
            (
                await self._session.execute(
                    select(context_preview_tokens, context_preview_requests)
                    .join(
                        context_preview_requests,
                        context_preview_requests.c.id == context_preview_tokens.c.request_id,
                    )
                    .where(
                        context_preview_tokens.c.admin_user_id == admin_user_id,
                        context_preview_tokens.c.bot_chat_id == bot_chat_id,
                        context_preview_tokens.c.used_at.is_(None),
                        context_preview_tokens.c.expires_at > current_time,
                        context_preview_requests.c.bot_identity == bot_identity,
                        context_preview_requests.c.state == "pending_confirmation",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        matched = next(
            (
                item
                for item in row
                if hmac.compare_digest(cast(bytes, item["token_hash"]), token_hash)
            ),
            None,
        )
        if matched is None:
            return None
        token_id = cast(UUID, matched["id"])
        request_id = cast(UUID, matched["request_id"])
        consumed_token = await self._session.scalar(
            update(context_preview_tokens)
            .where(
                context_preview_tokens.c.id == token_id, context_preview_tokens.c.used_at.is_(None)
            )
            .values(used_at=current_time)
            .returning(context_preview_tokens.c.id)
        )
        if consumed_token is None:
            return None
        confirmed_request = await self._session.scalar(
            update(context_preview_requests)
            .where(
                context_preview_requests.c.id == request_id,
                context_preview_requests.c.state == "pending_confirmation",
            )
            .values(state="confirmed", confirmed_at=current_time)
            .returning(context_preview_requests.c.id)
        )
        if confirmed_request is None:
            raise RuntimeError("context_preview_confirmation_conflict")
        return PreviewRequestRecord(
            request_id,
            cast(UUID, matched["context_manifest_id"]),
            cast(bytes, matched["manifest_sha256"]),
            cast(bytes, matched["source_revision_vector_sha256"]),
            "confirmed",
            admin_user_id,
            bot_chat_id,
            bot_identity,
        )

    async def begin_preview_delivery(self, *, request: PreviewRequestRecord) -> bool:
        claimed = await self._session.scalar(
            update(context_preview_requests)
            .where(
                context_preview_requests.c.id == request.request_id,
                context_preview_requests.c.context_manifest_id == request.manifest_id,
                context_preview_requests.c.manifest_sha256 == request.manifest_sha256,
                context_preview_requests.c.source_revision_vector_sha256
                == request.source_revision_vector_sha256,
                context_preview_requests.c.admin_user_id == request.admin_user_id,
                context_preview_requests.c.bot_identity == request.bot_identity,
                context_preview_requests.c.bot_chat_id == request.bot_chat_id,
                context_preview_requests.c.state == "confirmed",
            )
            .values(state="delivering")
            .returning(context_preview_requests.c.id)
        )
        return claimed == request.request_id

    async def record_preview_delivery(
        self,
        *,
        request: PreviewRequestRecord,
        states: tuple[tuple[str, int | None], ...],
        now: datetime,
        delete_after: datetime,
    ) -> None:
        current_time = require_aware(now, "now")
        deletion_time = require_aware(delete_after, "delete_after")
        for ordinal, (state, message_id) in enumerate(states, 1):
            await self._session.execute(
                insert(context_preview_deliveries).values(
                    request_id=request.request_id,
                    bot_identity=request.bot_identity,
                    bot_chat_id=request.bot_chat_id,
                    ordinal=ordinal,
                    state=state,
                    bot_message_id=message_id,
                    sent_at=current_time if state == "sent" else None,
                    delete_after=deletion_time if message_id is not None else None,
                    last_error_code="send_unknown" if state == "send_unknown" else None,
                )
            )
        delivered = sum(state == "sent" for state, _ in states)
        final_state = (
            "send_unknown" if any(state == "send_unknown" for state, _ in states) else "delivered"
        )
        completed = await self._session.scalar(
            update(context_preview_requests)
            .where(
                context_preview_requests.c.id == request.request_id,
                context_preview_requests.c.context_manifest_id == request.manifest_id,
                context_preview_requests.c.manifest_sha256 == request.manifest_sha256,
                context_preview_requests.c.source_revision_vector_sha256
                == request.source_revision_vector_sha256,
                context_preview_requests.c.admin_user_id == request.admin_user_id,
                context_preview_requests.c.bot_identity == request.bot_identity,
                context_preview_requests.c.bot_chat_id == request.bot_chat_id,
                context_preview_requests.c.state == "delivering",
            )
            .values(
                state=final_state,
                chunk_count=len(states),
                delivered_chunk_count=delivered,
                delivered_at=current_time,
                delete_after=deletion_time,
                last_error_code="send_unknown" if final_state == "send_unknown" else None,
            )
            .returning(context_preview_requests.c.id)
        )
        if completed != request.request_id:
            raise RuntimeError("context_preview_delivery_conflict")

    async def due_preview_deletions(
        self, *, bot_identity: str, now: datetime, limit: int = 50
    ) -> tuple[PreviewDeletionRecord, ...]:
        current_time = require_aware(now, "now")
        rows = (
            (
                await self._session.execute(
                    select(context_preview_deliveries)
                    .where(
                        context_preview_deliveries.c.bot_identity == bot_identity,
                        context_preview_deliveries.c.state.in_(("sent", "delete_failed")),
                        context_preview_deliveries.c.bot_message_id.is_not(None),
                        context_preview_deliveries.c.delete_after <= current_time,
                    )
                    .order_by(
                        context_preview_deliveries.c.delete_after,
                        context_preview_deliveries.c.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        records: list[PreviewDeletionRecord] = []
        for row in rows:
            await self._session.execute(
                update(context_preview_deliveries)
                .where(context_preview_deliveries.c.id == row["id"])
                .values(state="delete_pending")
            )
            records.append(
                PreviewDeletionRecord(
                    cast(int, row["id"]),
                    cast(UUID, row["request_id"]),
                    cast(str, row["bot_identity"]),
                    cast(int, row["bot_chat_id"]),
                    cast(int, row["bot_message_id"]),
                )
            )
        return tuple(records)

    async def finish_preview_deletion(
        self,
        *,
        deletion: PreviewDeletionRecord,
        deleted: bool,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        current_time = require_aware(now, "now")
        completed_delivery = await self._session.scalar(
            update(context_preview_deliveries)
            .where(
                context_preview_deliveries.c.id == deletion.delivery_id,
                context_preview_deliveries.c.request_id == deletion.request_id,
                context_preview_deliveries.c.bot_identity == deletion.bot_identity,
                context_preview_deliveries.c.bot_chat_id == deletion.bot_chat_id,
                context_preview_deliveries.c.bot_message_id == deletion.bot_message_id,
                context_preview_deliveries.c.state == "delete_pending",
            )
            .values(
                state="deleted" if deleted else "delete_failed",
                deleted_at=current_time if deleted else None,
                last_error_code=None if deleted else error_code or "delete_failed",
            )
            .returning(context_preview_deliveries.c.id)
        )
        if completed_delivery != deletion.delivery_id:
            raise RuntimeError("context_preview_deletion_conflict")
        states = tuple(
            await self._session.scalars(
                select(context_preview_deliveries.c.state).where(
                    context_preview_deliveries.c.request_id == deletion.request_id,
                    context_preview_deliveries.c.bot_identity == deletion.bot_identity,
                    context_preview_deliveries.c.bot_chat_id == deletion.bot_chat_id,
                )
            )
        )
        if states and all(state == "deleted" for state in states):
            request_state = "deleted"
        elif any(state == "delete_failed" for state in states):
            request_state = "delete_partial"
        else:
            request_state = "delete_pending"
        completed_request = await self._session.scalar(
            update(context_preview_requests)
            .where(
                context_preview_requests.c.id == deletion.request_id,
                context_preview_requests.c.bot_identity == deletion.bot_identity,
                context_preview_requests.c.bot_chat_id == deletion.bot_chat_id,
                context_preview_requests.c.state.in_(
                    ("delivered", "delete_pending", "delete_partial")
                ),
            )
            .values(
                state=request_state,
                completed_at=now if request_state in {"deleted", "delete_partial"} else None,
                last_error_code=(
                    error_code or "delete_failed" if request_state == "delete_partial" else None
                ),
            )
            .returning(context_preview_requests.c.id)
        )
        if completed_request != deletion.request_id:
            raise RuntimeError("context_preview_deletion_request_conflict")
