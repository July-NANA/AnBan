"""Persistence Ports owned by Core and implemented by storage adapters."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

from anban.core.context import ContextEntry, ContextScope, ContextSummary
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ContextEntryId,
    EventId,
    ExecutionRunId,
    NodeRunId,
    SessionId,
    TaskId,
)
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    Event,
    ExecutionRun,
    NodeRun,
    Task,
)


@dataclass(frozen=True)
class ExecutionRunAggregate:
    """A deterministic reconstruction of one Run and its persisted records."""

    task: Task
    run: ExecutionRun
    nodes: tuple[NodeRun, ...]
    invocations: tuple[CapabilityInvocation, ...]
    artifacts: tuple[Artifact, ...]
    events: tuple[Event, ...]


class ExecutionRepository(Protocol):
    """Focused persistence operations needed by the v0.1 Runtime."""

    async def add_task(self, task: Task) -> None: ...

    async def get_task(self, task_id: TaskId) -> Task | None: ...

    async def update_task(self, task: Task) -> None: ...

    async def add_run(self, run: ExecutionRun) -> None: ...

    async def get_run(self, run_id: ExecutionRunId) -> ExecutionRun | None: ...

    async def update_run(self, run: ExecutionRun) -> None: ...

    async def add_node_run(self, node_run: NodeRun) -> None: ...

    async def update_node_run(self, node_run: NodeRun) -> None: ...

    async def add_invocation(self, invocation: CapabilityInvocation) -> None: ...

    async def update_invocation(self, invocation: CapabilityInvocation) -> None: ...

    async def add_artifact(self, artifact: Artifact) -> None: ...

    async def get_artifact(self, artifact_id: ArtifactId) -> Artifact | None: ...

    async def add_event(self, event: Event) -> None: ...

    async def get_event(self, event_id: EventId) -> Event | None: ...

    async def get_node_run(self, node_run_id: NodeRunId) -> NodeRun | None: ...

    async def get_invocation(
        self, invocation_id: CapabilityInvocationId
    ) -> CapabilityInvocation | None: ...

    async def load_run(self, run_id: ExecutionRunId) -> ExecutionRunAggregate | None: ...

    async def list_runs(self, limit: int) -> tuple[ExecutionRun, ...]: ...

    async def list_events(self, run_id: ExecutionRunId) -> tuple[Event, ...]: ...

    async def add_context_entry(self, entry: ContextEntry) -> None: ...

    async def get_context_entry(self, entry_id: ContextEntryId) -> ContextEntry | None: ...

    async def update_context_entry(self, entry: ContextEntry) -> None: ...

    async def add_context_summary(self, summary: ContextSummary) -> None: ...

    async def list_context_entries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextEntry, ...]: ...

    async def list_context_summaries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextSummary, ...]: ...


class UnitOfWork(Protocol):
    """One short database transaction; external calls must occur outside it."""

    @property
    def executions(self) -> ExecutionRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """Creates a fresh transaction boundary for each Runtime persistence step."""

    def __call__(self) -> UnitOfWork: ...
