"""Content-free production ownership inventory for durable work tables."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QueueComposition(StrEnum):
    """Whether a durable queue has a callable production consumer."""

    COMPOSED = "composed"
    COMPOSED_ONE_SHOT = "composed_one_shot"
    COMPOSED_SYNCHRONOUS = "composed_synchronous"
    REQUIRED_MISSING = "required_missing"
    UNREACHABLE_PRODUCER = "unreachable_producer"


class QueueWorkClass(StrEnum):
    """Explicit disk-admission category; never inferred from numeric priority."""

    STANDARD = "standard"
    EXPLICIT_QUEUE_NAME = "explicit_queue_name"
    PROACTIVE = "proactive"
    SAFETY_CLEANUP = "safety_cleanup"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class DurableQueueRegistration:
    name: str
    owner: str
    claimer: str
    executor: str
    scheduler: str
    work_class: QueueWorkClass
    composition: QueueComposition
    producer: str = ""
    relay: str = ""
    consumers: tuple[str, ...] = ()
    required_for_ready: bool = True


# This is deliberately executable production metadata rather than a prose-only
# checklist. A required missing consumer keeps Worker NOT_READY until its actual
# claimer/executor is composed and this registration is changed in the same
# reviewed patch.
DURABLE_QUEUE_INVENTORY = (
    DurableQueueRegistration(
        "conversation_turns",
        "app",
        "conversation_runtime",
        "main_ai",
        "app_scheduler_500ms",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "outbound_delivery_groups",
        "app",
        "conversation_runtime",
        "telethon_gateway",
        "app_scheduler_500ms",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "outbound_intents:unknown_send_reconciliation",
        "app",
        "telegram_lifecycle_repository",
        "unknown_send_reconciliation",
        "app_recovery_schedule",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "control_commands",
        "app",
        "orchestrator_repository",
        "conversation_control_command_processor",
        "app_scheduler_500ms",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "control_bot_update_receipts",
        "control",
        "runtime_cursor_repository",
        "durable_control_update_executor",
        "control_bot_poller",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "media_objects",
        "app",
        "media_repository",
        "private_media_store",
        "app_scheduler_hourly",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "context_preview_requests",
        "control",
        "context_repository",
        "context_control_backend",
        "control_command_session",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED_SYNCHRONOUS,
    ),
    DurableQueueRegistration(
        "context_preview_deliveries",
        "control",
        "context_repository",
        "telegram_bot_delete",
        "control_maintenance_60s",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "transactional_outbox:durable_job.available",
        "worker",
        "worker_outbox_repository",
        "redis_arq_notifier",
        "worker_continuous",
        QueueWorkClass.EXPLICIT_QUEUE_NAME,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "transactional_outbox:model.credential.changed",
        "worker",
        "outbox_repository",
        "typed_redis_generation_marker",
        "scheduler_leader_continuous_1s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
        producer="control_model_configuration",
        relay="worker_scheduler_leader",
        consumers=("app_model_cache",),
    ),
    DurableQueueRegistration(
        "transactional_outbox:model.config.activated",
        "worker",
        "outbox_repository",
        "typed_redis_generation_marker",
        "scheduler_leader_continuous_1s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
        producer="control_model_configuration",
        relay="worker_scheduler_leader",
        consumers=("app_model_cache",),
    ),
    DurableQueueRegistration(
        "transactional_outbox:control.command.requested",
        "worker",
        "outbox_repository",
        "typed_redis_generation_marker",
        "scheduler_leader_continuous_1s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
        producer="control_command_backend",
        relay="worker_scheduler_leader",
        consumers=("app_scheduler",),
    ),
    DurableQueueRegistration(
        "transactional_outbox:control.command.completed",
        "worker",
        "outbox_repository",
        "typed_redis_generation_marker",
        "scheduler_leader_continuous_1s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
        producer="app_command_processor",
        relay="worker_scheduler_leader",
        consumers=("control_completion_event",),
    ),
    DurableQueueRegistration(
        "transactional_outbox:orchestrator.invalidated",
        "app",
        "outbox_repository",
        "orchestrator_invalidation_notification",
        "app_continuous",
        QueueWorkClass.STANDARD,
        QueueComposition.UNREACHABLE_PRODUCER,
        required_for_ready=False,
    ),
    DurableQueueRegistration(
        "background_jobs",
        "worker",
        "postgres_lease_via_arq_wakeup",
        "worker_executor_registry",
        "outbox_continuous_and_compensation_60s",
        QueueWorkClass.EXPLICIT_QUEUE_NAME,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "memory_jobs",
        "worker",
        "background_jobs:memory.generate",
        "memory_pipeline",
        "outbox_continuous_and_compensation_60s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "embedding_records:pending",
        "worker",
        "background_jobs:embedding.compute",
        "embedding_runtime",
        "outbox_continuous_and_compensation_60s",
        QueueWorkClass.STANDARD,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "memory_review_actions",
        "worker",
        "background_jobs:memory.review_action",
        "memory_review_runtime",
        "worker_arq_wakeup",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "proactive_jobs",
        "worker",
        "proactive_repository",
        "proactive_agent_pipeline",
        "worker_policy_schedule",
        QueueWorkClass.PROACTIVE,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "proactive_scan_cursors",
        "worker",
        "proactive_repository",
        "occurrence_scan_scheduler",
        "worker_policy_schedule",
        QueueWorkClass.PROACTIVE,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "proactive_budget_reservations:held",
        "worker",
        "proactive_repository",
        "reservation_reaper",
        "worker_policy_schedule_60s",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "data_erasure_requests",
        "worker",
        "memory_repository",
        "memory.reconcile_erasure",
        "worker_continuous",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "erasure_progress",
        "worker",
        "memory_repository",
        "memory.reconcile_erasure",
        "worker_continuous",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED,
    ),
    DurableQueueRegistration(
        "erasure_ledger",
        "maintenance",
        "restore_gate_repository",
        "restore_gate._replay_ledger",
        "restore_validation",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED_ONE_SHOT,
    ),
    DurableQueueRegistration(
        "deployment_restore_state",
        "maintenance",
        "restore_gate_repository",
        "restore_gate.close_and_verify",
        "operator_restore",
        QueueWorkClass.SAFETY_CLEANUP,
        QueueComposition.COMPOSED_ONE_SHOT,
    ),
    DurableQueueRegistration(
        "data_export_requests",
        "data-export",
        "ops_data_export",
        "age_streaming_export",
        "operator_one_shot",
        QueueWorkClass.OPERATOR,
        QueueComposition.COMPOSED_ONE_SHOT,
        required_for_ready=False,
    ),
)


def missing_required_surfaces(*, owner: str | None = None) -> tuple[str, ...]:
    """Return only stable table identifiers, never payload or secret data."""

    return tuple(
        registration.name
        for registration in DURABLE_QUEUE_INVENTORY
        if registration.required_for_ready
        and registration.composition is QueueComposition.REQUIRED_MISSING
        and (owner is None or registration.owner == owner)
    )


def worker_required_queues_composed() -> bool:
    return owner_required_surfaces_composed("worker")


def owner_required_surfaces_composed(owner: str) -> bool:
    if not owner or owner != owner.strip():
        raise ValueError("durable work owner is invalid")
    return not missing_required_surfaces(owner=owner)


__all__ = [
    "DURABLE_QUEUE_INVENTORY",
    "DurableQueueRegistration",
    "QueueComposition",
    "QueueWorkClass",
    "missing_required_surfaces",
    "owner_required_surfaces_composed",
    "worker_required_queues_composed",
]
