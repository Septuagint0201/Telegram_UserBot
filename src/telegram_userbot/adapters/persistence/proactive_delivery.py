"""Recheck proactive authorization at the App's final Telegram boundary."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_userbot.adapters.persistence import schema as s
from telegram_userbot.adapters.persistence.proactive_generation import (
    ProactiveGenerationRepository,
    authorization,
)
from telegram_userbot.adapters.persistence.proactive_repository import ProactiveRepository
from telegram_userbot.adapters.persistence.proactive_runtime import (
    ProactiveRuntimeError,
    ProactiveRuntimeRepository,
)
from telegram_userbot.domain.proactive.pipeline import FinalGateInput, final_gate


async def authorize_proactive_delivery(
    session: AsyncSession, *, decision_id: UUID, turn_id: UUID, control_version: int, now: datetime
) -> bool:
    try:
        row = (
            (
                await session.execute(
                    select(s.proactive_decisions).where(s.proactive_decisions.c.id == decision_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["state"] != "accepted":
            return False
        runtime = ProactiveRuntimeRepository(session)
        candidate = await runtime.candidate(row["candidate_id"], now=now)
        scope = await runtime.scope(
            candidate.conversation_id, now=now, own_turn=turn_id, own_decision=decision_id
        )
        accepted = await ProactiveGenerationRepository(session).decision(candidate.id)
        reservation = (
            (
                await session.execute(
                    select(s.proactive_budget_reservations)
                    .where(s.proactive_budget_reservations.c.decision_id == decision_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            accepted is None
            or reservation is None
            or reservation["state"] not in {"held", "committed", "send_unknown"}
            or reservation["expires_at"] <= now
            or candidate.timezone_name != scope.settings.timezone_name
        ):
            return False
        decision = accepted[1]
        if decision.defer_until is not None and now < decision.defer_until:
            return False
        return final_gate(
            FinalGateInput(
                authorization(candidate, decision, scope, now=now),
                control_version,
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
        ).allowed
    except ProactiveRuntimeError, ValueError:
        return False


async def commit_proactive_delivery(
    session: AsyncSession, *, decision_id: UUID, now: datetime
) -> None:
    row = (
        (
            await session.execute(
                select(s.proactive_budget_reservations).where(
                    s.proactive_budget_reservations.c.decision_id == decision_id
                )
            )
        )
        .mappings()
        .one()
    )
    await ProactiveRepository(session).commit_budget(
        account_id=row["account_id"], reservation_key=row["reservation_key"], now=now, unknown=True
    )


async def expire_proactive_targets(session: AsyncSession, *, now: datetime) -> int:
    rows = (
        (
            await session.execute(
                select(s.proactive_budget_reservations)
                .where(
                    s.proactive_budget_reservations.c.state == "held",
                    s.proactive_budget_reservations.c.expires_at <= now,
                )
                .order_by(s.proactive_budget_reservations.c.expires_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .all()
    )
    count = 0
    for row in rows:
        turn = None
        group_id = row["outbound_group_id"]
        if group_id is None and row["copilot_draft_id"] is not None:
            group_id = await session.scalar(
                select(s.outbound_delivery_groups.c.id)
                .where(s.outbound_delivery_groups.c.copilot_draft_id == row["copilot_draft_id"])
                .limit(1)
            )
        if group_id is not None:
            turn = await session.scalar(
                update(s.outbound_delivery_groups)
                .where(
                    s.outbound_delivery_groups.c.id == group_id,
                    s.outbound_delivery_groups.c.state == "planned",
                    s.outbound_delivery_groups.c.first_side_effect_at.is_(None),
                )
                .values(state="cancelled", completed_at=now, updated_at=now)
                .returning(s.outbound_delivery_groups.c.turn_id)
            )
            if turn is not None:
                await session.execute(
                    update(s.outbound_intents)
                    .where(
                        s.outbound_intents.c.delivery_group_id == group_id,
                        s.outbound_intents.c.state.in_(("pending", "retry_wait")),
                    )
                    .values(
                        state="cancelled",
                        last_error_code="PROACTIVE_RESERVATION_EXPIRED",
                        updated_at=now,
                    )
                )
        if row["copilot_draft_id"] is not None and (group_id is None or turn is not None):
            turn = await session.scalar(
                update(s.copilot_drafts)
                .where(
                    s.copilot_drafts.c.id == row["copilot_draft_id"],
                    s.copilot_drafts.c.state.in_(("ready", "editing", "send_queued")),
                )
                .values(
                    state="expired",
                    terminal_at=now,
                    terminal_reason="PROACTIVE_RESERVATION_EXPIRED",
                )
                .returning(s.copilot_drafts.c.turn_id)
            )
        if turn is not None:
            await session.execute(
                update(s.conversation_turns)
                .where(
                    s.conversation_turns.c.id == turn,
                    s.conversation_turns.c.state == "output_ready",
                )
                .values(
                    state="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    terminal_reason="PROACTIVE_RESERVATION_EXPIRED",
                    completed_at=now,
                )
            )
            count += 1
    return count
