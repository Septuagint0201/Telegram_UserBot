from __future__ import annotations

import pytest

from telegram_userbot.processes.durable_queue_inventory import (
    DURABLE_QUEUE_INVENTORY,
    QueueComposition,
    QueueWorkClass,
    missing_required_surfaces,
    owner_required_surfaces_composed,
    worker_required_queues_composed,
)


@pytest.mark.unit
def test_durable_queue_inventory_has_one_content_free_owner_contract_per_table() -> None:
    by_name = {registration.name: registration for registration in DURABLE_QUEUE_INVENTORY}

    assert len(by_name) == len(DURABLE_QUEUE_INVENTORY)
    assert set(by_name) == {
        "background_jobs",
        "context_preview_requests",
        "context_preview_deliveries",
        "control_commands",
        "control_bot_update_receipts",
        "conversation_turns",
        "data_export_requests",
        "data_erasure_requests",
        "deployment_restore_state",
        "erasure_ledger",
        "erasure_progress",
        "media_objects",
        "embedding_records:pending",
        "memory_jobs",
        "memory_review_actions",
        "outbound_delivery_groups",
        "outbound_intents:unknown_send_reconciliation",
        "proactive_budget_reservations:held",
        "proactive_jobs",
        "proactive_scan_cursors",
        "transactional_outbox:control.command.completed",
        "transactional_outbox:control.command.requested",
        "transactional_outbox:durable_job.available",
        "transactional_outbox:model.config.activated",
        "transactional_outbox:model.credential.changed",
        "transactional_outbox:orchestrator.invalidated",
    }
    assert all(
        value
        for registration in DURABLE_QUEUE_INVENTORY
        for value in (
            registration.owner,
            registration.claimer,
            registration.executor,
            registration.scheduler,
        )
    )


@pytest.mark.unit
def test_inventory_composes_all_required_worker_pipelines() -> None:
    by_name = {registration.name: registration for registration in DURABLE_QUEUE_INVENTORY}

    assert by_name["background_jobs"].composition is QueueComposition.COMPOSED
    assert by_name["context_preview_deliveries"].composition is QueueComposition.COMPOSED
    assert by_name["context_preview_requests"].composition is QueueComposition.COMPOSED_SYNCHRONOUS
    assert by_name["media_objects"].composition is QueueComposition.COMPOSED
    assert by_name["data_export_requests"].composition is QueueComposition.COMPOSED_ONE_SHOT
    assert by_name["control_bot_update_receipts"].composition is QueueComposition.COMPOSED
    assert by_name["memory_jobs"].composition is QueueComposition.COMPOSED
    assert by_name["embedding_records:pending"].composition is QueueComposition.COMPOSED
    assert by_name["proactive_jobs"].composition is QueueComposition.COMPOSED
    assert missing_required_surfaces(owner="worker") == ()
    assert missing_required_surfaces(owner="app") == ()
    assert missing_required_surfaces(owner="control") == ()
    assert owner_required_surfaces_composed("app")
    assert owner_required_surfaces_composed("control")
    assert worker_required_queues_composed()


@pytest.mark.unit
def test_outbox_topics_have_one_worker_relay_and_typed_consumers() -> None:
    by_name = {registration.name: registration for registration in DURABLE_QUEUE_INVENTORY}

    for topic in (
        "transactional_outbox:model.credential.changed",
        "transactional_outbox:model.config.activated",
        "transactional_outbox:control.command.requested",
        "transactional_outbox:control.command.completed",
    ):
        registration = by_name[topic]
        assert registration.composition is QueueComposition.COMPOSED
        assert registration.owner == "worker"
        assert registration.relay == "worker_scheduler_leader"
        assert registration.producer
        assert registration.consumers
    assert (
        by_name["transactional_outbox:orchestrator.invalidated"].composition
        is QueueComposition.UNREACHABLE_PRODUCER
    )
    for surface in (
        "proactive_scan_cursors",
        "proactive_budget_reservations:held",
    ):
        assert by_name[surface].composition is QueueComposition.COMPOSED
    assert by_name["data_erasure_requests"].composition is QueueComposition.COMPOSED
    assert by_name["erasure_progress"].composition is QueueComposition.COMPOSED
    assert by_name["erasure_ledger"].composition is QueueComposition.COMPOSED_ONE_SHOT
    assert by_name["deployment_restore_state"].composition is QueueComposition.COMPOSED_ONE_SHOT


@pytest.mark.unit
def test_inventory_uses_explicit_disk_work_classes() -> None:
    by_name = {registration.name: registration for registration in DURABLE_QUEUE_INVENTORY}

    assert by_name["background_jobs"].work_class is QueueWorkClass.EXPLICIT_QUEUE_NAME
    assert by_name["proactive_jobs"].work_class is QueueWorkClass.PROACTIVE
    assert by_name["proactive_scan_cursors"].work_class is QueueWorkClass.PROACTIVE
    assert by_name["proactive_budget_reservations:held"].work_class is QueueWorkClass.SAFETY_CLEANUP
    assert by_name["context_preview_deliveries"].work_class is QueueWorkClass.SAFETY_CLEANUP
    assert by_name["media_objects"].work_class is QueueWorkClass.SAFETY_CLEANUP
