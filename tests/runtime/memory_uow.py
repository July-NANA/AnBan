"""Transactional in-memory persistence fixture shared by Runtime tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from anban.core.context import ContextEntry, ContextScope, ContextSummary
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ContextEntryId,
    ContextSummaryId,
    EventId,
    ExecutionRunId,
    NodeRunId,
    SessionId,
    TaskId,
)
from anban.core.models import Artifact, CapabilityInvocation, Event, ExecutionRun, NodeRun, Task
from anban.core.persistence import ExecutionRunAggregate


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
    context_entries: dict[ContextEntryId, ContextEntry] = field(
        default_factory=lambda: dict[ContextEntryId, ContextEntry]()
    )
    context_summaries: dict[ContextSummaryId, ContextSummary] = field(
        default_factory=lambda: dict[ContextSummaryId, ContextSummary]()
    )

    def copy(self) -> MemoryStore:
        return MemoryStore(
            tasks=dict(self.tasks),
            runs=dict(self.runs),
            nodes=dict(self.nodes),
            invocations=dict(self.invocations),
            artifacts=dict(self.artifacts),
            events=dict(self.events),
            context_entries=dict(self.context_entries),
            context_summaries=dict(self.context_summaries),
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

    async def add_context_entry(self, entry: ContextEntry) -> None:
        self.store.context_entries[entry.id] = entry

    async def get_context_entry(self, entry_id: ContextEntryId) -> ContextEntry | None:
        return self.store.context_entries.get(entry_id)

    async def update_context_entry(self, entry: ContextEntry) -> None:
        if entry.id not in self.store.context_entries:
            raise RuntimeError("test-only missing Context entry")
        self.store.context_entries[entry.id] = entry

    async def add_context_summary(self, summary: ContextSummary) -> None:
        self.store.context_summaries[summary.id] = summary

    async def list_context_entries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextEntry, ...]:
        return tuple(
            sorted(
                (
                    entry
                    for entry in self.store.context_entries.values()
                    if entry.scope is scope
                    and (entry.task_id if scope is ContextScope.TASK else entry.session_id)
                    == identity
                ),
                key=lambda entry: (entry.created_at, entry.id),
            )
        )

    async def list_context_summaries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextSummary, ...]:
        return tuple(
            sorted(
                (
                    summary
                    for summary in self.store.context_summaries.values()
                    if summary.scope is scope
                    and (summary.task_id if scope is ContextScope.TASK else summary.session_id)
                    == identity
                ),
                key=lambda summary: (summary.created_at, summary.id),
            )
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
