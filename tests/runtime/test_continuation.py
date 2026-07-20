"""Durable in-process continuation over real background Capability execution."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from anban.capability import CapabilityRegistry
from anban.capability.process import ProcessCapability
from anban.capability.workspace import WorkspaceBoundary
from anban.core import CheckpointStatus, ErrorCode, new_checkpoint_id
from anban.core.errors import AnbanError
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    ExecutionQueryService,
    ExecutionResult,
    PersistentRuntime,
    TaskExecutionRoute,
    TaskRouteEvaluator,
    WaitingExecution,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_graph_routing import route_turn
from tests.runtime.test_graph_task_routing import one_action_graph


class ContinuationModel:
    def __init__(self, factory: MemoryUnitOfWorkFactory, turns: list[ModelTurn]) -> None:
        self._factory = factory
        self._turns = turns

    async def complete(self, request: ModelRequest) -> ModelTurn:
        assert self._factory.active == 0
        return self._turns.pop(0)


def background_turn(label: str, code: str) -> ModelTurn:
    return ModelTurn(
        tool_calls=(
            ToolCall(
                id=f"background-{label}",
                name="process.execute",
                arguments={
                    "command": sys.executable,
                    "args": ["-c", f"import time;time.sleep(.15);{code}"],
                    "background": True,
                },
            ),
        ),
        finish_reason="tool_calls",
    )


def runtime_for(
    tmp_path: Path,
    factory: MemoryUnitOfWorkFactory,
    turns: list[ModelTurn],
) -> PersistentRuntime:
    registry = CapabilityRegistry((ProcessCapability(WorkspaceBoundary(tmp_path)),))
    return PersistentRuntime(ContinuationModel(factory, turns), registry, factory)


async def load(factory: MemoryUnitOfWorkFactory, waiting: WaitingExecution):
    async with factory() as unit:
        return await unit.executions.load_run(waiting.run_id)


async def test_waiting_checkpoint_survives_fresh_query_and_resume_does_not_replay(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "journal",
                "from pathlib import Path;"
                "p=Path('journal.txt');"
                "p.write_text((p.read_text() if p.exists() else '')+'once\\n')",
            ),
            ModelTurn(content="The continued execution completed.", finish_reason="stop"),
        ],
    )

    waiting = await runtime.start_async("Run a real process and pause after accepted execution.")

    assert isinstance(waiting, WaitingExecution)
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.run.status.value == "running"
    assert aggregate.invocations[0].status.value == "running"
    assert len(aggregate.checkpoints) == 1
    checkpoint = aggregate.checkpoints[0]
    assert checkpoint.id == waiting.checkpoint_id
    assert checkpoint.status is CheckpointStatus.WAITING
    assert checkpoint.invocation_id == waiting.invocation_id
    assert {
        "checkpoint.created",
        "checkpoint.waiting",
        "run.waiting",
    } <= {event.event_type for event in aggregate.events}

    detail = await ExecutionQueryService(factory).show(waiting.run_id)
    assert detail.checkpoints[0].status is CheckpointStatus.WAITING
    result = await runtime.resume_async(waiting.checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.persisted
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert (tmp_path / "journal.txt").read_text() == "once\n"
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.COMPLETED
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("checkpoint.resumed") == 1
    assert event_types.count("checkpoint.completed") == 1
    assert event_types.count("run.resumed") == 1
    assert all(
        event.checkpoint_id == waiting.checkpoint_id
        for event in aggregate.events
        if event.event_type.startswith("checkpoint.")
        or event.event_type in {"run.waiting", "run.resumed"}
    )


async def test_cancel_waiting_checkpoint_stops_real_process_without_late_success(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "cancel",
                "import time;from pathlib import Path;time.sleep(.6);"
                "Path('late.txt').write_text('must not appear')",
            )
        ],
    )
    waiting = await runtime.start_async("Cancel the real process while execution is waiting.")
    assert isinstance(waiting, WaitingExecution)

    result = await runtime.cancel_async(waiting.checkpoint_id)

    assert result.persisted
    assert result.outcome.status is AgentOutcomeStatus.CANCELLED
    await asyncio.sleep(0.7)
    assert not (tmp_path / "late.txt").exists()
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.CANCELLED
    assert aggregate.invocations[0].status.value == "cancelled"
    event_types = {event.event_type for event in aggregate.events}
    assert {
        "checkpoint.cancel_requested",
        "run.cancel_requested",
        "checkpoint.cancelled",
    } <= event_types
    assert "capability.completed" not in event_types


async def test_interrupted_resume_retains_handle_for_durable_cancel(tmp_path: Path) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "interrupt",
                "import time;from pathlib import Path;time.sleep(.6);"
                "Path('interrupted-late.txt').write_text('must not appear')",
            )
        ],
    )
    waiting = await runtime.start_async("Interrupt a resumed wait, then cancel its real process.")
    assert isinstance(waiting, WaitingExecution)
    resumed = asyncio.create_task(runtime.resume_async(waiting.checkpoint_id))
    await asyncio.sleep(0.05)
    resumed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resumed

    result = await runtime.cancel_async(waiting.checkpoint_id)

    assert result.outcome.status is AgentOutcomeStatus.CANCELLED
    await asyncio.sleep(0.7)
    assert not (tmp_path / "interrupted-late.txt").exists()
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.CANCELLED
    assert aggregate.invocations[0].status.value == "cancelled"


async def test_multiple_checkpoints_resume_in_order_and_each_side_effect_runs_once(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn("first", "from pathlib import Path;Path('first.txt').write_text('A')"),
            background_turn(
                "second", "from pathlib import Path;Path('second.txt').write_text('B')"
            ),
            ModelTurn(content="Both continuations completed.", finish_reason="stop"),
        ],
    )

    first = await runtime.start_async("Execute two independently correlated background steps.")
    assert isinstance(first, WaitingExecution)
    second = await runtime.resume_async(first.checkpoint_id)
    assert isinstance(second, WaitingExecution)
    assert second.checkpoint_id != first.checkpoint_id
    result = await runtime.resume_async(second.checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert (tmp_path / "first.txt").read_text() == "A"
    assert (tmp_path / "second.txt").read_text() == "B"
    aggregate = await load(factory, first)
    assert aggregate is not None
    assert [checkpoint.status for checkpoint in aggregate.checkpoints] == [
        CheckpointStatus.COMPLETED,
        CheckpointStatus.COMPLETED,
    ]


async def test_resume_persistence_failure_keeps_waiting_execution_retry_safe(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "retry", "from pathlib import Path;Path('retry.txt').write_text('one')"
            ),
            ModelTurn(content="The retried resume completed.", finish_reason="stop"),
        ],
    )
    waiting = await runtime.start_async("Resume only after its durable event commits.")
    assert isinstance(waiting, WaitingExecution)
    factory.fail_event_type = "checkpoint.resumed"

    with pytest.raises(AnbanError) as raised:
        await runtime.resume_async(waiting.checkpoint_id)
    assert raised.value.info.code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.checkpoints[0].status is CheckpointStatus.WAITING

    result = await runtime.resume_async(waiting.checkpoint_id)
    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert (tmp_path / "retry.txt").read_text() == "one"


async def test_unknown_and_repeated_checkpoint_operations_fail_explicitly(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "known", "from pathlib import Path;Path('known.txt').write_text('done')"
            ),
            ModelTurn(content="Known checkpoint completed.", finish_reason="stop"),
        ],
    )

    with pytest.raises(AnbanError, match="unavailable"):
        await runtime.resume_async(new_checkpoint_id())
    with pytest.raises(AnbanError, match="unavailable"):
        await runtime.cancel_async(new_checkpoint_id())

    waiting = await runtime.start_async("Reject a repeated resume without replaying execution.")
    assert isinstance(waiting, WaitingExecution)
    result = await runtime.resume_async(waiting.checkpoint_id)
    assert isinstance(result, ExecutionResult)
    with pytest.raises(AnbanError, match="unavailable"):
        await runtime.resume_async(waiting.checkpoint_id)
    assert (tmp_path / "known.txt").read_text() == "done"


async def test_detach_releases_local_coroutine_without_cancelling_worker(tmp_path: Path) -> None:
    factory = MemoryUnitOfWorkFactory()
    runtime = runtime_for(
        tmp_path,
        factory,
        [
            background_turn(
                "detach",
                "import time;from pathlib import Path;time.sleep(.2);"
                "Path('detached.txt').write_text('durable')",
            )
        ],
    )

    waiting = await runtime.start_async("Detach local ownership from durable external work.")
    assert isinstance(waiting, WaitingExecution)
    await runtime.detach_async(waiting.checkpoint_id)
    detached = tmp_path / "detached.txt"
    state = tmp_path / ".anban" / "process" / str(waiting.invocation_id)
    result_path = state / "result.json"
    async with asyncio.timeout(2):
        while not result_path.is_file():
            await asyncio.sleep(0.01)

    assert detached.read_text() == "durable"
    assert not (state / "cancel").exists()
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.run.status.value == "running"
    assert aggregate.invocations[0].status.value == "running"
    assert aggregate.checkpoints[0].status is CheckpointStatus.WAITING


async def test_graph_node_uses_the_same_checkpoint_continuation_path(tmp_path: Path) -> None:
    factory = MemoryUnitOfWorkFactory()
    spec = one_action_graph()
    registry = CapabilityRegistry((ProcessCapability(WorkspaceBoundary(tmp_path)),))
    model = ContinuationModel(
        factory,
        [
            route_turn(TaskExecutionRoute.TASK_GRAPH.value, spec.model_dump(mode="json")),
            background_turn(
                "graph",
                "from pathlib import Path;Path('graph.txt').write_text('continued')",
            ),
            ModelTurn(content='{"result":"Graph continuation complete."}', finish_reason="stop"),
        ],
    )
    runtime = PersistentRuntime(
        model,
        registry,
        factory,
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    )

    waiting = await runtime.start_async("Route a background operation through one graph node.")
    assert isinstance(waiting, WaitingExecution)
    result = await runtime.resume_async(waiting.checkpoint_id)

    assert isinstance(result, ExecutionResult)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "Graph continuation complete."
    assert (tmp_path / "graph.txt").read_text() == "continued"
    aggregate = await load(factory, waiting)
    assert aggregate is not None
    assert aggregate.checkpoints[0].node_run_id == waiting.node_run_id
    assert aggregate.checkpoints[0].status is CheckpointStatus.COMPLETED
