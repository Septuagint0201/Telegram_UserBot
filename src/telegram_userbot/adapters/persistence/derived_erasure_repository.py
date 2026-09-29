"""Clear derived payloads without asserting whole-scope erasure completion."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Table, and_, delete, func, null, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.scope_erasure_rules import PAYLOAD_ERASURE_V1

# UUIDs are only unique within each table; the kind is part of every graph node.
_DEPENDENCIES = text("""
WITH RECURSIVE
edges(source_kind, source_id, target_kind, target_id) AS (
  SELECT CASE WHEN message_revision_id IS NOT NULL THEN 'message'
              WHEN summary_version_id IS NOT NULL THEN 'summary' ELSE 'memory' END,
         coalesce(message_revision_id, summary_version_id, other_memory_version_id),
         'memory', memory_version_id FROM memory_evidence WHERE account_id = :account_id
  UNION
  SELECT CASE WHEN message_revision_id IS NOT NULL THEN 'message' ELSE 'summary' END,
         coalesce(message_revision_id, prior_summary_version_id), 'summary', summary_version_id
         FROM summary_version_sources WHERE account_id = :account_id
  UNION
  SELECT 'memory', a.id, 'memory', b.id FROM memory_versions a JOIN memory_versions b
    ON a.memory_id = b.memory_id AND a.account_id = b.account_id WHERE a.account_id = :account_id
  UNION
  SELECT 'summary', a.id, 'summary', b.id
    FROM summary_versions a JOIN summary_versions b
    ON a.summary_id = b.summary_id AND a.account_id = b.account_id WHERE a.account_id = :account_id
),
seeds(kind, id) AS (
  SELECT 'message', r.id FROM message_revisions r JOIN messages m ON m.id = r.message_id
    JOIN conversations c ON c.id = m.conversation_id
    WHERE r.account_id = :account_id AND
      (CAST(:contact_id AS uuid) IS NULL OR c.contact_id = :contact_id)
  UNION
  SELECT 'memory', v.id FROM memory_versions v JOIN memories m ON m.id = v.memory_id
    LEFT JOIN conversations c ON c.id = m.conversation_id
    WHERE v.account_id = :account_id AND
      (CAST(:contact_id AS uuid) IS NULL OR m.contact_id = :contact_id
       OR c.contact_id = :contact_id)
  UNION
  SELECT 'summary', v.id FROM summary_versions v JOIN summaries m ON m.id = v.summary_id
    JOIN conversations c ON c.id = m.conversation_id
    WHERE v.account_id = :account_id AND
      (CAST(:contact_id AS uuid) IS NULL OR c.contact_id = :contact_id)
),
affected(kind, id) AS (
  SELECT kind, id FROM seeds
  UNION
  SELECT e.target_kind, e.target_id FROM edges e JOIN affected a
    ON e.source_kind = a.kind AND e.source_id = a.id
)
SELECT kind, id FROM affected
""")


class DerivedErasureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def redact(  # noqa: PLR0912, PLR0915 - atomic, ordered payload transitions
        self, *, account_id: UUID, contact_id: UUID | None, now: datetime
    ) -> None:
        """Caller owns the scope's account lock; all steps share its transaction."""
        conversations = select(s.conversations.c.id).where(
            s.conversations.c.account_id == account_id
        )
        if contact_id is not None:
            conversations = conversations.where(s.conversations.c.contact_id == contact_id)

        def owned(table: Table) -> ColumnElement[bool]:
            condition = table.c.account_id == account_id
            if contact_id is not None:
                target: ColumnElement[bool] = table.c.conversation_id.in_(conversations)
                if "contact_id" in table.c:
                    target = or_(target, table.c.contact_id == contact_id)
                condition = and_(condition, target)
            return condition

        nodes: dict[str, list[UUID]] = {"message": [], "memory": [], "summary": []}
        for kind, identity in await self._session.execute(
            _DEPENDENCIES, {"account_id": account_id, "contact_id": contact_id}
        ):
            nodes[kind].append(identity)
        memory_versions = select(s.memory_versions.c.id).where(
            s.memory_versions.c.id.in_(nodes["memory"])
        )
        summary_versions = select(s.summary_versions.c.id).where(
            s.summary_versions.c.id.in_(nodes["summary"])
        )
        memories = select(s.memory_versions.c.memory_id).where(
            s.memory_versions.c.id.in_(memory_versions)
        )
        summaries = select(s.summary_versions.c.summary_id).where(
            s.summary_versions.c.id.in_(summary_versions)
        )
        proposals = select(s.memory_proposals.c.id).where(
            s.memory_proposals.c.account_id == account_id,
            or_(
                owned(s.memory_proposals),
                s.memory_proposals.c.accepted_memory_version_id.in_(memory_versions),
                s.memory_proposals.c.id.in_(
                    select(s.memory_proposal_targets.c.proposal_id).where(
                        s.memory_proposal_targets.c.target_memory_id.in_(memories)
                    )
                ),
                s.memory_proposals.c.id.in_(
                    select(s.memory_proposal_evidence.c.proposal_id).where(
                        s.memory_proposal_evidence.c.message_revision_id.in_(nodes["message"])
                    )
                ),
            ),
        )
        scopes: dict[str, ColumnElement[bool]] = {
            "memories": s.memories.c.id.in_(memories),
            "memory_versions": s.memory_versions.c.id.in_(memory_versions),
            "memory_evidence": or_(
                s.memory_evidence.c.memory_version_id.in_(memory_versions),
                s.memory_evidence.c.message_revision_id.in_(nodes["message"]),
            ),
            "memory_proposals": s.memory_proposals.c.id.in_(proposals),
            "memory_proposal_evidence": s.memory_proposal_evidence.c.proposal_id.in_(proposals),
            "summaries": s.summaries.c.id.in_(summaries),
            "summary_versions": s.summary_versions.c.id.in_(summary_versions),
            "summary_version_sources": s.summary_version_sources.c.summary_version_id.in_(
                summary_versions
            ),
        }
        for name in PAYLOAD_ERASURE_V1:
            table = s.metadata.tables[name]
            if name not in scopes and "conversation_id" in table.c:
                scopes[name] = owned(table)
        for name, parent_name, parent_key, extra in (
            ("memory_input_manifest_items", "memory_input_manifests", "manifest_id", True),
            ("context_manifest_items", "context_manifests", "manifest_id", True),
            ("proactive_input_manifest_items", "proactive_input_manifests", "manifest_id", False),
            ("proactive_occurrence_evidence", "proactive_occurrences", "occurrence_id", False),
        ):
            table, parent = s.metadata.tables[name], s.metadata.tables[parent_name]
            condition: ColumnElement[bool] = table.c[parent_key].in_(
                select(parent.c.id).where(owned(parent))
            )
            if extra:
                condition = or_(
                    condition,
                    table.c.memory_version_id.in_(memory_versions),
                    table.c.summary_version_id.in_(summary_versions),
                    table.c.message_revision_id.in_(nodes["message"]),
                )
            else:
                condition = or_(
                    condition,
                    and_(
                        table.c.source_type == "message_revision",
                        table.c.source_id.in_(nodes["message"]),
                    ),
                )
            scopes[name] = condition

        # A manifest containing even one erased dependency is unusable as a whole.
        # Freeze those identities before extending predicates, avoiding recursive SQL.
        for parent_name, child_name in (
            ("memory_input_manifests", "memory_input_manifest_items"),
            ("context_manifests", "context_manifest_items"),
            ("proactive_input_manifests", "proactive_input_manifest_items"),
        ):
            parent, child = s.metadata.tables[parent_name], s.metadata.tables[child_name]
            affected_manifests = list(
                await self._session.scalars(
                    select(child.c.manifest_id).where(
                        child.c.account_id == account_id, scopes[child_name]
                    )
                )
            )
            scopes[parent_name] = or_(scopes[parent_name], parent.c.id.in_(affected_manifests))
            scopes[child_name] = child.c.manifest_id.in_(
                select(parent.c.id).where(parent.c.account_id == account_id, scopes[parent_name])
            )
        for column, parent_name in (
            (s.model_runs.c.memory_input_manifest_id, "memory_input_manifests"),
            (s.model_runs.c.context_manifest_id, "context_manifests"),
            (s.model_runs.c.proactive_input_manifest_id, "proactive_input_manifests"),
        ):
            parent = s.metadata.tables[parent_name]
            scopes["model_runs"] = or_(
                scopes["model_runs"],
                column.in_(
                    select(parent.c.id).where(
                        parent.c.account_id == account_id, scopes[parent_name]
                    )
                ),
            )
        runs = select(s.model_runs.c.id).where(
            s.model_runs.c.account_id == account_id, scopes["model_runs"]
        )
        drafts = select(s.copilot_drafts.c.id).where(
            s.copilot_drafts.c.account_id == account_id,
            or_(owned(s.copilot_drafts), s.copilot_drafts.c.model_run_id.in_(runs)),
        )
        scopes["copilot_draft_revisions"] = s.copilot_draft_revisions.c.draft_id.in_(drafts)
        for name in ("outbound_intents", "outbound_delivery_groups"):
            table = s.metadata.tables[name]
            scopes[name] = or_(scopes[name], table.c.model_run_id.in_(runs))

        # Invalidate retrieval/publication state before dropping payloads. A crash
        # rolls the whole transaction back, and replay retains the first timestamps.
        await self._session.execute(
            update(s.memories)
            .where(s.memories.c.account_id == account_id, scopes["memories"])
            .values(
                status="forgotten",
                forgotten_at=func.coalesce(s.memories.c.forgotten_at, now),
                updated_at=now,
            )
        )
        await self._session.execute(
            update(s.summaries)
            .where(s.summaries.c.account_id == account_id, scopes["summaries"])
            .values(status="invalidated", updated_at=now)
        )
        await self._session.execute(
            update(s.summary_versions)
            .where(s.summary_versions.c.account_id == account_id, scopes["summary_versions"])
            .values(invalidation_state="invalidated")
        )
        await self._session.execute(
            update(s.memory_proposals)
            .where(
                s.memory_proposals.c.account_id == account_id,
                scopes["memory_proposals"],
                s.memory_proposals.c.state != "accepted",
            )
            .values(state="invalidated", validation_code="ERASURE_SCOPE", decided_at=now)
        )
        # Actual vector payloads and source hashes have no audit dependents.
        await self._session.execute(
            delete(s.embedding_records).where(
                s.embedding_records.c.account_id == account_id,
                or_(
                    s.embedding_records.c.memory_version_id.in_(memory_versions),
                    s.embedding_records.c.summary_version_id.in_(summary_versions),
                    s.embedding_records.c.message_revision_id.in_(nodes["message"]),
                ),
            )
        )
        await self._session.execute(
            update(s.model_runs)
            .where(
                s.model_runs.c.account_id == account_id,
                scopes["model_runs"],
                s.model_runs.c.state.in_(("created", "running", "retry_wait", "output_ready")),
            )
            .values(
                state="cancelled",
                cancel_requested_at=now,
                completed_at=now,
                error_code="ERASURE_SCOPE",
            )
        )
        for name in ("proactive_life_events", "proactive_intentions", "proactive_relationships"):
            table = s.metadata.tables[name]
            await self._session.execute(
                update(table)
                .where(table.c.account_id == account_id, scopes[name])
                .values(status="invalidated")
            )
        await self._session.execute(
            update(s.proactive_decisions)
            .where(s.proactive_decisions.c.account_id == account_id, scopes["proactive_decisions"])
            .values(state="stale")
        )
        scopes["copilot_drafts"] = s.copilot_drafts.c.id.in_(drafts)
        await self._cancel_unsent(account_id, scopes, now)
        for name, rule in PAYLOAD_ERASURE_V1.items():
            table = s.metadata.tables[name]
            values: dict[str, Any] = dict.fromkeys(rule.null_columns, null())
            values.update({column: {} for column in rule.empty_objects})
            values["scope_erased_at"] = now
            if "redacted_at" in table.c:
                values["redacted_at"] = func.coalesce(table.c.redacted_at, now)
            if "redaction_reason" in table.c:
                values["redaction_reason"] = "contact_purge" if contact_id else "account_wipe"
            await self._session.execute(
                update(table)
                .where(
                    table.c.account_id == account_id,
                    scopes[name],
                    table.c.scope_erased_at.is_(None),
                )
                .values(**values)
            )

    async def _cancel_unsent(
        self, account_id: UUID, scopes: dict[str, ColumnElement[bool]], now: datetime
    ) -> None:
        await self._session.execute(
            update(s.outbound_intents)
            .where(
                s.outbound_intents.c.account_id == account_id,
                scopes["outbound_intents"],
                s.outbound_intents.c.state.in_(("pending", "retry_wait")),
            )
            .values(
                state="cancelled",
                next_attempt_at=None,
                last_error_code="ERASURE_SCOPE",
                updated_at=now,
            )
        )
        # Keep sending/unknown/sent outcomes and their fencing identifiers intact.
        await self._session.execute(
            update(s.outbound_delivery_groups)
            .where(
                s.outbound_delivery_groups.c.account_id == account_id,
                scopes["outbound_delivery_groups"],
                s.outbound_delivery_groups.c.state == "planned",
            )
            .values(state="cancelled", updated_at=now)
        )
        await self._session.execute(
            update(s.copilot_drafts)
            .where(
                s.copilot_drafts.c.account_id == account_id,
                scopes["copilot_drafts"],
                s.copilot_drafts.c.state.in_(
                    ("requested", "collecting", "generating", "ready", "editing", "approved")
                ),
            )
            .values(state="invalidated", terminal_at=now, terminal_reason="ERASURE_SCOPE")
        )
