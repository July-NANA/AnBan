"""Runtime persistence tests with a transactional test-only Port implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    EventId,
    ExecutionRunId,
    NodeRunId,
    TaskId,
    new_artifact_id,
)
from anban.core.models import Artifact, CapabilityInvocation, Event, ExecutionRun, NodeRun, Task
from anban.core.persistence import ExecutionRunAggregate
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import AgentOutcomeStatus, PersistentRuntime


@dataclass
class MemoryStore:
    tasks: dict[TaskId, Task] = field(default_factory=lambda: dict[TaskId, Task]())
    runs: dict[ExecutionRunId, ExecutionRun] = field(
        default_factory=lambda: dict[ExecutionRunId, ExecutionRun]()
    )
    nodes: dict[NodeRunId, NodeRun] = field(default_factory=lambda: dict[NodeRunId, NodeRun]())
    invocations: dict[CapabilityInvocationId, CapabilityInvocation] = field(
        default_factory=lambda: dict[CapabilityInvocationId, CapabilityInvocation]()
    )
    artifacts: dict[ArtifactId, Artifact] = field(
        default_factory=lambda: dict[ArtifactId, Artifact]()
    )
    events: dict[EventId, Event] = field(default_factory=lambda: dict[EventId, Event]())

    def copy(self) -> MemoryStore:
        return MemoryStore(
            tasks=dict(self.tasks),
            runs=dict(self.runs),
            nodes=dict(self.nodes),
            invocations=dict(self.invocations),
            artifacts=dict(self.artifacts),
            events=dict(self.events),
        )


class MemoryRepository:
    def __init__(self, store: MemoryStore, factory: MemoryUnitOfWorkFactory) -> None:
        self.store = store
        self.factory = factory

    async def add_task(self, task: Task) -> None:
        self.store.tasks[task.id] = task

    async def get_task(self, task_id: TaskId) -> Task | None:
        return self.store.tasks.get(task_id)

    async def update_task(self, task: Task) -> None:
        self.store.tasks[task.id] = task

    async def add_run(self, run: ExecutionRun) -> None:
        self.store.runs[run.id] = run

    async def get_run(self, run_id: ExecutionRunId) -> ExecutionRun | None:
        return self.store.runs.get(run_id)

    async def update_run(self, run: ExecutionRun) -> None:
        self.store.runs[run.id] = run

    async def add_node_run(self, node_run: NodeRun) -> None:
        self.store.nodes[node_run.id] = node_run

    async def get_node_run(self, node_run_id: NodeRunId) -> NodeRun | None:
        return self.store.nodes.get(node_run_id)

    async def update_node_run(self, node_run: NodeRun) -> None:
        self.store.nodes[node_run.id] = node_run

    async def add_invocation(self, invocation: CapabilityInvocation) -> None:
        self.store.invocations[invocation.id] = invocation

    async def get_invocation(
        self, invocation_id: CapabilityInvocationId
    ) -> CapabilityInvocation | None:
        return self.store.invocations.get(invocation_id)

    async def update_invocation(self, invocation: CapabilityInvocation) -> None:
        self.store.invocations[invocation.id] = invocation

    async def add_artifact(self, artifact: Artifact) -> None:
        self.store.artifacts[artifact.id] = artifact

    async def get_artifact(self, artifact_id: ArtifactId) -> Artifact | None:
        return self.store.artifacts.get(artifact_id)

    async def add_event(self, event: Event) -> None:
        if self.factory.fail_event_type == event.event_type:
            self.factory.fail_event_type = None
            raise RuntimeError("test-only persistence failure")
        self.store.events[event.id] = event

    async def get_event(self, event_id: EventId) -> Event | None:
        return self.store.events.get(event_id)

    async def list_events(self, run_id: ExecutionRunId) -> tuple[Event, ...]:
        return tuple(
            sorted(
                (event for event in self.store.events.values() if event.run_id == run_id),
                key=lambda event: (event.sequence, event.id),
            )
        )

    async def load_run(self, run_id: ExecutionRunId) -> ExecutionRunAggregate | None:
        run = self.store.runs.get(run_id)
        if run is None:
            return None
        task = self.store.tasks[run.task_id]
        return ExecutionRunAggregate(
            task=task,
            run=run,
            nodes=tuple(node for node in self.store.nodes.values() if node.run_id == run_id),
            invocations=tuple(
                invocation
                for invocation in self.store.invocations.values()
                if invocation.run_id == run_id
            ),
            artifacts=tuple(
                artifact for artifact in self.store.artifacts.values() if artifact.run_id == run_id
            ),
            events=await self.list_events(run_id),
        )


class MemoryUnitOfWork:
    def __init__(self, factory: MemoryUnitOfWorkFactory) -> None:
        self.factory = factory
        self.working = factory.store.copy()
        self.executions = MemoryRepository(self.working, factory)
        self.committed = False

    async def __aenter__(self) -> Self:
        self.factory.active += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.factory.active -= 1

    async def commit(self) -> None:
        self.factory.store = self.working
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


class MemoryUnitOfWorkFactory:
    def __init__(self) -> None:
        self.store = MemoryStore()
        self.active = 0
        self.fail_event_type: str | None = None

    def __call__(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self)


class TransactionCheckingModel:
    def __init__(
        self, factory: MemoryUnitOfWorkFactory, turns: list[ModelTurn | AnbanError]
    ) -> None:
        self.factory = factory
        self.turns = turns
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelTurn:
        assert self.factory.active == 0
        self.calls += 1
        turn = self.turns.pop(0)
        if isinstance(turn, AnbanError):
            raise turn
        return turn


class TransactionCheckingCapability:
    def __init__(
        self,
        factory: MemoryUnitOfWorkFactory,
        result: CapabilityResult,
    ) -> None:
        self.factory = factory
        self.result = result
        self.calls = 0
        self.descriptor = CapabilityDescriptor(
            name="file.write",
            description="Write one bounded Workspace file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "content": {"type": "string", "maxLength": 16_384},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        assert self.factory.active == 0
        self.calls += 1
        return self.result

    async def cancel(self, context: InvocationContext) -> None:
        assert self.factory.active == 0


def tool_turn() -> ModelTurn:
    return ModelTurn(
        tool_calls=(
            ToolCall(
                id="call-1",
                name="file.write",
                arguments={"path": "result.txt", "content": "bounded"},
            ),
        ),
        finish_reason="tool_calls",
    )


def final_turn() -> ModelTurn:
    return ModelTurn(content="Persistent final result.", finish_reason="stop")


def completed_capability(*, artifact: bool = False) -> CapabilityResult:
    artifacts = ()
    if artifact:
        artifact_id = new_artifact_id()
        artifacts = (
            ArtifactReference(
                id=artifact_id,
                uri=f"anban://artifact/{artifact_id}",
                sha256="a" * 64,
                size_bytes=7,
                media_type="text/plain",
            ),
        )
    return CapabilityResult(
        status=CapabilityResultStatus.COMPLETED,
        observation="bounded write completed",
        artifacts=artifacts,
    )


async def load_run(
    factory: MemoryUnitOfWorkFactory, run_id: ExecutionRunId
) -> ExecutionRunAggregate:
    async with factory() as unit:
        aggregate = await unit.executions.load_run(run_id)
    assert aggregate is not None
    return aggregate


async def test_success_is_durable_and_external_calls_have_no_open_transaction() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [tool_turn(), final_turn()])
    capability = TransactionCheckingCapability(factory, completed_capability(artifact=True))
    result = await PersistentRuntime(
        model,
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Write the bounded result and answer.")

    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert model.calls == 2
    assert capability.calls == 1
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.task.status.value == "succeeded"
    assert aggregate.run.status.value == "succeeded"
    assert aggregate.nodes[0].status.value == "succeeded"
    assert aggregate.invocations[0].status.value == "succeeded"
    assert len(aggregate.artifacts) == 1
    assert tuple(event.sequence for event in aggregate.events) == tuple(
        range(1, len(aggregate.events) + 1)
    )
    assert {event.event_type for event in aggregate.events} >= {
        "model.requested",
        "model.completed",
        "capability.requested",
        "capability.completed",
        "artifact.created",
        "run.final",
    }


async def test_model_failure_is_persisted_as_safe_terminal_state() -> None:
    factory = MemoryUnitOfWorkFactory()
    failure = AnbanError(
        ErrorInfo(code=ErrorCode.MODEL_REQUEST_FAILED, message="Model request failed")
    )
    model = TransactionCheckingModel(factory, [failure])
    result = await PersistentRuntime(model, CapabilityRegistry(), factory).execute("Fail safely.")

    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.run.error_code is ErrorCode.MODEL_REQUEST_FAILED
    assert "model.failed" in {event.event_type for event in aggregate.events}
    assert "run.error" in {event.event_type for event in aggregate.events}


async def test_capability_failure_and_timeout_are_persisted() -> None:
    for capability_status, expected in (
        (CapabilityResultStatus.FAILED, AgentOutcomeStatus.FAILED),
        (CapabilityResultStatus.TIMED_OUT, AgentOutcomeStatus.TIMED_OUT),
    ):
        factory = MemoryUnitOfWorkFactory()
        error_code = (
            ErrorCode.EXECUTION_TIMED_OUT
            if capability_status is CapabilityResultStatus.TIMED_OUT
            else ErrorCode.CAPABILITY_EXECUTION_FAILED
        )
        capability = TransactionCheckingCapability(
            factory,
            CapabilityResult(
                status=capability_status,
                error=ErrorInfo(code=error_code, message="Capability failed safely"),
            ),
        )
        result = await PersistentRuntime(
            TransactionCheckingModel(factory, [tool_turn()]),
            CapabilityRegistry((capability,)),
            factory,
        ).execute("Invoke the bounded Capability.")
        assert result.persisted is True
        assert result.outcome.status is expected
        aggregate = await load_run(factory, result.run_id)
        assert aggregate.invocations[0].status.value == expected.value
        assert aggregate.run.status.value == expected.value


async def test_initial_persistence_failure_never_calls_model() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "task.created"
    model = TransactionCheckingModel(factory, [final_turn()])

    result = await PersistentRuntime(model, CapabilityRegistry(), factory).execute(
        "Do not execute without identity."
    )

    assert result.persisted is False
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    assert model.calls == 0


async def test_final_event_failure_replaces_success_with_durable_failure() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "run.succeeded"
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [final_turn()]),
        CapabilityRegistry(),
        factory,
    ).execute("Finish only after durable Events.")

    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.run.status.value == "failed"
    assert aggregate.run.final_text is None


async def test_post_side_effect_event_failure_does_not_retry_capability() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "capability.completed"
    capability = TransactionCheckingCapability(factory, completed_capability())
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [tool_turn(), final_turn()]),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Perform exactly one bounded side effect.")

    assert capability.calls == 1
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.run.status.value == "failed"
    assert aggregate.invocations[0].status.value == "running"
