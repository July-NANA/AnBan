"""Interaction envelopes enter Runtime with safe authoritative correlation."""

from __future__ import annotations

from anban.capability import CapabilityRegistry
from anban.core.ids import new_interaction_id
from anban.interaction import InteractionEnvelope, InteractionService
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
    expected = {"interaction_id": str(interaction_id), "source": "cli"}
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
