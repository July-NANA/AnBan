"""Restart reconstruction from durable Checkpoint and Process supervisor facts."""

from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from anban.capability import (
    CapabilityRegistry,
    CapabilityResultStatus,
    InvocationContext,
    UnifiedCapabilityInventory,
)
from anban.capability.process import ProcessCapability
from anban.capability.workspace import WorkspaceBoundary
from anban.core import (
    AnbanError,
    CheckpointId,
    CheckpointStatus,
    ExecutionRun,
    NodeRun,
    SafeMetadata,
    Task,
    TaskGraphEdge,
    TaskGraphSpec,
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
    new_task_id,
)
from anban.model import ModelRequest, ModelTurn
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionResult,
    ExecutionStrategy,
    PersistentRuntime,
    TaskRouteEvaluator,
    WaitingExecution,
)
from anban.runtime.capability_persistence import PersistedCapabilityPort
from anban.runtime.persistence import RunPersistence
from tests.core.test_graph import action, node_output
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_continuation import background_turn
from tests.runtime.test_graph_routing import route_turn
from tests.runtime.test_persistent_runtime import (
    TransactionCheckingModel,
    assessment_turn,
    completion_turn,
    final_turn,
)


def registry(root: Path) -> CapabilityRegistry:
    return CapabilityRegistry((ProcessCapability(WorkspaceBoundary(root)),))


def sufficiency(gateway: CapabilityRegistry) -> CapabilitySufficiencyEvaluator:
    return CapabilitySufficiencyEvaluator(UnifiedCapabilityInventory(gateway, model_available=True))


async def setup_waiting(
    root: Path,
    factory: MemoryUnitOfWorkFactory,
    code: str,
) -> tuple[CheckpointId, InvocationContext]:
    task = Task(id=new_task_id(), request="Recover one real background Process result.")
    run = ExecutionRun(id=new_execution_run_id(), task_id=task.id)
    node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
    persistence = RunPersistence(factory, task, run, node)
    await persistence.initialize()
    await persistence.start()
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    context = InvocationContext(
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=new_capability_invocation_id(),
        deadline_at=deadline,
        metadata=SafeMetadata(
            {
                "call_signature": "a" * 64,
                "deadline_epoch_ms": int(deadline.timestamp() * 1000),
            }
        ),
    )
    port = PersistedCapabilityPort(
        registry(root),
        persistence,
        checkpoint_background=True,
    )
    accepted = await port.invoke(
        "process.execute",
        {
            "command": sys.executable,
            "args": ["-c", code],
            "background": True,
        },
        context,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED
    checkpoint_id = CheckpointId(UUID(str(accepted.metadata.root["checkpoint_id"])))
    return checkpoint_id, context


async def load(factory: MemoryUnitOfWorkFactory, context: InvocationContext):
    async with factory() as unit:
        return await unit.executions.load_run(context.run_id)


async def test_fresh_runtime_resumes_real_result_without_replaying_side_effect(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    checkpoint_id, context = await setup_waiting(
        tmp_path,
        factory,
        "from pathlib import Path;"
        "p=Path('restart-count.txt');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )
    restarted_registry = registry(tmp_path)
    final_text = "The service-restart continuation used the recovered execution evidence."
    model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
            final_turn(final_text),
            completion_turn(final_text=final_text),
        ],
    )
    restarted = PersistentRuntime(
        model,
        restarted_registry,
        factory,
        sufficiency=sufficiency(restarted_registry),
    )

    result = await restarted.resume_async(checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == final_text
    assert (tmp_path / "restart-count.txt").read_text() == "1"
    aggregate = await load(factory, context)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.COMPLETED
    assert aggregate.invocations[0].status.value == "succeeded"
    assert aggregate.run.status.value == "succeeded"
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("run.recovery_started") == 1
    assert event_types.count("run.recovery_completed") == 1
    completed = next(
        event for event in aggregate.events if event.event_type == "run.recovery_completed"
    )
    assert completed.metadata.root["side_effect_replayed"] is False


class NoModelCalls:
    async def complete(self, request: ModelRequest) -> ModelTurn:
        raise AssertionError("cancel recovery must not request a model completion")


async def test_fresh_runtime_cancel_terminates_worker_without_late_success(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    checkpoint_id, context = await setup_waiting(
        tmp_path,
        factory,
        "import time;from pathlib import Path;time.sleep(.6);"
        "Path('restart-late.txt').write_text('must not appear')",
    )
    restarted_registry = registry(tmp_path)
    restarted = PersistentRuntime(
        NoModelCalls(),
        restarted_registry,
        factory,
        sufficiency=sufficiency(restarted_registry),
    )

    result = await restarted.cancel_async(checkpoint_id)

    assert result.outcome.status is AgentOutcomeStatus.CANCELLED
    await asyncio.sleep(0.7)
    assert not (tmp_path / "restart-late.txt").exists()
    aggregate = await load(factory, context)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.CANCELLED
    assert aggregate.run.status.value == "cancelled"


async def test_missing_supervisor_state_is_an_explicit_durable_failure(tmp_path: Path) -> None:
    factory = MemoryUnitOfWorkFactory()
    checkpoint_id, context = await setup_waiting(
        tmp_path,
        factory,
        "import time;time.sleep(.4)",
    )
    await asyncio.sleep(0.5)
    state = tmp_path / ".anban" / "process" / str(context.invocation_id)
    shutil.rmtree(state)
    restarted_registry = registry(tmp_path)
    restarted = PersistentRuntime(
        NoModelCalls(),
        restarted_registry,
        factory,
        sufficiency=sufficiency(restarted_registry),
    )

    result = await restarted.resume_async(checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code.value == "capability_execution_failed"
    aggregate = await load(factory, context)
    assert aggregate is not None
    assert aggregate.run.status.value == "failed"
    assert "run.recovery_failed" in {event.event_type for event in aggregate.events}


def three_action_graph() -> TaskGraphSpec:
    prepare = action("prepare_data", outputs=("seed",))
    perform = action(
        "perform_effect",
        dependencies=(prepare.id,),
        inputs={"seed": node_output(prepare.id, "seed")},
        outputs=("middle",),
    )
    publish = action(
        "publish_result",
        dependencies=(perform.id,),
        inputs={"middle": node_output(perform.id, "middle")},
        outputs=("result",),
    )
    return TaskGraphSpec(
        nodes=(prepare, perform, publish),
        edges=(
            TaskGraphEdge(source=prepare.id, target=perform.id),
            TaskGraphEdge(source=perform.id, target=publish.id),
        ),
        entry_node=prepare.id,
        terminal_nodes=(publish.id,),
        outputs={"result": node_output(publish.id, "result")},
    )


def direct_action_turns(content: str) -> list[ModelTurn]:
    return [
        assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
        final_turn(content),
        completion_turn(final_text=content),
    ]


async def test_graph_recovery_reuses_prior_output_and_continues_future_node(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    spec = three_action_graph()
    initial_registry = registry(tmp_path)
    initial_turns: list[ModelTurn | AnbanError] = [
        route_turn("task_graph", spec.model_dump(mode="json")),
        *direct_action_turns('{"seed":"prepared-once"}'),
        assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
        background_turn(
            "graph-restart",
            "import time;from pathlib import Path;time.sleep(.15);"
            "p=Path('graph-restart-count.txt');"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
        ),
    ]
    started = await PersistentRuntime(
        TransactionCheckingModel(factory, initial_turns),
        initial_registry,
        factory,
        sufficiency=sufficiency(initial_registry),
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    ).start_async("Recover a graph action without repeating any prior action.")
    assert isinstance(started, WaitingExecution)

    restarted_registry = registry(tmp_path)
    active_final = '{"middle":"recovered-once"}'
    future_final = '{"result":"graph-finished"}'
    restarted = PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                final_turn(active_final),
                completion_turn(final_text="A rewritten recovered-action summary."),
                *direct_action_turns(future_final),
            ],
        ),
        restarted_registry,
        factory,
        sufficiency=sufficiency(restarted_registry),
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    )

    result = await restarted.resume_async(started.checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "graph-finished"
    assert (tmp_path / "graph-restart-count.txt").read_text() == "1"
    aggregate = await load(
        factory,
        InvocationContext(
            run_id=started.run_id,
            node_run_id=started.node_run_id,
            invocation_id=started.invocation_id,
            deadline_at=datetime.now(UTC) + timedelta(seconds=1),
        ),
    )
    assert aggregate is not None
    graph_nodes = [node for node in aggregate.nodes if node.node_name != "general_agent"]
    assert [node.node_name for node in graph_nodes] == [
        "prepare_data",
        "perform_effect",
        "publish_result",
    ]
    assert [node.output for node in graph_nodes] == [
        {"seed": "prepared-once"},
        {"middle": "recovered-once"},
        {"result": "graph-finished"},
    ]
    assert all(node.status.value == "succeeded" for node in graph_nodes)
    assert aggregate.run.status.value == "succeeded"
