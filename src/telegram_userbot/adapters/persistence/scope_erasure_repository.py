"""Resumable scope quiescence, payload redaction, and media erasure preparation."""

import hashlib
import hmac
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Table, and_, func, null, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement, Select

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.derived_erasure_repository import (
    DerivedErasureRepository,
)
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository

FINALIZATION_PENDING = "ERASURE_MEDIA_INVENTORY_PENDING"


class ScopeErasureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prepare(  # noqa: PLR0913 - request and ledger identity are explicit
        self,
        *,
        account_id: UUID,
        request_id: UUID,
        contact_id: UUID | None,
        now: datetime,
        scope_secret: bytes,
        policy_version: int,
    ) -> None:
        """Caller holds the validated contact/account request lock and transaction."""
        conversations = select(s.conversations.c.id).where(
            s.conversations.c.account_id == account_id
        )
        contacts = select(s.contacts.c.id).where(s.contacts.c.account_id == account_id)
        if contact_id is not None:
            conversations = conversations.where(s.conversations.c.contact_id == contact_id)
            contacts = contacts.where(s.contacts.c.id == contact_id)
        # Share the account -> contact -> conversation lock order with admission.
        # Only marker columns are granted to the erasure worker.
        await self._session.execute(
            select(s.accounts.c.id)
            .where(s.accounts.c.id == account_id)
            .with_for_update(key_share=True)
        )
        if contact_id is None:
            await self._session.execute(
                update(s.accounts)
                .where(s.accounts.c.id == account_id)
                .values(status="deleting", updated_at=now)
            )
        await self._session.execute(
            update(s.contacts)
            .where(s.contacts.c.id.in_(contacts))
            .values(automation_status="deleting", proactive_enabled=False, updated_at=now)
        )
        await self._session.execute(
            update(s.conversations)
            .where(
                s.conversations.c.id.in_(conversations), s.conversations.c.contact_paused.is_(False)
            )
            .values(
                contact_paused=True, mode_version=s.conversations.c.mode_version + 1, updated_at=now
            )
        )
        await self._progress(request_id, "scope_quiescence", "completed", now)
        await self._session.execute(
            update(s.model_runs)
            .where(
                self._scope(s.model_runs, account_id, conversations, contact_id),
                s.model_runs.c.state.in_(("created", "running", "retry_wait", "output_ready")),
            )
            .values(
                state="cancelled",
                cancel_requested_at=now,
                error_code="ERASURE_SCOPE",
                completed_at=now,
            )
        )
        for table, terminal in ((s.memory_jobs, "cancelled"), (s.proactive_jobs, "expired")):
            await self._session.execute(
                update(table)
                .where(
                    self._scope(table, account_id, conversations, contact_id),
                    table.c.state.in_(("pending", "leased", "running", "retry_wait")),
                )
                .values(state=terminal, lease_owner=None, lease_expires_at=None, completed_at=now)
            )
        # The actual source redaction is one-way and replay-safe. Tombstones keep
        # Telegram deduplication intact while the remaining derived cleanup waits.
        messages = select(s.messages.c.id).where(
            self._scope(s.messages, account_id, conversations, contact_id)
        )
        await self._session.execute(
            update(s.messages)
            .where(s.messages.c.id.in_(messages))
            .values(is_tombstone=True, deleted_at=func.coalesce(s.messages.c.deleted_at, now))
        )
        await self._session.execute(
            update(s.message_revisions)
            .where(
                s.message_revisions.c.account_id == account_id,
                s.message_revisions.c.message_id.in_(messages),
                s.message_revisions.c.redacted_at.is_(None),
            )
            .values(
                text_content=None,
                caption=None,
                entities=null(),
                content_sha256=None,
                redacted_at=now,
                redaction_reason="contact_purge" if contact_id is not None else "account_wipe",
            )
        )
        await self._progress(request_id, "canonical_redaction", "completed", now)
        await DerivedErasureRepository(self._session).redact(
            account_id=account_id, contact_id=contact_id, now=now
        )
        await self._progress(request_id, "derived_redaction", "completed", now)
        await self._session.execute(
            text("SELECT public.redact_scope_metadata(:request, :now)"),
            {"request": request_id, "now": now},
        )
        await self._progress(request_id, "operational_metadata", "completed", now)
        await self._session.execute(
            text("SELECT public.redact_scope_retention(:request, :now)"),
            {"request": request_id, "now": now},
        )
        await self._progress(request_id, "retention_redaction", "completed", now)
        budget_complete = await ProactiveRepository(self._session).settle_erased_scope_budget(
            account_id=account_id, contact_id=contact_id, now=now
        )
        await self._progress(
            request_id, "budget_settlement", "completed" if budget_complete else "pending", now
        )
        exports = select(s.data_export_requests.c.id).where(
            s.data_export_requests.c.account_id == account_id,
            s.data_export_requests.c.erasure_requested_at.is_not(None),
            s.data_export_requests.c.erasure_cleaned_at.is_(None),
        )
        if contact_id is not None:
            exports = exports.where(
                or_(
                    s.data_export_requests.c.contact_id.is_(None),
                    s.data_export_requests.c.contact_id == contact_id,
                )
            )
        export_pending = await self._session.scalar(exports.limit(1)) is not None
        await self._progress(
            request_id, "export_artifacts", "pending" if export_pending else "completed", now
        )
        media_ids = self._media_ids(account_id, conversations, contact_id)
        protected = (
            self._shared_media_ids(account_id, conversations, contact_id)
            if contact_id is not None
            else None
        )
        shared_remaining = (
            protected is not None
            and await self._session.scalar(
                select(s.media_objects.c.id)
                .where(
                    s.media_objects.c.id.in_(media_ids),
                    s.media_objects.c.id.in_(protected),
                    s.media_objects.c.status != "deleted",
                )
                .limit(1)
            )
            is not None
        )
        await self._session.execute(
            update(s.media_objects)
            .where(
                s.media_objects.c.account_id == account_id,
                s.media_objects.c.id.in_(media_ids),
                *(() if protected is None else (s.media_objects.c.id.not_in(protected),)),
                s.media_objects.c.status != "deleted",
            )
            .values(
                delete_requested_at=func.coalesce(s.media_objects.c.delete_requested_at, now),
                expires_at=func.least(s.media_objects.c.expires_at, now),
            )
        )
        # Do not reset retry deadlines or fencing on failed/claimed deletes.
        remaining = await self._session.scalar(
            select(s.media_objects.c.id)
            .where(
                s.media_objects.c.account_id == account_id,
                s.media_objects.c.id.in_(media_ids),
                s.media_objects.c.status != "deleted",
            )
            .limit(1)
        )
        # Old uploads had no source binding before final attachment. A contact
        # cannot attest absence while those unregistered account files remain.
        unattributed = (
            contact_id is not None
            and await self._session.scalar(
                select(s.media_objects.c.id)
                .where(
                    s.media_objects.c.account_id == account_id,
                    s.media_objects.c.source_revision_id.is_(None),
                    s.media_objects.c.storage_key.is_(None),
                    s.media_objects.c.status.in_(
                        ("pending", "rejected", "failed", "delete_pending")
                    ),
                )
                .limit(1)
            )
            is not None
        )
        media_pending = remaining is not None or unattributed
        await self._progress(
            request_id, "physical_media", "pending" if media_pending else "completed", now
        )
        inventory_complete = (
            await self._session.scalar(
                select(s.erasure_media_checks.c.request_id).where(
                    s.erasure_media_checks.c.request_id == request_id
                )
            )
            is not None
        )
        await self._progress(
            request_id,
            "filesystem_inventory",
            "completed" if inventory_complete else "pending",
            now,
        )
        if not media_pending and not export_pending and budget_complete and inventory_complete:
            await self._finalize(
                account_id, request_id, contact_id, scope_secret, policy_version, now
            )
            return
        await self._progress(request_id, "scope_finalization", "pending", now)
        await self._state(
            request_id,
            "media_cleanup" if media_pending else "derived_cleanup",
            "ERASURE_MEDIA_SHARED_SCOPE"
            if shared_remaining
            else "ERASURE_MEDIA_UNATTRIBUTED_UPLOAD"
            if unattributed
            else "ERASURE_MEDIA_PENDING"
            if media_pending
            else (
                "ERASURE_EXPORT_PENDING"
                if export_pending
                else ("ERASURE_BUDGET_PENDING" if not budget_complete else FINALIZATION_PENDING)
            ),
            now,
        )

    async def _finalize(  # noqa: PLR0913, PLR0917 - ledger identity is explicit
        self,
        account_id: UUID,
        request_id: UUID,
        contact_id: UUID | None,
        scope_secret: bytes,
        policy_version: int,
        now: datetime,
    ) -> None:
        # All preceding redaction and eligibility checks share the request/account
        # locks and transaction with the cumulative ledger and terminal state.
        await self._session.execute(
            postgresql_insert(s.erasure_ledger)
            .values(
                account_scope_hmac=hmac.digest(scope_secret, account_id.bytes, "sha256"),
                scope_type="account" if contact_id is None else "contact",
                target_scope_hmac=hmac.digest(
                    scope_secret, (contact_id or account_id).bytes, "sha256"
                ),
                request_id=request_id,
                policy_version=policy_version,
                completed_at=now,
            )
            .on_conflict_do_nothing(index_elements=[s.erasure_ledger.c.request_id])
        )
        await self._session.execute(
            update(s.data_erasure_requests)
            .where(s.data_erasure_requests.c.id == request_id)
            .values(state="completed", completed_at=now, updated_at=now, last_error_code=None)
        )
        if (
            await self._session.scalar(
                select(s.audit_log.c.id).where(
                    s.audit_log.c.request_id == request_id,
                    s.audit_log.c.action == "scope_erasure_completed",
                )
            )
            is None
        ):
            await self._session.execute(
                postgresql_insert(s.audit_log).values(
                    account_id=account_id,
                    actor_type="system",
                    action="scope_erasure_completed",
                    target_type="erasure_request",
                    target_id=str(request_id),
                    result="success",
                    request_id=request_id,
                    metadata_schema_version=1,
                    metadata={},
                    occurred_at=now,
                )
            )
        await self._progress(request_id, "scope_finalization", "completed", now)

    @staticmethod
    def _scope(
        table: Table, account_id: UUID, conversations: Select[Any], contact_id: UUID | None
    ) -> ColumnElement[bool]:
        scope = table.c.account_id == account_id
        if contact_id is not None:
            scope = and_(scope, table.c.conversation_id.in_(conversations))
        return scope

    @staticmethod
    def _media_ids(
        account_id: UUID, conversations: Select[Any], contact_id: UUID | None
    ) -> Select[Any]:
        media = s.media_objects
        if contact_id is None:
            return select(media.c.id).where(media.c.account_id == account_id)
        linked = (
            select(s.message_media.c.media_object_id)
            .join(
                s.message_revisions,
                s.message_revisions.c.id == s.message_media.c.message_revision_id,
            )
            .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
            .where(
                s.message_media.c.account_id == account_id,
                s.messages.c.conversation_id.in_(conversations),
            )
        )
        context = (
            select(s.context_manifest_items.c.media_object_id)
            .join(
                s.context_manifests,
                s.context_manifests.c.id == s.context_manifest_items.c.manifest_id,
            )
            .where(
                s.context_manifests.c.account_id == account_id,
                s.context_manifests.c.conversation_id.in_(conversations),
            )
        )
        memory = (
            select(s.memory_input_manifest_items.c.media_object_id)
            .join(
                s.memory_input_manifests,
                s.memory_input_manifests.c.id == s.memory_input_manifest_items.c.manifest_id,
            )
            .where(
                s.memory_input_manifests.c.account_id == account_id,
                s.memory_input_manifests.c.conversation_id.in_(conversations),
            )
        )
        evidence = (
            select(s.memory_evidence.c.media_object_id)
            .join(
                s.memory_versions, s.memory_versions.c.id == s.memory_evidence.c.memory_version_id
            )
            .join(s.memories, s.memories.c.id == s.memory_versions.c.memory_id)
            .where(
                s.memory_evidence.c.account_id == account_id,
                or_(
                    s.memories.c.contact_id == contact_id,
                    s.memories.c.conversation_id.in_(conversations),
                ),
            )
        )
        proposal = (
            select(s.memory_proposal_evidence.c.media_object_id)
            .join(
                s.memory_proposals,
                s.memory_proposals.c.id == s.memory_proposal_evidence.c.proposal_id,
            )
            .where(
                s.memory_proposals.c.account_id == account_id,
                or_(
                    s.memory_proposals.c.contact_id == contact_id,
                    s.memory_proposals.c.conversation_id.in_(conversations),
                ),
            )
        )
        family = (
            select(media.c.id, media.c.parent_object_id)
            .where(
                media.c.account_id == account_id,
                or_(
                    media.c.id.in_(linked),
                    media.c.id.in_(context),
                    media.c.id.in_(memory),
                    media.c.id.in_(evidence),
                    media.c.id.in_(proposal),
                    media.c.source_revision_id.in_(
                        select(s.message_revisions.c.id)
                        .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
                        .where(
                            s.messages.c.conversation_id.in_(conversations),
                            s.messages.c.account_id == account_id,
                        )
                    ),
                ),
            )
            .cte("erasure_media_family", recursive=True)
        )
        family = family.union(
            select(media.c.id, media.c.parent_object_id)
            .join(
                family,
                or_(
                    media.c.parent_object_id == family.c.id, media.c.id == family.c.parent_object_id
                ),
            )
            .where(media.c.account_id == account_id)
        )
        return select(family.c.id)

    @staticmethod
    def _shared_media_ids(
        account_id: UUID, conversations: Select[Any], contact_id: UUID
    ) -> Select[Any]:
        # Preserve a media family if any unrelated conversation still references
        # it, including immutable model/memory manifests. Never unlink first.
        message = (
            select(s.message_media.c.media_object_id)
            .join(
                s.message_revisions,
                s.message_revisions.c.id == s.message_media.c.message_revision_id,
            )
            .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
            .where(
                s.message_media.c.account_id == account_id,
                s.message_revisions.c.redacted_at.is_(None),
                s.messages.c.is_tombstone.is_(False),
                s.messages.c.conversation_id.not_in(conversations),
            )
        )
        context = (
            select(s.context_manifest_items.c.media_object_id)
            .join(
                s.context_manifests,
                s.context_manifests.c.id == s.context_manifest_items.c.manifest_id,
            )
            .where(
                s.context_manifests.c.account_id == account_id,
                s.context_manifests.c.scope_erased_at.is_(None),
                s.context_manifests.c.conversation_id.not_in(conversations),
            )
        )
        memory = (
            select(s.memory_input_manifest_items.c.media_object_id)
            .join(
                s.memory_input_manifests,
                s.memory_input_manifests.c.id == s.memory_input_manifest_items.c.manifest_id,
            )
            .where(
                s.memory_input_manifests.c.account_id == account_id,
                s.memory_input_manifests.c.scope_erased_at.is_(None),
                s.memory_input_manifests.c.conversation_id.not_in(conversations),
            )
        )
        evidence = (
            select(s.memory_evidence.c.media_object_id)
            .join(
                s.memory_versions, s.memory_versions.c.id == s.memory_evidence.c.memory_version_id
            )
            .join(s.memories, s.memories.c.id == s.memory_versions.c.memory_id)
            .where(
                s.memory_evidence.c.account_id == account_id,
                s.memory_evidence.c.scope_erased_at.is_(None),
                or_(
                    s.memories.c.contact_id == contact_id,
                    s.memories.c.conversation_id.in_(conversations),
                ).is_not(True),
            )
        )
        proposal = (
            select(s.memory_proposal_evidence.c.media_object_id)
            .join(
                s.memory_proposals,
                s.memory_proposals.c.id == s.memory_proposal_evidence.c.proposal_id,
            )
            .where(
                s.memory_proposals.c.account_id == account_id,
                s.memory_proposal_evidence.c.scope_erased_at.is_(None),
                or_(
                    s.memory_proposals.c.contact_id == contact_id,
                    s.memory_proposals.c.conversation_id.in_(conversations),
                ).is_not(True),
            )
        )
        media = s.media_objects
        source = (
            select(s.message_revisions.c.id)
            .join(s.messages, s.messages.c.id == s.message_revisions.c.message_id)
            .where(
                s.messages.c.account_id == account_id,
                s.messages.c.conversation_id.not_in(conversations),
                s.message_revisions.c.redacted_at.is_(None),
                s.messages.c.is_tombstone.is_(False),
            )
        )
        family = (
            select(media.c.id, media.c.parent_object_id)
            .where(
                media.c.account_id == account_id,
                or_(
                    media.c.id.in_(message),
                    media.c.id.in_(context),
                    media.c.id.in_(memory),
                    media.c.id.in_(evidence),
                    media.c.id.in_(proposal),
                    media.c.source_revision_id.in_(source),
                ),
            )
            .cte("protected_media_family", recursive=True)
        )
        family = family.union(
            select(media.c.id, media.c.parent_object_id)
            .join(
                family,
                or_(
                    media.c.parent_object_id == family.c.id, media.c.id == family.c.parent_object_id
                ),
            )
            .where(media.c.account_id == account_id)
        )
        return select(family.c.id)

    async def _progress(self, request_id: UUID, step: str, state: str, now: datetime) -> None:
        await self._session.execute(
            postgresql_insert(s.erasure_progress)
            .values(
                request_id=request_id,
                step_name=step,
                state=state,
                idempotency_key=hashlib.sha256(request_id.bytes + step.encode("ascii")).digest(),
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_erasure_progress_request_step",
                set_={"state": state, "updated_at": now},
            )
        )

    async def _state(self, request_id: UUID, state: str, code: str, now: datetime) -> None:
        await self._session.execute(
            update(s.data_erasure_requests)
            .where(s.data_erasure_requests.c.id == request_id)
            .values(state=state, last_error_code=code, updated_at=now)
        )
