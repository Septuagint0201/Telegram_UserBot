"""Resumable shadow backfill and database-derived coverage before atomic switch.

The records themselves are the durable checkpoint: an anti-join finds missing
targets, so a restart or a late source never relies on an in-memory cursor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import and_, exists, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.embedding_runtime import EmbeddingRuntimeRepository


def current_targets(account_id: UUID) -> Any:
    """One row per current, nonempty canonical body in an admissible scope."""
    queries = []
    for kind, versions, parents, parent_key, body in (
        (
            "memory_version",
            s.memory_versions,
            s.memories,
            "memory_id",
            s.memory_versions.c.rendered_text,
        ),
        (
            "summary_version",
            s.summary_versions,
            s.summaries,
            "summary_id",
            s.summary_versions.c.content_text,
        ),
        (
            "message_revision",
            s.message_revisions,
            s.messages,
            "message_id",
            func.coalesce(s.message_revisions.c.text_content, s.message_revisions.c.caption),
        ),
    ):
        version = versions.c.revision_no if kind == "message_revision" else versions.c.version_no
        pointer = (
            parents.c.current_revision_no
            if kind == "message_revision"
            else parents.c.current_version_no
        )
        eligible: list[Any] = (
            [
                parents.c.deleted_at.is_(None),
                parents.c.is_tombstone.is_(False),
                versions.c.body_kind.in_(("text", "caption")),
                versions.c.content_sha256.is_not(None),
            ]
            if kind == "message_revision"
            else [parents.c.status == "active"]
        )
        if kind == "summary_version":
            eligible.append(versions.c.invalidation_state == "active")
        queries.append(
            select(literal(kind).label("kind"), versions.c.id.label("id"), body.label("body"))
            .join(
                parents,
                and_(
                    parents.c.id == versions.c[parent_key],
                    parents.c.account_id == versions.c.account_id,
                ),
            )
            .join(s.conversations, s.conversations.c.id == parents.c.conversation_id)
            .join(s.contacts, s.contacts.c.id == s.conversations.c.contact_id)
            .where(
                versions.c.account_id == account_id,
                pointer == version,
                versions.c.redacted_at.is_(None),
                func.length(body) > 0,
                *eligible,
                s.conversations.c.deleted_at.is_(None),
                s.contacts.c.deleted_at.is_(None),
                s.contacts.c.automation_status != "deleting",
                ~exists(
                    select(1).where(
                        s.data_erasure_requests.c.account_id == account_id,
                        or_(
                            s.data_erasure_requests.c.scope_type == "account",
                            and_(
                                s.data_erasure_requests.c.scope_type == "contact",
                                s.data_erasure_requests.c.contact_id == s.contacts.c.id,
                            ),
                            and_(
                                s.data_erasure_requests.c.scope_type == "memory",
                                s.data_erasure_requests.c.memory_id == parents.c.id,
                            )
                            if kind == "memory_version"
                            else literal(False),
                        ),
                    )
                ),
            )
        )
    return union_all(*queries).subquery("current_embedding_targets")


def record_matches(targets: Any) -> Any:
    return or_(
        *(
            and_(targets.c.kind == kind, s.embedding_records.c[f"{kind}_id"] == targets.c.id)
            for kind in ("message_revision", "memory_version", "summary_version")
        )
    )


class EmbeddingRebuildRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_spaces(self, *, now: datetime) -> int:
        """A newly activated model configuration receives its own shadow space."""
        configs = (
            (
                await self.session.execute(
                    select(
                        s.model_config_versions,
                        s.model_profiles.c.id.label("profile_id"),
                        s.model_capability_snapshots.c.embedding_dimensions,
                    )
                    .join(
                        s.model_profiles,
                        and_(
                            s.model_profiles.c.id == s.model_config_versions.c.profile_id,
                            s.model_profiles.c.active_config_version_no
                            == s.model_config_versions.c.version_no,
                        ),
                    )
                    .join(
                        s.model_capability_snapshots,
                        s.model_capability_snapshots.c.id
                        == s.model_config_versions.c.capability_snapshot_id,
                    )
                    .where(
                        s.model_profiles.c.logical_role == "embedding",
                        s.model_profiles.c.state == "active",
                        s.model_config_versions.c.enabled.is_(True),
                        s.model_capability_snapshots.c.status == "valid",
                        s.model_capability_snapshots.c.observed_at <= now,
                        s.model_capability_snapshots.c.expires_at > now,
                    )
                )
            )
            .mappings()
            .all()
        )
        accounts = (
            (
                await self.session.execute(
                    select(s.accounts.c.id).where(
                        s.accounts.c.status == "active", s.accounts.c.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        count = 0
        for config in configs:
            dimensions = config["protocol_options"].get("dimensions")
            if dimensions is None and len(config["embedding_dimensions"]) == 1:
                dimensions = config["embedding_dimensions"][0]
            if dimensions not in config["embedding_dimensions"]:
                continue
            for account_id in accounts:
                existing = await self.session.scalar(
                    select(s.embedding_spaces.c.id).where(
                        s.embedding_spaces.c.account_id == account_id,
                        s.embedding_spaces.c.config_version_id == config["id"],
                    )
                )
                if existing is not None:
                    continue
                generation = 1 + (
                    await self.session.scalar(
                        select(func.max(s.embedding_spaces.c.generation)).where(
                            s.embedding_spaces.c.account_id == account_id,
                            s.embedding_spaces.c.model_profile_id == config["profile_id"],
                        )
                    )
                    or 0
                )
                created = await self.session.scalar(
                    insert(s.embedding_spaces)
                    .values(
                        id=uuid5(account_id, f"embedding-shadow:{config['id']}"),
                        account_id=account_id,
                        model_profile_id=config["profile_id"],
                        config_version_id=config["id"],
                        model_name_snapshot=config["model_name"],
                        dimensions=dimensions,
                        distance_metric="cosine",
                        normalization="l2",
                        chunker_version="v1",
                        state="building",
                        generation=generation,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                    .returning(s.embedding_spaces.c.id)
                )
                count += int(created is not None)
        return count

    async def advance(self, *, now: datetime, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("embedding rebuild batch invalid")
        spaces = (
            (
                await self.session.execute(
                    select(s.embedding_spaces)
                    .where(
                        s.embedding_spaces.c.state == "building",
                        s.embedding_spaces.c.account_id.is_not(None),
                    )
                    .order_by(s.embedding_spaces.c.created_at, s.embedding_spaces.c.id)
                    .limit(10)
                )
            )
            .mappings()
            .all()
        )
        count = 0
        for space in spaces:
            # Match the canonical writers' account -> space lock order. This
            # keeps the final coverage snapshot stable until activation commits.
            await self.session.execute(
                select(s.accounts.c.id)
                .where(s.accounts.c.id == space["account_id"])
                .with_for_update()
            )
            locked = (
                (
                    await self.session.execute(
                        select(s.embedding_spaces)
                        .where(
                            s.embedding_spaces.c.id == space["id"],
                            s.embedding_spaces.c.state == "building",
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if locked is None:
                continue
            targets = current_targets(space["account_id"])
            matching = and_(
                s.embedding_records.c.embedding_space_id == space["id"], record_matches(targets)
            )
            missing = (
                await self.session.execute(
                    select(targets.c.kind, targets.c.id)
                    .where(~exists(select(1).where(matching)))
                    .order_by(targets.c.kind, targets.c.id)
                    .limit(limit)
                )
            ).all()
            for kind, target_id in missing:
                count += await EmbeddingRuntimeRepository(self.session).stage_target(
                    account_id=space["account_id"],
                    target_kind=kind,
                    target_id=target_id,
                    space_id=space["id"],
                    now=now,
                )
            if missing:
                continue
            # Match chunk count AND every canonical chunk hash in one fresh SQL
            # snapshot. A ready-but-stale row cannot count towards coverage.
            records = s.embedding_records
            expected_count = func.ceil(func.greatest(func.length(targets.c.body) - 100, 1) / 1400.0)
            actual_count = select(func.count()).where(matching).correlate(targets).scalar_subquery()
            chunk_body = func.substr(targets.c.body, records.c.chunk_index * 1400 + 1, 1500)
            invalid = exists(
                select(1).where(
                    matching,
                    or_(
                        records.c.state != "ready",
                        records.c.invalidated_at.is_not(None),
                        records.c.dimensions != space["dimensions"],
                        records.c.chunker_version != "v1",
                        records.c.source_sha256 != func.sha256(func.convert_to(chunk_body, "UTF8")),
                        func.jsonb_array_length(records.c.vector_payload) != space["dimensions"],
                    ),
                )
            )
            incomplete = await self.session.scalar(
                select(
                    exists(
                        select(1)
                        .select_from(targets)
                        .where(or_(actual_count != expected_count, invalid))
                    )
                )
            )
            if incomplete:
                failed = await self.session.scalar(
                    select(
                        exists(
                            select(1)
                            .select_from(targets)
                            .where(exists(select(1).where(matching, records.c.state == "failed")))
                        )
                    )
                )
                if failed:
                    await self.session.execute(
                        update(s.embedding_spaces)
                        .where(s.embedding_spaces.c.id == space["id"])
                        .values(state="failed")
                    )
                continue
            # Configuration may have changed while backfilling. Keep the prior
            # active space until the current configuration's shadow is complete.
            current = await self.session.scalar(
                select(s.model_config_versions.c.id)
                .join(
                    s.model_profiles, s.model_profiles.c.id == s.model_config_versions.c.profile_id
                )
                .where(
                    s.model_profiles.c.id == space["model_profile_id"],
                    s.model_profiles.c.state == "active",
                    s.model_config_versions.c.version_no
                    == s.model_profiles.c.active_config_version_no,
                )
            )
            if current != space["config_version_id"]:
                continue
            await self.session.execute(
                update(s.embedding_spaces)
                .where(
                    s.embedding_spaces.c.account_id == space["account_id"],
                    s.embedding_spaces.c.model_profile_id == space["model_profile_id"],
                    s.embedding_spaces.c.state == "active",
                )
                .values(state="retired", retired_at=now)
            )
            await self.session.execute(
                update(s.embedding_spaces)
                .where(s.embedding_spaces.c.id == space["id"])
                .values(state="active", activated_at=now, retired_at=None)
            )
            count += 1
        return count

    async def request_rollback(self, *, account_id: UUID, space_id: UUID) -> bool:
        """Revalidate/backfill a retired space after its config has been restored."""
        row = await self.session.scalar(
            update(s.embedding_spaces)
            .where(
                s.embedding_spaces.c.id == space_id,
                s.embedding_spaces.c.account_id == account_id,
                s.embedding_spaces.c.state == "retired",
            )
            .values(state="building")
            .returning(s.embedding_spaces.c.id)
        )
        return row is not None
