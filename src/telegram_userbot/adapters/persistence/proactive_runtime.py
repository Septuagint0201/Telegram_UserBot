"""Current proactive scope, canonical evidence and bounded occurrence scans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_inputs import (
    MemoryPipelineError,
    message_source_query,
)
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.domain.conversation.mode import (
    AccountControl,
    BaseMode,
    ConversationControl,
    MaintenanceState,
    ModeResolution,
    resolve_mode,
)
from telegram_userbot.domain.proactive.models import (
    Candidate,
    CandidateState,
    ContactSettings,
    OccurrenceState,
    ProactivePolicy,
    ReasonCode,
    RelationshipLevel,
    RuleOccurrence,
    TypedEvidence,
    membership_digest,
)
from telegram_userbot.domain.proactive.rules import (
    ExplicitFollowupFact,
    IntentionFact,
    LifeEventFact,
    RelationshipFact,
    aggregate_candidates,
    filter_occurrences,
    materialize_explicit_followup,
    materialize_intention,
    materialize_life_event,
    materialize_reconnect,
)
from telegram_userbot.domain.shared.time import require_aware


class ProactiveRuntimeError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code, self.retryable = code, retryable
        super().__init__(code)


@dataclass(frozen=True)
class ProactiveScope:
    account: Any
    conversation: Any
    contact: Any
    policy_row: Any
    policy: ProactivePolicy
    settings: ContactSettings
    resolution: ModeResolution
    last_activity: datetime | None
    activity_revision: int
    last_proactive: datetime | None
    conflicting: bool


def policy_value(row: Any) -> ProactivePolicy:
    options = dict(row["settings_json"])
    for key in (
        "close_min_interval",
        "friend_min_interval",
        "acquaintance_min_interval",
        "close_reconnect_after",
        "friend_reconnect_after",
    ):
        if key in options:
            options[key] = timedelta(seconds=options[key])
    if "allowed_reasons" in options:
        options["allowed_reasons"] = frozenset(
            ReasonCode(value) for value in options["allowed_reasons"]
        )
    fixed = {
        "version_id": row["id"],
        "version_no": row["version_no"],
        "enabled": row["enabled"],
        "quiet_start_local": time.fromisoformat(row["quiet_start_local"]),
        "quiet_end_local": time.fromisoformat(row["quiet_end_local"]),
        "account_daily_limit": row["account_daily_limit"],
        "contact_bypass_daily_limit": row["contact_bypass_daily_limit"],
        "activity_suppression_seconds": row["activity_suppression_seconds"],
    }
    if options.keys() & fixed.keys():
        raise ProactiveRuntimeError("PROACTIVE_POLICY_INVALID")
    try:
        return ProactivePolicy(**fixed, **options)
    except TypeError, ValueError:
        raise ProactiveRuntimeError("PROACTIVE_POLICY_INVALID") from None


def timestamp(value: Any) -> datetime | None:
    return require_aware(datetime.fromisoformat(value), "projection time") if value else None


class ProactiveRuntimeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.scan_interval_seconds: int | None = None

    async def scope(
        self,
        conversation_id: UUID,
        *,
        now: datetime,
        own_turn: UUID | None = None,
        own_decision: UUID | None = None,
    ) -> ProactiveScope:
        account_id = await self.session.scalar(
            select(s.conversations.c.account_id).where(s.conversations.c.id == conversation_id)
        )
        account = (
            (
                await self.session.execute(
                    select(s.accounts)
                    .where(s.accounts.c.id == account_id)
                    .with_for_update(key_share=True)
                )
            )
            .mappings()
            .one()
        )
        control = (
            (
                await self.session.execute(
                    select(s.account_orchestrator_states)
                    .where(s.account_orchestrator_states.c.account_id == account_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        conversation = (
            (
                await self.session.execute(
                    select(s.conversations)
                    .where(s.conversations.c.id == conversation_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        contact = (
            (
                await self.session.execute(
                    select(s.contacts)
                    .where(s.contacts.c.id == conversation["contact_id"])
                    .with_for_update(key_share=True)
                )
            )
            .mappings()
            .one()
        )
        erased = await self.session.scalar(
            select(s.data_erasure_requests.c.id)
            .where(
                s.data_erasure_requests.c.account_id == account_id,
                or_(
                    s.data_erasure_requests.c.scope_type == "account",
                    and_(
                        s.data_erasure_requests.c.scope_type == "contact",
                        s.data_erasure_requests.c.contact_id == contact["id"],
                    ),
                ),
            )
            .limit(1)
        )
        if (
            control is None
            or account["status"] != "active"
            or any(row["deleted_at"] is not None for row in (account, conversation, contact))
            or erased is not None
        ):
            raise ProactiveRuntimeError("PROACTIVE_SCOPE_UNAVAILABLE")
        policy = (
            (
                await self.session.execute(
                    select(s.proactive_policies)
                    .where(s.proactive_policies.c.account_id == account_id)
                    .order_by(s.proactive_policies.c.version_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        settings = (
            (
                await self.session.execute(
                    select(s.proactive_contact_settings)
                    .where(
                        s.proactive_contact_settings.c.account_id == account_id,
                        s.proactive_contact_settings.c.contact_id == contact["id"],
                    )
                    .order_by(s.proactive_contact_settings.c.version_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if policy is None:
            raise ProactiveRuntimeError("PROACTIVE_POLICY_UNAVAILABLE")
        setting = ContactSettings(
            contact["id"],
            version=settings["version_no"] if settings else 1,
            enabled=bool(
                contact["proactive_enabled"] and (settings is None or settings["enabled"])
            ),
            daily_limit=settings["daily_limit"] if settings else None,
            minimum_interval=timedelta(seconds=settings["minimum_interval_seconds"])
            if settings and settings["minimum_interval_seconds"]
            else None,
            relationship_level=RelationshipLevel(settings["relationship_level"])
            if settings
            else RelationshipLevel.UNKNOWN,
            timezone_name=(settings["timezone_name"] if settings else None)
            or contact["timezone"]
            or account["default_timezone"]
            or policy["timezone_name"],
        )
        resolution = resolve_mode(
            account=AccountControl(
                BaseMode(control["default_base_mode"]),
                control["global_paused"],
                MaintenanceState(control["maintenance_state"]),
                control["control_version"],
            ),
            conversation=ConversationControl(
                BaseMode(conversation["base_mode_override"])
                if conversation["base_mode_override"]
                else None,
                conversation["contact_paused"],
                conversation["temporary_human_until"],
                conversation["mode_version"],
                conversation["content_revision"],
            ),
            now=now,
            contact_automation_status=contact["automation_status"],
        )
        revision, activity = (
            await self.session.execute(
                select(
                    func.max(s.message_events.c.id), func.max(s.message_events.c.observed_at)
                ).where(s.message_events.c.conversation_id == conversation_id)
            )
        ).one()
        conflict = await self.session.scalar(
            select(
                exists(
                    select(1).where(
                        s.conversation_turns.c.conversation_id == conversation_id,
                        s.conversation_turns.c.state.in_(
                            ("collecting", "ready", "generating", "output_ready")
                        ),
                        s.conversation_turns.c.id != own_turn if own_turn else true(),
                    )
                )
            )
        )
        last = await self.session.scalar(
            select(func.max(s.proactive_budget_reservations.c.committed_at)).where(
                s.proactive_budget_reservations.c.conversation_id == conversation_id,
                s.proactive_budget_reservations.c.state.in_(("committed", "send_unknown")),
                s.proactive_budget_reservations.c.decision_id != own_decision
                if own_decision is not None
                else true(),
            )
        )
        return ProactiveScope(
            account,
            conversation,
            contact,
            policy,
            policy_value(policy),
            setting,
            resolution,
            activity,
            int(revision or 0),
            last,
            bool(conflict),
        )

    async def evidence_current(
        self, evidence: TypedEvidence, *, account: UUID, conversation: UUID
    ) -> bool:
        # Only canonical message roots may authorize production proactive work.
        # Projection summaries cannot attest to their own correctness.
        if evidence.source_type != "message_revision" or not evidence.valid:
            return False
        row = (
            (
                await self.session.execute(
                    message_source_query(account, conversation).where(
                        s.message_revisions.c.id == evidence.source_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return bool(
            row is not None
            and evidence.source_version == f"revision-{row['revision_no']}"
            and evidence.source_hash == row["content_sha256"]
            and row["message_source"] in {"telegram_user", "human"}
        )

    async def candidate(self, candidate_id: UUID, *, now: datetime) -> Candidate:
        row = (
            (
                await self.session.execute(
                    select(s.proactive_candidates)
                    .where(s.proactive_candidates.c.id == candidate_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        occurrences = []
        members = (
            (
                await self.session.execute(
                    select(
                        s.proactive_occurrences,
                        s.proactive_candidate_memberships.c.occurrence_key.label("member_key"),
                        s.proactive_candidate_memberships.c.occurrence_generation.label(
                            "member_generation"
                        ),
                    )
                    .join(
                        s.proactive_candidate_memberships,
                        s.proactive_candidate_memberships.c.occurrence_id
                        == s.proactive_occurrences.c.id,
                    )
                    .where(s.proactive_candidate_memberships.c.candidate_id == candidate_id)
                    .order_by(s.proactive_candidate_memberships.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        for item in members:
            evidence_rows = (
                (
                    await self.session.execute(
                        select(s.proactive_occurrence_evidence)
                        .where(s.proactive_occurrence_evidence.c.occurrence_id == item["id"])
                        .order_by(s.proactive_occurrence_evidence.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            evidence = tuple(
                TypedEvidence(
                    **{
                        key: item[key]
                        for key in (
                            "source_type",
                            "source_id",
                            "source_version",
                            "source_hash",
                            "summary",
                            "current",
                            "active",
                            "explicit",
                        )
                    }
                )
                for item in evidence_rows
            )
            if (
                item["state"] not in {"eligible", "materialized"}
                or item["member_key"] != item["occurrence_key"]
                or item["member_generation"] != item["generation"]
            ):
                raise ProactiveRuntimeError("PROACTIVE_EVIDENCE_CHANGED")
            for source in evidence:
                if not await self.evidence_current(
                    source, account=row["account_id"], conversation=row["conversation_id"]
                ):
                    raise ProactiveRuntimeError("PROACTIVE_EVIDENCE_CHANGED")
            # The formal fact itself must still be current as well as its roots.
            fact = await self.session.scalar(
                select(s.memories.c.id).where(
                    s.memories.c.id == item["source_id"],
                    s.memories.c.account_id == row["account_id"],
                    s.memories.c.conversation_id == row["conversation_id"],
                    s.memories.c.status == "active",
                    s.memories.c.current_version_no == int(item["source_version"]),
                )
            )
            if fact is None:
                raise ProactiveRuntimeError("PROACTIVE_FACT_CHANGED")
            occurrences.append(
                RuleOccurrence(
                    **{
                        key: item[key]
                        for key in (
                            "id",
                            "account_id",
                            "contact_id",
                            "conversation_id",
                            "occurrence_key",
                            "generation",
                            "window_start_at",
                            "window_end_at",
                            "hard_deadline_at",
                            "timezone_name",
                            "local_date",
                            "source_type",
                            "source_id",
                            "source_version",
                            "quiet_bypass_possible",
                            "policy_version_id",
                            "contact_setting_version",
                            "relationship_state_version",
                        )
                    },
                    reason=ReasonCode(item["reason"]),
                    state=OccurrenceState(item["state"]),
                    importance=float(item["importance"]),
                    evidence=evidence,
                )
            )
        value = Candidate(
            **{
                key: row[key]
                for key in (
                    "id",
                    "account_id",
                    "contact_id",
                    "conversation_id",
                    "candidate_key",
                    "generation",
                    "membership_hash",
                    "window_start_at",
                    "window_end_at",
                    "policy_version_id",
                    "timezone_name",
                    "mode_version",
                    "content_revision",
                    "activity_revision",
                    "contact_setting_version",
                    "relationship_state_version",
                )
            },
            state=CandidateState(row["state"]),
            occurrences=tuple(occurrences),
        )
        if membership_digest(value.occurrences) != value.membership_hash:
            raise ProactiveRuntimeError("PROACTIVE_MEMBERSHIP_CHANGED")
        return value

    async def materialize(  # noqa: PLR0912 - typed fact kinds are deliberately distinct
        self, scope: ProactiveScope, *, secret: bytes, now: datetime
    ) -> int:
        conversation, account = scope.conversation["id"], scope.account["id"]
        facts = (
            (
                await self.session.execute(
                    select(
                        s.memories,
                        s.memory_versions.c.id.label("version_id"),
                        s.memory_versions.c.payload,
                        s.memory_versions.c.rendered_text,
                        s.memory_versions.c.importance,
                    )
                    .join(
                        s.memory_versions,
                        and_(
                            s.memory_versions.c.memory_id == s.memories.c.id,
                            s.memory_versions.c.version_no == s.memories.c.current_version_no,
                        ),
                    )
                    .where(
                        s.memories.c.conversation_id == conversation,
                        s.memories.c.status == "active",
                        s.memory_versions.c.redacted_at.is_(None),
                        s.memories.c.memory_type.in_(("event", "intention", "relationship")),
                    )
                    .order_by(s.memories.c.id)
                )
            )
            .mappings()
            .all()
        )
        occurrences: list[RuleOccurrence] = []
        for fact in facts:
            roots = (
                (
                    await self.session.execute(
                        select(s.memory_evidence).where(
                            s.memory_evidence.c.memory_version_id == fact["version_id"]
                        )
                    )
                )
                .mappings()
                .all()
            )
            evidence = []
            for root in roots:
                revision = (
                    (
                        await self.session.execute(
                            message_source_query(account, conversation).where(
                                s.message_revisions.c.id == root["message_revision_id"]
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    revision is None
                    or revision["message_source"] not in {"telegram_user", "human"}
                    or revision["content_sha256"] != root["source_content_sha256"]
                ):
                    break
                evidence.append(
                    TypedEvidence(
                        "message_revision",
                        revision["id"],
                        f"revision-{revision['revision_no']}",
                        revision["content_sha256"],
                        fact["rendered_text"][:500],
                    )
                )
            else:
                if not evidence:
                    continue
                try:
                    payload = fact["payload"]
                    base = {
                        "id": fact["id"],
                        "account_id": account,
                        "contact_id": scope.contact["id"],
                        "conversation_id": conversation,
                        "version": fact["current_version_no"],
                        "timezone_name": scope.settings.timezone_name
                        or scope.policy_row["timezone_name"],
                        "evidence": tuple(evidence),
                    }
                    args: dict[str, Any] = {
                        "now": now,
                        "policy": scope.policy,
                        "secret": secret,
                        "contact_setting_version": scope.settings.version,
                    }
                    importance = float(fact["importance"])
                    if fact["memory_type"] == "event":
                        occurrences.extend(
                            materialize_life_event(
                                LifeEventFact(
                                    **base,
                                    importance=importance,
                                    start_at=timestamp(payload.get("start_at")),
                                    end_at=timestamp(payload.get("end_at")),
                                    local_date=date.fromisoformat(payload["local_date"])
                                    if payload.get("local_date")
                                    else None,
                                    followup_allowed=payload.get("followup_allowed") is True,
                                ),
                                **args,
                            )
                        )
                    elif fact["memory_type"] == "intention":
                        if payload.get("explicit_followup") is True:
                            occurrences.extend(
                                materialize_explicit_followup(
                                    ExplicitFollowupFact(
                                        **base,
                                        importance=importance,
                                        expected_at=timestamp(payload.get("expected_at")),
                                    ),
                                    **args,
                                )
                            )
                        else:
                            occurrences.extend(
                                materialize_intention(
                                    IntentionFact(
                                        **base,
                                        importance=importance,
                                        expected_at=timestamp(payload.get("expected_at")),
                                        owner=payload.get("owner", "unknown"),
                                    ),
                                    **args,
                                )
                            )
                    else:
                        occurrences.extend(
                            materialize_reconnect(
                                RelationshipFact(
                                    **base,
                                    relationship=scope.settings.relationship_level,
                                    last_meaningful_at=scope.last_activity,
                                ),
                                **args,
                            )
                        )
                except TypeError, ValueError:
                    continue
        filtered = filter_occurrences(
            tuple(occurrences),
            now=now,
            policy=scope.policy,
            settings=scope.settings,
            account_enabled=scope.policy.enabled,
            mode_permits=scope.resolution.permits_auto or scope.resolution.permits_copilot,
            meaningful_activity_at=scope.last_activity,
            conflicting_work=scope.conflicting,
            last_proactive_at=scope.last_proactive,
        )
        # An occurrence belongs to at most one production candidate, including
        # terminal none/failed decisions. Time-based candidate IDs alone are not
        # sufficient to prevent a repeated scan from deciding it twice.
        fresh = []
        for eligible in filtered.eligible:
            occurrence = replace(
                eligible,
                source_type={
                    ReasonCode.EVENT_UPCOMING: "life_event",
                    ReasonCode.EVENT_FOLLOWUP: "life_event",
                    ReasonCode.PROMISE_DUE: "intention",
                    ReasonCode.EXPLICIT_FOLLOWUP: "intention",
                    ReasonCode.RELATIONSHIP_RECONNECT: "relationship",
                }[eligible.reason],
            )
            seen = await self.session.scalar(
                select(s.proactive_occurrences.c.id).where(
                    s.proactive_occurrences.c.occurrence_key == occurrence.occurrence_key
                )
            )
            if seen is None:
                fresh.append(occurrence)
        candidates = aggregate_candidates(
            tuple(fresh),
            now=now,
            policy=scope.policy,
            secret=secret,
            mode_versions={conversation: scope.resolution.mode_version},
            content_revisions={conversation: scope.resolution.content_revision},
            activity_revisions={conversation: scope.activity_revision},
        )
        for candidate in candidates:
            await ProactiveRepository(self.session).enqueue_candidate(candidate, now=now)
        return len(candidates)

    async def scan(
        self, *, now: datetime, secret: bytes, after: UUID | None = None, limit: int = 25
    ) -> tuple[int, UUID | None]:
        ids = (
            (
                await self.session.execute(
                    select(s.conversations.c.id)
                    .join(s.contacts, s.contacts.c.id == s.conversations.c.contact_id)
                    .where(
                        s.contacts.c.proactive_enabled.is_(True),
                        s.conversations.c.deleted_at.is_(None),
                        s.conversations.c.id > after if after else true(),
                    )
                    .order_by(s.conversations.c.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        count = 0
        for conversation in ids:
            async with self.session.begin_nested():
                try:
                    scope = await self.scope(conversation, now=now)
                    interval = scope.policy.scheduler_scan_seconds
                    self.scan_interval_seconds = min(
                        self.scan_interval_seconds or interval, interval
                    )
                    count += await self.materialize(scope, secret=secret, now=now)
                    await self.session.execute(
                        insert(s.proactive_scan_cursors)
                        .values(
                            account_id=scope.account["id"],
                            cursor_kind="due",
                            last_scanned_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            constraint="pk_proactive_scan_cursors",
                            set_={
                                "last_scanned_at": now,
                                "updated_at": now,
                                "version": s.proactive_scan_cursors.c.version + 1,
                            },
                        )
                    )
                except ProactiveRuntimeError, MemoryPipelineError:
                    continue
        return count, ids[-1] if len(ids) == limit else None
