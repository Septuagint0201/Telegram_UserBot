"""Sealed two-stage proactive provider inputs and fenced publication."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, null, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.memory_inputs import (
    MemoryModelSnapshot,
    load_memory_model,
)
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.proactive_runtime import (
    ProactiveRuntimeError,
    ProactiveRuntimeRepository,
    ProactiveScope,
)
from telegram_userbot.adapters.persistence.records import NewDeliveryGroupRecord
from telegram_userbot.adapters.persistence.telegram_repository import TelegramLifecycleRepository
from telegram_userbot.domain.messaging import (
    OutboundChunk,
    payload_sha256,
    stable_telegram_random_id,
)
from telegram_userbot.domain.proactive.jobs import DueJob
from telegram_userbot.domain.proactive.models import (
    AgentDecision,
    BudgetLimits,
    Candidate,
    CandidateState,
    ProactiveAction,
)
from telegram_userbot.domain.proactive.pipeline import (
    AuthorizationInput,
    FinalGateInput,
    ProactiveTarget,
    final_gate,
    map_mode,
    preliminary_gate,
)
from telegram_userbot.domain.proactive.time import quiet_decision
from telegram_userbot.domain.shared.redaction import SensitiveValue

ADAPTER_VERSION = "proactive-runtime-v1"


def document_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def decision_value(row: Any, selected: tuple[UUID, ...]) -> AgentDecision:
    return AgentDecision(
        row["candidate_id"],
        ProactiveAction(row["action"]),
        row["decision_code"],
        selected,
        row["topic"],
        float(row["priority"]),
        row["defer_until"],
        1 if row["action"] == "defer_once" else 0,
    )


def authorization(
    candidate: Candidate, decision: AgentDecision, scope: ProactiveScope, *, now: datetime
) -> AuthorizationInput:
    minimum = scope.settings.minimum_interval or scope.policy.minimum_interval(
        scope.settings.relationship_level
    )
    return AuthorizationInput(
        replace(candidate, state=CandidateState.EVALUATING),
        decision,
        now,
        scope.policy,
        scope.resolution.effective_mode,
        operational_ready=scope.resolution.permits_auto or scope.resolution.permits_copilot,
        contact_enabled=scope.settings.enabled,
        meaningful_activity_at=scope.last_activity,
        conflicting_work=scope.conflicting,
        minimum_interval_ok=scope.last_proactive is None or now - scope.last_proactive >= minimum,
    )


@dataclass(frozen=True)
class PreparedProactive:
    job: DueJob
    candidate: Candidate = field(repr=False)
    model: MemoryModelSnapshot = field(repr=False)
    run_id: UUID
    purpose: str
    input_fingerprint: bytes
    user_input: SensitiveValue[str] = field(repr=False)
    account_control_version: int
    decision_id: UUID | None = None


class ProactiveGenerationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.runtime = ProactiveRuntimeRepository(session)

    async def fence(self, job: DueJob, *, now: datetime) -> Any:
        row = (
            (
                await self.session.execute(
                    select(s.proactive_jobs)
                    .where(s.proactive_jobs.c.id == job.id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_owner"] != job.lease_owner
            or row["fencing_token"] != job.fencing_token
            or row["lease_expires_at"] <= now
        ):
            raise ProactiveRuntimeError("PROACTIVE_LEASE_LOST", retryable=True)
        return row

    async def decision(self, candidate_id: UUID) -> tuple[Any, AgentDecision] | None:
        row = (
            (
                await self.session.execute(
                    select(s.proactive_decisions).where(
                        s.proactive_decisions.c.candidate_id == candidate_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        ids = tuple(
            (
                await self.session.execute(
                    select(s.proactive_decision_memberships.c.occurrence_id)
                    .where(s.proactive_decision_memberships.c.decision_id == row["id"])
                    .order_by(s.proactive_decision_memberships.c.ordinal)
                )
            ).scalars()
        )
        return row, decision_value(row, ids)

    async def prepare(  # noqa: PLR0912 - sealed provenance is checked explicitly
        self, job: DueJob, *, purpose: str, secret: bytes, now: datetime
    ) -> PreparedProactive:
        row = await self.fence(job, now=now)
        candidate = await self.runtime.candidate(row["candidate_id"], now=now)
        scope = await self.runtime.scope(candidate.conversation_id, now=now)
        if candidate.state not in {
            CandidateState.OPEN,
            CandidateState.EVALUATING,
            CandidateState.SEND_SELECTED,
            CandidateState.DEFERRED_ONCE,
        }:
            raise ProactiveRuntimeError("PROACTIVE_CANDIDATE_TERMINAL")
        accepted = await self.decision(candidate.id)
        decision = (
            accepted[1]
            if accepted
            else AgentDecision(
                candidate.id,
                ProactiveAction.SEND_NOW,
                "timely_support",
                tuple(item.id for item in candidate.occurrences),
                "evaluate",
                0.5,
            )
        )
        gate = final_gate(
            FinalGateInput(
                authorization(candidate, decision, scope, now=now),
                scope.resolution.account_control_version,
                scope.resolution.account_control_version,
                scope.resolution.effective_mode,
                candidate.mode_version,
                scope.resolution.mode_version,
                candidate.content_revision,
                scope.resolution.content_revision,
                candidate.activity_revision,
                scope.activity_revision,
                now,
                current_contact_setting_version=scope.settings.version,
                current_relationship_state_version=None,
            )
        )
        if not gate.allowed or candidate.timezone_name != scope.settings.timezone_name:
            raise ProactiveRuntimeError("PROACTIVE_GATE_" + gate.reason)
        if purpose == "proactive_final" and (
            accepted is None
            or decision.action is ProactiveAction.NONE
            or (decision.defer_until and decision.defer_until > now)
        ):
            raise ProactiveRuntimeError("PROACTIVE_DECISION_NOT_READY")
        role = "main_ai" if purpose == "proactive_final" else "proactive_agent"
        manifest_id = uuid5(job.id, purpose + ":manifest")
        sealed = (
            (
                await self.session.execute(
                    select(s.proactive_input_manifests).where(
                        s.proactive_input_manifests.c.id == manifest_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if sealed is not None and sealed["scope_erased_at"] is not None:
            raise ProactiveRuntimeError("PROACTIVE_SCOPE_ERASED")
        prompt = (
            (
                await self.session.execute(
                    select(s.prompt_versions)
                    .where(
                        s.prompt_versions.c.logical_role == role,
                        s.prompt_versions.c.id == sealed["prompt_version_id"] if sealed else true(),
                    )
                    .order_by(s.prompt_versions.c.version_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if prompt is None:
            raise ProactiveRuntimeError("PROACTIVE_PROMPT_UNAVAILABLE")
        model = await load_memory_model(
            self.session,
            logical_role=role,
            prompt_version=f"prompt-v{prompt['version_no']}",
            now=now,
            config_id=sealed["model_config_version_id"] if sealed else None,
            credential_version_id=sealed["credential_version_id"] if sealed else None,
        )
        policy = {
            key: scope.policy_row[key]
            for key in (
                "id",
                "version_no",
                "enabled",
                "timezone_name",
                "quiet_start_local",
                "quiet_end_local",
                "account_daily_limit",
                "contact_bypass_daily_limit",
                "activity_suppression_seconds",
                "settings_json",
            )
        }
        policy = json.loads(document_bytes(policy))
        selected = (
            set(decision.selected_occurrence_ids)
            if accepted and purpose == "proactive_final"
            else {item.id for item in candidate.occurrences}
        )
        decision_doc = (
            None
            if purpose == "proactive_decision"
            else {
                "id": str(accepted[0]["id"]) if accepted else None,
                "action": decision.action.value,
                "topic": decision.topic,
                "selected_occurrence_ids": [
                    str(value) for value in decision.selected_occurrence_ids
                ],
            }
        )
        body = document_bytes(
            {
                "schema": ADAPTER_VERSION,
                "purpose": purpose,
                "candidate_id": str(candidate.id),
                "now": (sealed["created_at"] if sealed else now).isoformat(),
                "timezone": candidate.timezone_name,
                "window_end": candidate.window_end_at.isoformat(),
                "policy": policy,
                "decision": decision_doc,
                "occurrences": [
                    {
                        "id": str(item.id),
                        "reason": item.reason.value,
                        "importance": item.importance,
                        "evidence": [
                            {
                                "source_id": str(source.source_id),
                                "source_revision": source.source_version,
                                "source_sha256": source.source_hash.hex(),
                                "summary": source.summary,
                            }
                            for source in item.evidence
                        ],
                    }
                    for item in candidate.occurrences
                    if item.id in selected
                ],
            }
        )
        if (
            len(body)
            + len(model.prompt.reveal_for_use().encode())
            + (model.config.max_output_tokens or 0)
            + 2048
            > model.capabilities.max_context_tokens
        ):
            raise ProactiveRuntimeError("PROACTIVE_INPUT_LIMIT_EXCEEDED")
        digest = hmac.digest(
            secret,
            body
            + model.prompt_sha256
            + model.capability_sha256
            + model.config_id.bytes
            + model.credential_version_id.bytes,
            "sha256",
        )
        run_id = uuid5(job.id, purpose + ":run")
        if sealed is not None:
            if sealed["manifest_sha256"] != digest:
                raise ProactiveRuntimeError("PROACTIVE_INPUT_CHANGED")
        else:
            await self.session.execute(
                insert(s.proactive_input_manifests).values(
                    id=manifest_id,
                    account_id=candidate.account_id,
                    conversation_id=candidate.conversation_id,
                    proactive_job_id=job.id,
                    candidate_id=candidate.id,
                    proactive_decision_id=accepted[0]["id"]
                    if purpose == "proactive_final" and accepted
                    else None,
                    logical_role=role,
                    purpose=purpose,
                    job_fencing_token=job.fencing_token,
                    candidate_generation=candidate.generation,
                    candidate_key=candidate.candidate_key,
                    candidate_membership_hash=candidate.membership_hash,
                    decision_snapshot=decision_doc if decision_doc is not None else null(),
                    decision_snapshot_sha256=hashlib.sha256(document_bytes(decision_doc)).digest()
                    if decision_doc
                    else None,
                    mode_version=candidate.mode_version,
                    content_revision=candidate.content_revision,
                    activity_revision=candidate.activity_revision,
                    policy_version_id=scope.policy.version_id,
                    policy_version_no=scope.policy.version_no,
                    policy_snapshot=policy,
                    policy_snapshot_sha256=hashlib.sha256(document_bytes(policy)).digest(),
                    timezone_snapshot=candidate.timezone_name,
                    context_contract_version=ADAPTER_VERSION,
                    prompt_version_id=prompt["id"],
                    prompt_version=f"prompt-v{prompt['version_no']}",
                    prompt_bundle_sha256=model.prompt_sha256,
                    model_config_version_id=model.config_id,
                    credential_version_id=model.credential_version_id,
                    capability_snapshot_sha256=model.capability_sha256,
                    input_schema_version=1,
                    output_schema_version=2 if role == "main_ai" else 1,
                    input_token_estimate=len(body),
                    occurrence_count=len(selected),
                    manifest_sha256=None,
                    sealed_at=None,
                    created_at=now,
                )
            )
            ordinal = 0
            for occurrence_ordinal, item in enumerate(candidate.occurrences, 1):
                if item.id not in selected:
                    continue
                for evidence_ordinal, source in enumerate(item.evidence, 1):
                    ordinal += 1
                    await self.session.execute(
                        insert(s.proactive_input_manifest_items).values(
                            manifest_id=manifest_id,
                            account_id=candidate.account_id,
                            ordinal=ordinal,
                            occurrence_id=item.id,
                            occurrence_ordinal=occurrence_ordinal,
                            occurrence_generation=item.generation,
                            occurrence_key=item.occurrence_key,
                            evidence_ordinal=evidence_ordinal,
                            source_type=source.source_type,
                            source_id=source.source_id,
                            source_version=source.source_version,
                            source_hash=source.source_hash,
                            summary=source.summary,
                        )
                    )
            await self.session.execute(
                update(s.proactive_input_manifests)
                .where(s.proactive_input_manifests.c.id == manifest_id)
                .values(manifest_sha256=digest, sealed_at=now)
            )
            await self.session.execute(
                insert(s.model_runs).values(
                    id=run_id,
                    account_id=candidate.account_id,
                    conversation_id=candidate.conversation_id,
                    proactive_job_id=job.id,
                    logical_role=role,
                    model_profile_id=model.config.profile_id,
                    purpose=purpose,
                    generation_no=candidate.generation,
                    state="running",
                    account_control_version_snapshot=scope.resolution.account_control_version,
                    mode_version_snapshot=candidate.mode_version,
                    content_revision_snapshot=candidate.content_revision,
                    config_version_id=model.config_id,
                    credential_version_id=model.credential_version_id,
                    proactive_input_manifest_id=manifest_id,
                    prompt_version=f"prompt-v{prompt['version_no']}",
                    prompt_bundle_sha256=model.prompt_sha256,
                    capability_snapshot_sha256=model.capability_sha256,
                    orchestration_claim_fingerprint=hashlib.sha256(job.id.bytes + digest).digest(),
                    input_fingerprint=digest,
                    adapter_version=ADAPTER_VERSION,
                    request_schema_version=1,
                    output_schema_version=2 if role == "main_ai" else 1,
                    normalizer_version="v1",
                    started_at=now,
                )
            )
        run = (
            (await self.session.execute(select(s.model_runs).where(s.model_runs.c.id == run_id)))
            .mappings()
            .one()
        )
        if (
            run["account_control_version_snapshot"] != scope.resolution.account_control_version
            or run["adapter_version"] != ADAPTER_VERSION
        ):
            raise ProactiveRuntimeError("PROACTIVE_CONTROL_CHANGED")
        return PreparedProactive(
            job,
            candidate,
            model,
            run_id,
            purpose,
            digest,
            SensitiveValue(body.decode()),
            run["account_control_version_snapshot"],
            accepted[0]["id"] if accepted else None,
        )

    async def verify(
        self, prepared: PreparedProactive, *, secret: bytes, now: datetime
    ) -> ProactiveScope:
        current = await self.prepare(prepared.job, purpose=prepared.purpose, secret=secret, now=now)
        if current.input_fingerprint != prepared.input_fingerprint:
            raise ProactiveRuntimeError("PROACTIVE_INPUT_CHANGED")
        return await self.runtime.scope(prepared.candidate.conversation_id, now=now)

    async def finish_run(  # noqa: PLR0913 - immutable result metadata
        self,
        prepared: PreparedProactive,
        *,
        raw: str,
        secret: bytes,
        now: datetime,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        await self.session.execute(
            update(s.model_runs)
            .where(s.model_runs.c.id == prepared.run_id)
            .values(
                state="succeeded",
                output_fingerprint=hmac.digest(secret, raw.encode(), "sha256"),
                finish_reason="complete",
                result_kind="text",
                is_complete=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                completed_at=now,
            )
        )
        await self.session.execute(
            update(s.model_run_attempts)
            .where(
                s.model_run_attempts.c.model_run_id == prepared.run_id,
                s.model_run_attempts.c.attempt_no == prepared.job.attempt_count,
            )
            .values(
                state="succeeded",
                completed_at=now,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                http_status=200,
            )
        )

    async def reserve(self, prepared: PreparedProactive, *, now: datetime) -> bool:
        scope = await self.runtime.scope(prepared.candidate.conversation_id, now=now)
        accepted = await self.decision(prepared.candidate.id)
        if accepted is None:
            raise ProactiveRuntimeError("PROACTIVE_DECISION_MISSING")
        candidate, decision = prepared.candidate, accepted[1]
        gate = preliminary_gate(authorization(candidate, decision, scope, now=now))
        if not gate.allowed:
            raise ProactiveRuntimeError("PROACTIVE_GATE_" + gate.reason)
        selected = [
            item for item in candidate.occurrences if item.id in decision.selected_occurrence_ids
        ]
        bypass = any(
            quiet_decision(
                now, timezone_name=candidate.timezone_name, policy=scope.policy, occurrence=item
            ).in_quiet_hours
            for item in selected
        )
        target = map_mode(scope.resolution.effective_mode)
        existing = (
            (
                await self.session.execute(
                    select(s.proactive_budget_reservations)
                    .where(s.proactive_budget_reservations.c.decision_id == accepted[0]["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return bool(
                existing["state"] == "held"
                and existing["expires_at"] > now
                and existing["target"] == target.value
                and existing["contact_local_date"]
                == now.astimezone(ZoneInfo(candidate.timezone_name)).date()
                and existing["account_local_date"]
                == now.astimezone(ZoneInfo(scope.policy_row["timezone_name"])).date()
            )
        reservation = await ProactiveRepository(self.session).reserve_budget(
            account_id=candidate.account_id,
            contact_id=candidate.contact_id,
            account_local_date=now.astimezone(ZoneInfo(scope.policy_row["timezone_name"])).date(),
            contact_local_date=now.astimezone(ZoneInfo(candidate.timezone_name)).date(),
            account_timezone_name=scope.policy_row["timezone_name"],
            contact_timezone_name=candidate.timezone_name,
            limits=BudgetLimits(
                scope.policy.account_daily_limit,
                scope.settings.daily_limit
                if scope.settings.daily_limit is not None
                else scope.policy.daily_limit(scope.settings.relationship_level),
                scope.policy.contact_bypass_daily_limit,
            ),
            now=now,
            expires_at=min(candidate.window_end_at, now + timedelta(minutes=10)),
            reservation_key=hashlib.sha256(
                b"proactive-reservation:" + accepted[0]["id"].bytes
            ).digest(),
            candidate_id=candidate.id,
            decision_id=accepted[0]["id"],
            policy_version_id=scope.policy.version_id,
            authorization_generation=1,
            target=target,
            bypass=bypass,
        )
        return (
            reservation is not None
            and reservation.state.value == "held"
            and reservation.expires_at > now
        )

    async def publish(
        self, prepared: PreparedProactive, *, content: str, secret: bytes, now: datetime
    ) -> UUID:
        scope = await self.verify(prepared, secret=secret, now=now)
        accepted = await self.decision(prepared.candidate.id)
        if accepted is None:
            raise ProactiveRuntimeError("PROACTIVE_DECISION_MISSING")
        candidate, decision = prepared.candidate, accepted[1]
        key = hashlib.sha256(b"proactive-reservation:" + accepted[0]["id"].bytes).digest()
        reservation = (
            (
                await self.session.execute(
                    select(s.proactive_budget_reservations)
                    .where(s.proactive_budget_reservations.c.reservation_key == key)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            reservation is None
            or reservation["state"] != "held"
            or reservation["expires_at"] <= now
            or reservation["outbound_group_id"]
            or reservation["copilot_draft_id"]
        ):
            raise ProactiveRuntimeError("PROACTIVE_RESERVATION_UNAVAILABLE")
        gate = final_gate(
            FinalGateInput(
                authorization(candidate, decision, scope, now=now),
                prepared.account_control_version,
                scope.resolution.account_control_version,
                scope.resolution.effective_mode,
                candidate.mode_version,
                scope.resolution.mode_version,
                candidate.content_revision,
                scope.resolution.content_revision,
                candidate.activity_revision,
                scope.activity_revision,
                now,
                current_contact_setting_version=scope.settings.version,
            )
        )
        if not gate.allowed:
            raise ProactiveRuntimeError("PROACTIVE_GATE_" + gate.reason)
        turn_id, target_id = uuid5(accepted[0]["id"], "turn"), uuid5(accepted[0]["id"], "target")
        sequence = 1 + (
            await self.session.scalar(
                select(func.max(s.conversation_turns.c.collection_sequence)).where(
                    s.conversation_turns.c.conversation_id == candidate.conversation_id
                )
            )
            or 0
        )
        await self.session.execute(
            insert(s.conversation_turns).values(
                id=turn_id,
                account_id=candidate.account_id,
                conversation_id=candidate.conversation_id,
                state="output_ready",
                trigger_kind="proactive",
                collection_sequence=sequence,
                active_generation_no=candidate.generation,
                base_mode_snapshot=scope.resolution.base_mode.value,
                base_mode_source_snapshot=scope.resolution.base_source,
                effective_mode_snapshot=scope.resolution.effective_mode.value,
                account_control_version_snapshot=prepared.account_control_version,
                mode_version_snapshot=candidate.mode_version,
                content_revision_snapshot=candidate.content_revision,
                sealed_at=now,
            )
        )
        digest = payload_sha256(content)
        await self.session.execute(
            update(s.model_runs)
            .where(s.model_runs.c.id == prepared.run_id)
            .values(delivery_turn_id=turn_id)
        )
        target = map_mode(scope.resolution.effective_mode)
        if target is ProactiveTarget.AUTO_SEND:
            intent = uuid5(target_id, "intent")
            await TelegramLifecycleRepository(self.session).create_delivery_group(
                group=NewDeliveryGroupRecord(
                    id=target_id,
                    account_id=candidate.account_id,
                    conversation_id=candidate.conversation_id,
                    model_run_id=prepared.run_id,
                    source="proactive_ai",
                    idempotency_key=hashlib.sha256(target_id.bytes).digest(),
                    created_at=now,
                    mode_version=candidate.mode_version,
                    content_revision=candidate.content_revision,
                    turn_id=turn_id,
                    generation_no=candidate.generation,
                    account_control_version=prepared.account_control_version,
                    proactive_decision_id=accepted[0]["id"],
                    logical_content_sha256=digest,
                    max_delivery_chunks=1,
                    send_authorized_at=now,
                ),
                chunks=(
                    OutboundChunk(
                        intent, 0, stable_telegram_random_id(intent, secret), content, digest
                    ),
                ),
            )
        else:
            await self.session.execute(
                insert(s.copilot_drafts).values(
                    id=target_id,
                    account_id=candidate.account_id,
                    contact_id=candidate.contact_id,
                    conversation_id=candidate.conversation_id,
                    turn_id=turn_id,
                    model_run_id=prepared.run_id,
                    model_role="main_ai",
                    proactive_decision_id=accepted[0]["id"],
                    draft_kind="proactive",
                    state="ready",
                    current_revision_no=1,
                    account_control_version_snapshot=prepared.account_control_version,
                    mode_version_snapshot=candidate.mode_version,
                    content_revision_snapshot=candidate.content_revision,
                    requested_by="proactive_worker",
                    requested_at=now,
                    ready_at=now,
                    expires_at=reservation["expires_at"],
                )
            )
            await self.session.execute(
                insert(s.copilot_draft_revisions).values(
                    id=uuid5(target_id, "revision-1"),
                    account_id=candidate.account_id,
                    conversation_id=candidate.conversation_id,
                    draft_id=target_id,
                    revision_no=1,
                    author_type="model",
                    content_text=content,
                    content_sha256=digest,
                    created_at=now,
                )
            )
        bound = await ProactiveRepository(self.session).bind_budget_target(
            account_id=candidate.account_id,
            reservation_key=key,
            target=target,
            target_id=target_id,
            now=now,
        )
        if bound is None:
            raise ProactiveRuntimeError("PROACTIVE_RESERVATION_UNAVAILABLE")
        return target_id
