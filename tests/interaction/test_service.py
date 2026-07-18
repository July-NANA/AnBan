"""Interaction envelopes enter Runtime with safe authoritative correlation."""

from __future__ import annotations

import pytest

from anban.capability import CapabilityRegistry
from anban.core.ids import new_interaction_id
from anban.interaction import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
    InteractionService,
)
from anban.runtime import PersistentRuntime
from tests.runtime.test_persistent_runtime import (
    MemoryUnitOfWorkFactory,
    TransactionCheckingModel,
    final_turn,
    load_run,
)


async def test_interaction_envelope_maps_to_durable_runtime_metadata() -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = PersistentRuntime(
        TransactionCheckingModel(factory, [final_turn()]),
        CapabilityRegistry(),
        factory,
    )
    interaction_id = new_interaction_id()
    result = await InteractionService(runtime).submit(
        InteractionEnvelope(id=interaction_id, content="Execute through Interaction.")
    )

    aggregate = await load_run(factory, result.run_id)
    expected = {
        "interaction_id": str(interaction_id),
        "source": "cli",
        "input_kind": "user_message",
        "interaction_route": "new_task",
    }
    assert aggregate.task.metadata.root == expected
    assert aggregate.run.metadata.root == expected
    assert aggregate.nodes[0].metadata.root == expected


async def test_interaction_chat_maps_each_envelope_to_one_run_node() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(factory, [final_turn(), final_turn()]),
            CapabilityRegistry(),
            factory,
        )
    )
    chat = service.chat()
    first_id, second_id = new_interaction_id(), new_interaction_id()
    first = await chat.submit(InteractionEnvelope(id=first_id, content="First."))
    second = await chat.submit(InteractionEnvelope(id=second_id, content="Second."))
    closed = await chat.close()

    assert closed is not None
    assert first.run_id == second.run_id == closed.run_id
    aggregate = await load_run(factory, closed.run_id)
    assert len(aggregate.nodes) == 2
    assert aggregate.nodes[0].metadata.root["interaction_id"] == str(first_id)
    assert aggregate.nodes[1].metadata.root["interaction_id"] == str(second_id)


@pytest.mark.parametrize(
    "envelope",
    [
        InteractionEnvelope(
            id=new_interaction_id(),
            source="message.adapter",
            content="External new work.",
        ),
        InteractionEnvelope(
            id=new_interaction_id(),
            input_kind=InteractionInputKind.ASYNC_CAPABILITY_RESULT,
            content="An asynchronous result.",
        ),
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Resume prior work.",
            correlation=InteractionCorrelation(
                route=InteractionRoute.RESUME_ELIGIBLE_RUN,
                resume_key=CorrelationKey(
                    purpose=CorrelationPurpose.RESUME,
                    namespace="external.thread",
                    value="thread-9347",
                ),
            ),
        ),
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Deduplicated new work.",
            correlation=InteractionCorrelation(
                deduplication_key=CorrelationKey(
                    purpose=CorrelationPurpose.DEDUPLICATION,
                    namespace="external.delivery",
                    value="delivery-8219",
                )
            ),
        ),
    ],
)
async def test_existing_cli_service_rejects_unimplemented_v05_routing(
    envelope: InteractionEnvelope,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn()])
    service = InteractionService(PersistentRuntime(model, CapabilityRegistry(), factory))

    with pytest.raises(RuntimeError, match="routing is not configured"):
        await service.submit(envelope)

    assert model.calls == 0
