"""Durable Interaction inbox, deduplication, expiry, and restart safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from anban.capability import CapabilityRegistry
from anban.core import AnbanError
from anban.core.ids import new_interaction_id
from anban.core.models import now_utc
from anban.interaction import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionService,
)
from anban.interaction.inbox import InteractionInboxCoordinator
from anban.runtime import ExecutionQueryService, PersistentRuntime
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import TransactionCheckingModel, final_turn


def deduplicated_envelope(
    value: str,
    content: str,
    *,
    received_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source="message.adapter",
        content=content,
        received_at=received_at or now_utc(),
        correlation=InteractionCorrelation(
            deduplication_key=CorrelationKey(
                purpose=CorrelationPurpose.DEDUPLICATION,
                namespace="external.delivery",
                value=value,
                expires_at=expires_at,
            )
        ),
    )


async def test_duplicate_delivery_reuses_one_durable_run_without_model_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn("One durable answer.")])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    value = "delivery-4951"
    first = await service.submit(deduplicated_envelope(value, "Handle one bounded message."))

    restarted = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    duplicate = await restarted.submit(deduplicated_envelope(value, "Handle one bounded message."))

    assert duplicate.run_id == first.run_id
    assert duplicate.task_id == first.task_id
    assert duplicate.outcome.final_text == first.outcome.final_text
    assert model.calls == 1
    inbox = await restarted.inbox()
    assert len(inbox) == 1
    assert inbox[0].status.value == "processed"
    assert inbox[0].delivery_count == 2
    assert inbox[0].last_disposition.value == "deduplicated"
    trace = await ExecutionQueryService(factory).trace(first.run_id)
    assert sum(event.event_type == "interaction.inbox_routed" for event in trace.audit) == 1


async def test_same_deduplication_identity_rejects_changed_semantics() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn()])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    value = "delivery-7392"
    await service.submit(deduplicated_envelope(value, "Original delivery content."))

    with pytest.raises(AnbanError) as captured:
        await service.submit(deduplicated_envelope(value, "Changed delivery content."))

    assert captured.value.info.details.root["reason"] == "conflicting"
    assert model.calls == 1
    inbox = await service.inbox()
    assert inbox[0].delivery_count == 2
    assert inbox[0].last_disposition.value == "conflicting"


async def test_event_expired_after_receipt_is_durable_and_never_admitted() -> None:
    factory = MemoryUnitOfWorkFactory()
    received_at = datetime(2026, 7, 19, 1, 2, tzinfo=UTC)
    expires_at = received_at + timedelta(minutes=1)
    coordinator = InteractionInboxCoordinator(
        factory,
        clock=lambda: expires_at + timedelta(seconds=1),
    )

    with pytest.raises(AnbanError) as captured:
        await coordinator.admit(
            deduplicated_envelope(
                "delivery-9026",
                "This delivery expires before routing.",
                received_at=received_at,
                expires_at=expires_at,
            )
        )

    assert captured.value.info.details.root["reason"] == "expired"
    entries = await coordinator.list()
    assert entries[0].status.value == "expired"
    assert entries[0].failure_reason == "expired"
    assert entries[0].run_id is None


async def test_stale_unrouted_claim_is_restart_safe_to_reclaim() -> None:
    factory = MemoryUnitOfWorkFactory()
    first_time = datetime(2026, 7, 19, 2, 3, tzinfo=UTC)
    value = "delivery-6148"
    envelope = deduplicated_envelope(
        value,
        "Persist before a simulated gateway stop.",
        received_at=first_time,
    )
    initial = InteractionInboxCoordinator(factory, clock=lambda: first_time)
    assert await initial.admit(envelope) is None

    restart_time = first_time + timedelta(minutes=6)
    restarted = InteractionInboxCoordinator(factory, clock=lambda: restart_time)
    retry = deduplicated_envelope(
        value,
        "Persist before a simulated gateway stop.",
        received_at=first_time,
    )
    assert await restarted.admit(retry) is None

    entry = factory.store.inbox[envelope.id]
    assert entry.claimed_at == restart_time
    assert entry.delivery_count == 2
    assert entry.run_id is None
