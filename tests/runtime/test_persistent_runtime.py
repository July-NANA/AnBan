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
    UnifiedCapabilityInventory,
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
from anban.core.metadata import SafeMetadata
from anban.core.models import Artifact, CapabilityInvocation, Event, ExecutionRun, NodeRun, Task
from anban.core.persistence import ExecutionRunAggregate
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
)


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
        if self.factory.fail_add_task:
            self.factory.fail_add_task = False
            raise RuntimeError("test-only state write failure")
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
        if self.factory.fail_add_artifact:
            self.factory.fail_add_artifact = False
            raise RuntimeError("test-only Artifact write failure")
        self.store.artifacts[artifact.id] = artifact

    async def get_artifact(self, artifact_id: ArtifactId) -> Artifact | None:
        return self.store.artifacts.get(artifact_id)

    async def add_event(self, event: Event) -> None:
        if self.factory.commit_before_event_failure_type == event.event_type:
            self.factory.commit_before_event_failure_type = None
            self.store.events[event.id] = event
            self.factory.store = self.store.copy()
            raise RuntimeError("test-only ambiguous commit response")
        if self.factory.fail_event_types and self.factory.fail_event_types[0] == event.event_type:
            self.factory.fail_event_types.pop(0)
            raise RuntimeError("test-only queued Event failure")
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

    async def list_runs(self, limit: int) -> tuple[ExecutionRun, ...]:
        return tuple(
            sorted(
                self.store.runs.values(),
                key=lambda run: (run.created_at, run.id),
                reverse=True,
            )[:limit]
        )

    async def load_run(self, run_id: ExecutionRunId) -> ExecutionRunAggregate | None:
        if self.factory.fail_next_load:
            self.factory.fail_next_load = False
            raise RuntimeError("test-only one-shot read failure")
        if self.factory.fail_load:
            raise RuntimeError("test-only read failure")
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
        self.fail_event_types: list[str] = []
        self.commit_before_event_failure_type: str | None = None
        self.fail_load = False
        self.fail_next_load = False
        self.fail_add_task = False
        self.fail_add_artifact = False

    def __call__(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self)


class TransactionCheckingModel:
    def __init__(
        self, factory: MemoryUnitOfWorkFactory, turns: list[ModelTurn | AnbanError]
    ) -> None:
        self.factory = factory
        self.turns = turns
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        assert self.factory.active == 0
        self.calls += 1
        self.requests.append(request)
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
            name="test.action",
            description="Perform one bounded test action.",
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
                name="test.action",
                arguments={"path": "result.txt", "content": "bounded"},
            ),
        ),
        finish_reason="tool_calls",
    )


def final_turn(content: str = "Persistent final result.") -> ModelTurn:
    return ModelTurn(content=content, finish_reason="stop")


def assessment_turn(
    strategy: ExecutionStrategy = ExecutionStrategy.USE_CAPABILITY,
    target: str = "test.action",
) -> ModelTurn:
    return ModelTurn(
        structured_output={
            "strategy": strategy.value,
            "target": target,
            "rationale": "The current bounded inventory supports this path.",
            "confidence": 0.86,
            "missing_condition": "",
            "substantial_temporary_code": False,
            "complex_domain_workflow": False,
            "high_improvisation_risk": False,
            "low_implementation_confidence": False,
            "repeated_reusable_need": False,
            "existing_process_path_unreasonable": False,
        },
        finish_reason="stop",
    )


def completed_capability(*, artifact_count: int = 0) -> CapabilityResult:
    artifacts: tuple[ArtifactReference, ...] = ()
    if artifact_count:
        references: list[ArtifactReference] = []
        for index in range(artifact_count):
            artifact_id = new_artifact_id()
            references.append(
                ArtifactReference(
                    id=artifact_id,
                    uri=f"anban://artifact/{artifact_id}",
                    sha256=f"{index + 1:x}" * 64,
                    size_bytes=7 + index,
                    media_type="text/plain",
                )
            )
        artifacts = tuple(references)
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
    capability = TransactionCheckingCapability(factory, completed_capability(artifact_count=2))
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
    assert len(aggregate.artifacts) == 2
    assert len({artifact.invocation_id for artifact in aggregate.artifacts}) == 1
    assert sum(event.event_type == "artifact.created" for event in aggregate.events) == 2
    terminal = next(event for event in aggregate.events if event.event_type == "run.final")
    assert terminal.metadata.root["model_turn_count"] == 2
    assert terminal.metadata.root["capability_call_count"] == 1
    assert terminal.metadata.root["artifact_count"] == 2
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


async def test_sufficiency_and_observations_are_durable_in_the_same_run() -> None:
    factory = MemoryUnitOfWorkFactory()
    capability = TransactionCheckingCapability(factory, completed_capability())
    registry = CapabilityRegistry((capability,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [assessment_turn(), tool_turn(), final_turn()]),
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Assess, execute, observe, and complete one dynamic task.")

    assert result.persisted
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.model_turn_count == 3
    aggregate = await load_run(factory, result.run_id)
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("agent.sufficiency_assessed") == 1
    assert event_types.count("agent.observed") == 1
    assessment = next(
        event for event in aggregate.events if event.event_type == "agent.sufficiency_assessed"
    )
    assert assessment.metadata.root["strategy"] == "use_capability"
    assert assessment.metadata.root["target"] == "test.action"
    observed = next(event for event in aggregate.events if event.event_type == "agent.observed")
    assert observed.metadata.root["observation_sequence"] == 1
    assert observed.metadata.root["summary_hash"]
    trace = await ExecutionQueryService(factory).trace(result.run_id)
    assert trace.complete
    assert {entry.event_type for entry in trace.audit} >= {
        "agent.sufficiency_assessed",
        "agent.observed",
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


async def test_response_repair_events_are_safe_and_complete() -> None:
    factory = MemoryUnitOfWorkFactory()
    raw_canary = "raw-provider-output-must-not-persist"
    invalid = AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="model response shape is invalid",
            details=SafeMetadata(
                {
                    "diagnostic_reason": "empty_response",
                    "choice_count": 1,
                    "repairable": True,
                    "content_present": False,
                }
            ),
        )
    )
    model = TransactionCheckingModel(factory, [invalid, final_turn()])
    result = await PersistentRuntime(model, CapabilityRegistry(), factory).execute("Repair safely.")
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    aggregate = await load_run(factory, result.run_id)
    events = {event.event_type: event for event in aggregate.events}
    assert {
        "model.response_invalid",
        "model.repair_requested",
        "model.repair_completed",
    } <= set(events)
    assert events["model.response_invalid"].metadata.root["diagnostic_reason"] == "empty_response"
    assert events["model.repair_requested"].metadata.root["repair_attempt"] == 1
    assert events["model.repair_completed"].metadata.root["repair_attempt"] == 1
    assert raw_canary not in "".join(event.model_dump_json() for event in aggregate.events)


async def test_normalized_companion_content_calls_complete_without_repair_or_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    raw_companion = "provider-companion-content-must-not-persist"
    mixed = tool_turn().model_copy(
        update={
            "metadata": SafeMetadata(
                {"response_variant": "content_with_calls", "content_present": True}
            )
        }
    )
    capability = TransactionCheckingCapability(factory, completed_capability())
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [mixed, final_turn()]),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Execute one normalized Tool Call.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert capability.calls == 1
    aggregate = await load_run(factory, result.run_id)
    event_types = [event.event_type for event in aggregate.events]
    assert "model.response_invalid" not in event_types
    assert "model.repair_requested" not in event_types
    completed = next(
        event
        for event in aggregate.events
        if event.event_type == "model.completed"
        and event.metadata.root.get("result_kind") == "tool_calls"
    )
    assert completed.metadata.root["response_variant"] == "content_with_calls"
    assert completed.metadata.root["content_present"] is True
    assert raw_companion not in "".join(event.model_dump_json() for event in aggregate.events)


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


async def test_explainable_capability_failure_remains_failed_while_model_recovers() -> None:
    factory = MemoryUnitOfWorkFactory()
    failure = CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        observation='{"status":"failed","exit_code":1}',
        error=ErrorInfo(
            code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
            message="Capability failed safely",
        ),
    )
    capability = TransactionCheckingCapability(factory, failure)
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [tool_turn(), final_turn("Recovered safely.")]),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Recover from one explainable tool failure.")

    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert capability.calls == 1
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.run.status.value == "succeeded"
    assert aggregate.invocations[0].status.value == "failed"
    assert "capability.failed" in {event.event_type for event in aggregate.events}
    observation = await ExecutionQueryService(factory).trace(result.run_id)
    assert observation.complete is True
    assert observation.inconsistencies == ()


async def test_initial_event_failure_never_calls_model() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "task.created"
    model = TransactionCheckingModel(factory, [final_turn()])

    result = await PersistentRuntime(model, CapabilityRegistry(), factory).execute(
        "Do not execute without identity."
    )

    assert result.persisted is False
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
    assert model.calls == 0


async def test_initial_state_persistence_failure_never_calls_model() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_add_task = True
    model = TransactionCheckingModel(factory, [final_turn()])

    result = await PersistentRuntime(model, CapabilityRegistry(), factory).execute(
        "Do not execute without durable state."
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
    assert result.outcome.error.code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
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
    assert result.outcome.error.code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.run.status.value == "failed"
    assert aggregate.invocations[0].status.value == "failed"
    assert "capability.failed" in {event.event_type for event in aggregate.events}


async def test_chat_uses_one_run_and_one_node_per_bounded_input() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn(), final_turn()])
    session = PersistentRuntime(model, CapabilityRegistry(), factory).chat()

    first = await session.submit("First temporary message.")
    second = await session.submit("Second temporary message.")
    closed = await session.close()

    assert first.run_id == second.run_id
    assert first.task_id == second.task_id
    assert first.node_run_id != second.node_run_id
    assert closed is not None
    assert closed.run_id == first.run_id
    aggregate = await load_run(factory, first.run_id)
    assert aggregate.run.status.value == "succeeded"
    assert aggregate.task.request == "First temporary message."
    assert len(aggregate.nodes) == 2
    assert all(node.status.value == "succeeded" for node in aggregate.nodes)
    assert session.user_input_count == 2
    terminal = next(event for event in aggregate.events if event.event_type == "run.final")
    assert terminal.metadata.root["model_turn_count"] == 2
    assert terminal.metadata.root["capability_call_count"] == 0
    assert terminal.metadata.root["artifact_count"] == 0
    assert "Previous user: First temporary message." in (
        model.requests[1].messages[1].content or ""
    )


async def test_chat_run_terminal_counts_accumulate_capability_calls_across_nodes() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [tool_turn(), final_turn("First."), tool_turn(), final_turn("Second.")],
    )
    capability = TransactionCheckingCapability(factory, completed_capability())
    session = PersistentRuntime(
        model,
        CapabilityRegistry((capability,)),
        factory,
    ).chat()

    first = await session.submit("First action.")
    await session.submit("Second action.")
    await session.close()

    aggregate = await load_run(factory, first.run_id)
    terminal = next(event for event in aggregate.events if event.event_type == "run.final")
    assert terminal.metadata.root["model_turn_count"] == 4
    assert terminal.metadata.root["capability_call_count"] == 2
    assert terminal.metadata.root["artifact_count"] == 0
    assert capability.calls == 2


async def test_chat_limit_closes_without_creating_another_run() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory, [final_turn(f"Answer {index}.") for index in range(8)]
    )
    session = PersistentRuntime(model, CapabilityRegistry(), factory).chat()

    results = [await session.submit(f"Message {index}.") for index in range(8)]
    assert session.can_continue is False
    closed = await session.close()

    assert closed is not None
    assert {result.run_id for result in results} == {closed.run_id}
    aggregate = await load_run(factory, closed.run_id)
    assert len(aggregate.nodes) == 8
    assert aggregate.run.status.value == "succeeded"


async def test_empty_chat_creates_no_persistent_identity() -> None:
    factory = MemoryUnitOfWorkFactory()
    session = PersistentRuntime(
        TransactionCheckingModel(factory, []), CapabilityRegistry(), factory
    ).chat()

    assert await session.close() is None
    assert factory.store.tasks == {}
    assert factory.store.runs == {}


async def test_chat_timeout_and_interruption_match_persisted_terminal_status() -> None:
    for terminate, expected in (("expire", "timed_out"), ("interrupt", "cancelled")):
        factory = MemoryUnitOfWorkFactory()
        session = PersistentRuntime(
            TransactionCheckingModel(factory, [final_turn()]), CapabilityRegistry(), factory
        ).chat()
        submitted = await session.submit("Create one chat node.")

        terminal = await session.expire() if terminate == "expire" else await session.interrupt()

        assert terminal is not None
        assert terminal.outcome.status.value == expected
        aggregate = await load_run(factory, submitted.run_id)
        assert aggregate.run.status.value == expected
        assert aggregate.task.status.value == expected
        assert aggregate.nodes[0].status.value == "succeeded"
        assert "run.error" in {event.event_type for event in aggregate.events}
