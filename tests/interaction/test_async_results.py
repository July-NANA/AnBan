"""Asynchronous result signals resume authoritative Capability results."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityProgress,
    CapabilityProgressStatus,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.core import AnbanError, CheckpointStatus, ContextScope, SafeMetadata
from anban.core.ids import new_interaction_id
from anban.interaction import (
    CorrelatedWaitingExecution,
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
    InteractionService,
)
from anban.model import ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_continuation import background_turn
from tests.runtime.test_persistent_runtime import (
    TransactionCheckingModel,
    assessment_turn,
    completion_turn,
    final_turn,
)
from tests.runtime.test_recovery import registry as process_registry
from tests.runtime.test_recovery import sufficiency


def result_signal(
    waiting: CorrelatedWaitingExecution,
    input_kind: InteractionInputKind,
    content: str,
    delivery: str,
    *,
    source: str,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source=source,
        input_kind=input_kind,
        content=content,
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=waiting.resume_key,
            deduplication_key=CorrelationKey(
                purpose=CorrelationPurpose.DEDUPLICATION,
                namespace="external.result-delivery",
                value=delivery,
            ),
        ),
    )


async def test_process_result_signal_recovers_real_result_and_rejects_wrong_kind(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    initial_registry = process_registry(tmp_path)
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    background_turn(
                        "result-signal",
                        "import time;from pathlib import Path;time.sleep(.15);"
                        "p=Path('result-signal-count.txt');"
                        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
                    ),
                ],
            ),
            initial_registry,
            factory,
            sufficiency=sufficiency(initial_registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    )
    waiting = await initial.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Run one background Process and wait for its asynchronous result.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

    restarted_registry = process_registry(tmp_path)
    final = "The authoritative Process result completed without replay."
    restarted_model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
            final_turn(final),
            completion_turn(final_text=final),
        ],
    )
    restarted = InteractionService(
        PersistentRuntime(
            restarted_model,
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    )

    with pytest.raises(AnbanError) as mismatch:
        await restarted.submit(
            result_signal(
                waiting,
                InteractionInputKind.MCP_RESULT,
                "A mismatched result-ready signal.",
                "wrong-kind",
                source="mcp.adapter",
            )
        )
    assert mismatch.value.info.details.root["reason"] == "result_kind_mismatch"
    correct = result_signal(
        waiting,
        InteractionInputKind.ASYNC_CAPABILITY_RESULT,
        "The Process supervisor reports that its result is ready.",
        "process-ready",
        source="process.adapter",
    )
    result = await restarted.submit(correct)
    model_calls = restarted_model.calls
    duplicate = await restarted.submit(
        result_signal(
            waiting,
            InteractionInputKind.ASYNC_CAPABILITY_RESULT,
            correct.content,
            "process-ready",
            source="process.adapter",
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == final
    assert duplicate.run_id == result.run_id
    assert restarted_model.calls == model_calls
    assert (tmp_path / "result-signal-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        entries = await unit.executions.list_context_entries(ContextScope.TASK, waiting.task_id)
    assert aggregate is not None
    assert entries == ()
    assert aggregate.invocations[0].status.value == "succeeded"
    assert aggregate.checkpoints[0].status is CheckpointStatus.COMPLETED
    received = [
        event for event in aggregate.events if event.event_type == "interaction.result_received"
    ]
    assert len(received) == 1
    assert received[0].invocation_id == waiting.invocation_id
    assert received[0].checkpoint_id == waiting.checkpoint_id
    assert received[0].metadata.root["inventory_kind"] == "process"
    assert received[0].metadata.root["side_effect_replayed"] is False
    assert "process-ready" not in str(aggregate.events)
    inbox = await restarted.inbox()
    wrong = next(item for item in inbox if item.input_kind == "mcp_result")
    delivered = next(item for item in inbox if item.input_kind == "async_capability_result")
    assert wrong.status.value == "rejected"
    assert wrong.failure_reason == "result_kind_mismatch"
    assert wrong.run_id is None
    assert delivered.status.value == "processed"
    assert delivered.delivery_count == 2
    assert all(item.status.value in {"processed", "rejected"} for item in inbox)
    trace = await ExecutionQueryService(factory).trace(waiting.run_id)
    assert trace.complete is True
    assert trace.inconsistencies == ()


async def test_result_signal_releases_the_live_continuation_without_registry_restore(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    registry = process_registry(tmp_path)
    final = "The live continuation consumed the authoritative Process result."
    model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
            background_turn(
                "live-result",
                "from pathlib import Path;Path('live-result.txt').write_text('once')",
            ),
            final_turn(final),
            completion_turn(final_text=final),
        ],
    )
    service = InteractionService(
        PersistentRuntime(
            model,
            registry,
            factory,
            sufficiency=sufficiency(registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    )
    waiting = await service.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Consume one live asynchronous Process result.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)

    result = await service.submit(
        result_signal(
            waiting,
            InteractionInputKind.ASYNC_CAPABILITY_RESULT,
            "The live Process result is ready.",
            "live-result-ready",
            source="process.adapter",
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == final
    assert (tmp_path / "live-result.txt").read_text() == "once"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
    assert aggregate is not None
    assert sum(event.event_type == "run.recovery_started" for event in aggregate.events) == 0
    assert sum(event.event_type == "interaction.result_received" for event in aggregate.events) == 1
    assert aggregate.checkpoints[0].status is CheckpointStatus.COMPLETED


class RecoverableResultHandler:
    def __init__(self, name: str, inventory_kind: InventoryKind, observation: str) -> None:
        self.descriptor = CapabilityDescriptor(
            name=name,
            description=f"Return one deterministic {inventory_kind.value} test result.",
            inventory_kind=inventory_kind,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "minLength": 1}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self._observation = observation
        self.cancelled = False

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.ACCEPTED,
            metadata=SafeMetadata({"restart_recoverable": True}),
        )

    async def recover(self, context: InvocationContext, progress_sequence: int) -> None:
        assert progress_sequence == 0

    async def progress(self, context: InvocationContext) -> CapabilityProgress:
        return CapabilityProgress(sequence=1, status=CapabilityProgressStatus.RESULT_READY)

    async def wait(self, context: InvocationContext) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=self._observation,
        )

    async def cancel(self, context: InvocationContext) -> None:
        self.cancelled = True


def accepted_turn(name: str, label: str) -> ModelTurn:
    return ModelTurn(
        tool_calls=(ToolCall(id=f"accepted-{label}", name=name, arguments={"value": label}),),
        finish_reason="tool_calls",
    )


@pytest.mark.parametrize(
    ("input_kind", "inventory_kind", "strategy", "name", "label"),
    [
        (
            InteractionInputKind.MCP_RESULT,
            InventoryKind.MCP,
            ExecutionStrategy.USE_CAPABILITY,
            "dynamic.protocol",
            "protocol",
        ),
        (
            InteractionInputKind.SUBAGENT_RESULT,
            InventoryKind.SUB_AGENT,
            ExecutionStrategy.DELEGATE,
            "agent.delegate",
            "delegate",
        ),
    ],
)
async def test_protocol_and_subagent_signals_share_one_result_delivery_path(
    input_kind: InteractionInputKind,
    inventory_kind: InventoryKind,
    strategy: ExecutionStrategy,
    name: str,
    label: str,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    initial_handler = RecoverableResultHandler(name, inventory_kind, f"real-{label}-result")
    initial_registry = CapabilityRegistry((initial_handler,))
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [assessment_turn(strategy, name), accepted_turn(name, label)],
            ),
            initial_registry,
            factory,
            sufficiency=sufficiency(initial_registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    )
    waiting = await initial.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content=f"Wait for one changed {label} result object.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

    restarted_handler = RecoverableResultHandler(name, inventory_kind, f"real-{label}-result")
    restarted_registry = CapabilityRegistry((restarted_handler,))
    final = f"The {label} result was consumed through its authoritative lifecycle."
    model = TransactionCheckingModel(
        factory,
        [assessment_turn(strategy, name), final_turn(final), completion_turn(final_text=final)],
    )
    restarted = InteractionService(
        PersistentRuntime(
            model,
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    )
    delivery = f"{label}-delivery"
    signal = result_signal(
        waiting,
        input_kind,
        f"The {label} result is ready for authoritative retrieval.",
        delivery,
        source=f"{label}.adapter",
    )
    result = await restarted.submit(signal)
    calls = model.calls
    duplicate = await restarted.submit(
        result_signal(
            waiting,
            input_kind,
            signal.content,
            delivery,
            source=f"{label}.adapter",
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert duplicate.run_id == result.run_id
    assert model.calls == calls
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
    assert aggregate is not None
    received = next(
        event for event in aggregate.events if event.event_type == "interaction.result_received"
    )
    assert received.metadata.root["input_kind"] == input_kind.value
    assert received.metadata.root["inventory_kind"] == inventory_kind.value
    assert received.metadata.root["capability_name"] == name
    assert (
        sum(event.event_type == "interaction.result_correlated" for event in aggregate.events) == 1
    )
    inbox = await restarted.inbox()
    delivered = next(item for item in inbox if item.route == "resume_eligible_run")
    assert delivered.status.value == "processed"
    assert delivered.delivery_count == 2
    trace = await ExecutionQueryService(factory).trace(waiting.run_id)
    assert trace.complete is True
    assert trace.inconsistencies == ()
