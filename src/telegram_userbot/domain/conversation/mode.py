"""Deterministic conversation-mode resolution and version snapshots."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BaseMode(StrEnum):
    AUTO = "AUTO"
    HUMAN = "HUMAN"
    COPILOT = "COPILOT"


class EffectiveMode(StrEnum):
    AUTO = "AUTO"
    HUMAN = "HUMAN"
    COPILOT = "COPILOT"
    PAUSED = "PAUSED"


class MaintenanceState(StrEnum):
    INACTIVE = "inactive"
    DRAINING = "draining"
    ACTIVE = "active"


class OperationalState(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class AccountControl:
    default_base_mode: BaseMode
    global_paused: bool
    maintenance_state: MaintenanceState
    control_version: int
    resume_floor_event_id: int | None = None

    def __post_init__(self) -> None:
        if self.control_version < 1:
            raise ValueError("control version must be positive")


@dataclass(frozen=True, slots=True)
class ConversationControl:
    base_mode_override: BaseMode | None
    contact_paused: bool
    temporary_human_until: datetime | None
    mode_version: int
    content_revision: int
    automation_resume_floor_event_id: int | None = None
    last_response_covered_event_id: int | None = None

    def __post_init__(self) -> None:
        if self.mode_version < 1 or self.content_revision < 0:
            raise ValueError("conversation versions are invalid")


@dataclass(frozen=True, slots=True)
class ModeResolution:
    base_mode: BaseMode
    base_source: str
    effective_mode: EffectiveMode
    pause_reason: str | None
    operational_state: OperationalState
    block_reason: str | None
    account_control_version: int
    mode_version: int
    content_revision: int
    automation_resume_floor_event_id: int | None
    last_response_covered_event_id: int | None

    @property
    def permits_auto(self) -> bool:
        return (
            self.effective_mode is EffectiveMode.AUTO
            and self.operational_state is OperationalState.READY
        )

    @property
    def permits_copilot(self) -> bool:
        return (
            self.effective_mode is EffectiveMode.COPILOT
            and self.operational_state is OperationalState.READY
        )


def resolve_mode(  # noqa: PLR0913 - resolver inputs are an explicit safety contract
    *,
    account: AccountControl,
    conversation: ConversationControl,
    now: datetime,
    account_active: bool = True,
    contact_automation_status: str = "allowed",
    dependency_block_reason: str | None = None,
) -> ModeResolution:
    """Resolve one consistent mode snapshot using the documented priority order."""

    base_mode = conversation.base_mode_override or account.default_base_mode
    base_source = (
        "conversation_override"
        if conversation.base_mode_override is not None
        else "account_default"
    )
    effective = EffectiveMode(base_mode)
    pause_reason = None
    policy_block = None
    if not account_active:
        policy_block = "ACCOUNT_INACTIVE"
    elif contact_automation_status != "allowed":
        policy_block = f"CONTACT_{contact_automation_status.upper()}"
    elif account.maintenance_state is not MaintenanceState.INACTIVE:
        effective = EffectiveMode.PAUSED
        pause_reason = f"maintenance_{account.maintenance_state}"
    elif account.global_paused:
        effective = EffectiveMode.PAUSED
        pause_reason = "global_pause"
    elif conversation.contact_paused:
        effective = EffectiveMode.PAUSED
        pause_reason = "contact_pause"
    elif (
        conversation.temporary_human_until is not None and conversation.temporary_human_until > now
    ):
        effective = EffectiveMode.HUMAN

    block_reason = policy_block or dependency_block_reason
    operational = OperationalState.BLOCKED if block_reason else OperationalState.READY
    floors = tuple(
        value
        for value in (
            account.resume_floor_event_id,
            conversation.automation_resume_floor_event_id,
        )
        if value is not None
    )
    return ModeResolution(
        base_mode,
        base_source,
        effective,
        pause_reason,
        operational,
        block_reason,
        account.control_version,
        conversation.mode_version,
        conversation.content_revision,
        max(floors) if floors else None,
        conversation.last_response_covered_event_id,
    )
