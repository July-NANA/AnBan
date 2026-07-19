"""Interaction envelopes enter Runtime with safe authoritative correlation."""

from __future__ import annotations

import pytest

from anban.capability import CapabilityRegistry
from anban.core import AnbanError
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
from anban.runtime import ExecutionQueryService, PersistentRuntime
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
    result = await InteractionService(runtime, unit_of_work=factory).submit(
        InteractionEnvelope(id=interaction_id, content="Execute through Interaction.")
    )

    aggregate = await load_run(factory, result.run_id)
    expected = {
        "interaction_id": str(interaction_id),
        "source": "cli",
        "input_kind": "user_message",
        "interaction_route": "new_task",
        "inbox_managed": True,
    }
    assert aggregate.task.metadata.root == expected
    assert aggregate.run.metadata.root == expected
    assert aggregate.nodes[0].metadata.root == expected
    observation = await ExecutionQueryService(factory).trace(result.run_id)
    routed = next(event for event in observation.audit if event.event_type == "interaction.routed")
    assert routed.node_run_id == result.node_run_id
    assert routed.metadata.root == {
        key: value for key, value in expected.items() if key != "inbox_managed"
    }


@pytest.mark.parametrize("source", ["message.adapter", "terminal.bridge", "mobile.input"])
async def test_new_user_work_routes_through_one_gateway_for_any_adapter(source: str) -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn()])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )

    result = await service.submit(
        InteractionEnvelope(
            id=new_interaction_id(),
            source=source,
            content=f"Create one new Task from {source}.",
        )
    )

    aggregate = await load_run(factory, result.run_id)
    assert aggregate.task.metadata.root["source"] == source
    assert aggregate.task.metadata.root["interaction_route"] == "new_task"
    assert model.calls == 1


async def test_interaction_chat_maps_each_envelope_to_one_run_node() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(factory, [final_turn(), final_turn()]),
            CapabilityRegistry(),
            factory,
        ),
        unit_of_work=factory,
    )
    chat = service.chat()
    first_id, second_id = new_interaction_id(), new_interaction_id()
    first = await chat.submit(
        InteractionEnvelope(id=first_id, source="message.adapter", content="First.")
    )
    second = await chat.submit(
        InteractionEnvelope(id=second_id, source="mobile.input", content="Second.")
    )
    closed = await chat.close()

    assert closed is not None
    assert first.run_id == second.run_id == closed.run_id
    aggregate = await load_run(factory, closed.run_id)
    assert len(aggregate.nodes) == 2
    assert aggregate.nodes[0].metadata.root["interaction_id"] == str(first_id)
    assert aggregate.nodes[1].metadata.root["interaction_id"] == str(second_id)
    assert aggregate.nodes[0].metadata.root["source"] == "message.adapter"
    assert aggregate.nodes[1].metadata.root["source"] == "mobile.input"


async def test_external_new_work_uses_the_same_async_entry() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(factory, [final_turn()]),
            CapabilityRegistry(),
            factory,
        ),
        unit_of_work=factory,
    )

    result = await service.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            source="message.adapter",
            content="Start one new Task through the asynchronous gateway entry.",
        )
    )

    aggregate = await load_run(factory, result.run_id)
    assert aggregate.task.metadata.root["source"] == "message.adapter"
    assert aggregate.task.metadata.root["interaction_route"] == "new_task"


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [
        (
            InteractionEnvelope(
                id=new_interaction_id(),
                input_kind=InteractionInputKind.ASYNC_CAPABILITY_RESULT,
                content="An asynchronous result.",
            ),
            "new_work_input_unavailable",
        ),
        (
            InteractionEnvelope(
                id=new_interaction_id(),
                input_kind=InteractionInputKind.SUPPLEMENTAL_INPUT,
                content="Supplement without a resumable Run.",
            ),
            "new_work_input_unavailable",
        ),
        (
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
            "resume_input_unavailable",
        ),
    ],
)
async def test_gateway_rejects_routes_owned_by_later_deliveries(
    envelope: InteractionEnvelope,
    reason: str,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn()])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )

    with pytest.raises(AnbanError) as captured:
        await service.submit(envelope)

    assert captured.value.info.details.root["reason"] == reason
    assert model.calls == 0
    inbox = await service.inbox()
    assert inbox[0].status.value == "rejected"
    assert inbox[0].failure_reason == reason
