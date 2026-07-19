"""Real child Runtime lifecycle, recovery, failure, and cancellation tests."""

from __future__ import annotations

import asyncio

from anban.capability import (
    AgentDelegateCapability,
    CapabilityRegistry,
    UnifiedCapabilityInventory,
)
from anban.core import AnbanError, ErrorCode, ErrorInfo
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
    WaitingExecution,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import (
    TransactionCheckingModel,
    assessment_turn,
    completion_turn,
    final_turn,
)


def delegate_turn(objective: str) -> ModelTurn:
    return ModelTurn(
        tool_calls=(
            ToolCall(
                id="delegate-dynamic-child",
                name="agent.delegate",
                arguments={"objective": objective},
            ),
        ),
        finish_reason="tool_calls",
    )


def runtime_with_delegate(
    factory: MemoryUnitOfWorkFactory,
    model: TransactionCheckingModel,
) -> tuple[PersistentRuntime, AgentDelegateCapability]:
    delegate = AgentDelegateCapability(factory)
    registry = CapabilityRegistry((delegate,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    runtime = PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        response_repair_retries=0,
    )
    delegate.bind(runtime.start_child)
    return runtime, delegate


async def terminal_child(factory: MemoryUnitOfWorkFactory, parent_run_id: object):
    for _ in range(500):
        children = tuple(
            run for run in factory.store.runs.values() if run.parent_run_id == parent_run_id
        )
        if children and children[0].status.value not in {"created", "running"}:
            return children[0]
        await asyncio.sleep(0)
    raise AssertionError("delegated child Run did not become terminal")


async def test_delegated_child_run_recovers_and_parent_aggregates_result() -> None:
    factory = MemoryUnitOfWorkFactory()
    objective = "Independently derive one changed bounded child result."
    child_final = "The independent child completed its bounded objective."
    parent_final = "The parent aggregated the independently persisted child result."
    initial_model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.DELEGATE, "agent.delegate"),
            delegate_turn(objective),
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, ""),
            final_turn(child_final),
            completion_turn(final_text=child_final),
        ],
    )
    initial, initial_delegate = runtime_with_delegate(factory, initial_model)

    waiting = await initial.start_async("Coordinate one independent child objective.")

    assert isinstance(waiting, WaitingExecution)
    child = await terminal_child(factory, waiting.run_id)
    await initial.detach_async(waiting.checkpoint_id)

    restarted_model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, ""),
            final_turn(parent_final),
            completion_turn(final_text=parent_final),
        ],
    )
    restarted, restarted_delegate = runtime_with_delegate(factory, restarted_model)
    result = await restarted.resume_async(waiting.checkpoint_id)

    assert not isinstance(result, WaitingExecution)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == parent_final
    parent = factory.store.runs[result.run_id]
    assert child.parent_run_id == parent.id
    assert child.parent_invocation_id is not None
    assert child.delegation_depth == 1
    assert child.final_text == child_final
    parent_aggregate = await ExecutionQueryService(factory).show(parent.id)
    child_aggregate = await ExecutionQueryService(factory).show(child.id)
    assert parent_aggregate.run.parent_run_id is None
    assert child_aggregate.run.parent_run_id == parent.id
    assert child_aggregate.run.parent_invocation_id == child.parent_invocation_id
    assert parent_aggregate.observability.complete is True
    assert child_aggregate.observability.complete is True
    parent_events = {event.event_type: event for event in parent_aggregate.observability.audit}
    child_events = {event.event_type: event for event in child_aggregate.observability.audit}
    assert parent_events["capability.completed"].metadata.root["child_run_id"] == str(child.id)
    assert child_events["subagent.child_created"].metadata.root["parent_run_id"] == str(parent.id)
    assert initial_model.turns == []
    assert restarted_model.turns == []
    await initial_delegate.aclose()
    await restarted_delegate.aclose()


async def test_child_failure_is_preserved_and_does_not_become_parent_success() -> None:
    factory = MemoryUnitOfWorkFactory()
    model_failure = AnbanError(
        ErrorInfo(code=ErrorCode.MODEL_REQUEST_FAILED, message="Child Model request failed")
    )
    parent_final = "The child failure prevents truthful completion."
    model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.DELEGATE, "agent.delegate"),
            delegate_turn("Attempt one independently failing child objective."),
            model_failure,
            final_turn(parent_final),
            completion_turn(
                resolution="fail",
                unmet_condition="The independently durable child Run failed.",
            ),
        ],
    )
    runtime, delegate = runtime_with_delegate(factory, model)

    result = await runtime.execute("Delegate work that must not fabricate a child success.")

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    children = tuple(
        run for run in factory.store.runs.values() if run.parent_run_id == result.run_id
    )
    assert len(children) == 1
    child = children[0]
    assert child.status.value == "failed"
    assert child.error_code is ErrorCode.MODEL_REQUEST_FAILED
    parent = await ExecutionQueryService(factory).show(result.run_id)
    invocation = parent.invocations[0]
    assert invocation.capability_name == "agent.delegate"
    assert invocation.status.value == "failed"
    assert invocation.error_code is ErrorCode.MODEL_REQUEST_FAILED
    assert parent.observability.complete is True
    assert "subagent.child_created" in {
        event.event_type for event in (await ExecutionQueryService(factory).trace(child.id)).audit
    }
    await delegate.aclose()


class BlockingChildModel(TransactionCheckingModel):
    def __init__(self, factory: MemoryUnitOfWorkFactory) -> None:
        super().__init__(
            factory,
            [
                assessment_turn(ExecutionStrategy.DELEGATE, "agent.delegate"),
                delegate_turn("Remain active until governed cancellation."),
            ],
        )
        self.child_started = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelTurn:
        if self.turns:
            return await super().complete(request)
        assert self.factory.active == 0
        self.calls += 1
        self.requests.append(request)
        self.child_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled child Model call returned")


async def test_parent_cancellation_propagates_to_real_child_run() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = BlockingChildModel(factory)
    runtime, delegate = runtime_with_delegate(factory, model)
    waiting = await runtime.start_async("Delegate one cancellable independent objective.")
    assert isinstance(waiting, WaitingExecution)
    await asyncio.wait_for(model.child_started.wait(), timeout=2)

    result = await runtime.cancel_async(waiting.checkpoint_id)

    assert result.outcome.status is AgentOutcomeStatus.CANCELLED
    child = await terminal_child(factory, result.run_id)
    assert child.status.value == "cancelled"
    parent = await ExecutionQueryService(factory).show(result.run_id)
    child_detail = await ExecutionQueryService(factory).show(child.id)
    assert parent.run.status.value == "cancelled"
    assert child_detail.run.status.value == "cancelled"
    assert parent.checkpoints[0].status.value == "cancelled"
    assert parent.observability.complete is True
    assert child_detail.observability.complete is True
    await delegate.aclose()
