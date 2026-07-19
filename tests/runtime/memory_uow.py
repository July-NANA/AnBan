"""Transactional in-memory persistence fixture shared by Runtime tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from anban.core.context import ContextEntry, ContextScope, ContextSummary
from anban.core.graph import GraphRevision
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    CheckpointId,
    ContextEntryId,
    ContextSummaryId,
    EventId,
    ExecutionRunId,
    GraphRevisionId,
    InteractionId,
    NodeRunId,
    ScheduleId,
    ScheduleOccurrenceId,
    SessionId,
    TaskId,
)
from anban.core.inbox import (
    InteractionInboxDisposition,
    InteractionInboxEntry,
    InteractionInboxStatus,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    Checkpoint,
    Event,
    ExecutionRun,
    NodeRun,
    Task,
    UtcDateTime,
)
from anban.core.persistence import ExecutionRunAggregate
from anban.core.schedule import (
    ScheduleDefinition,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
)


@dataclass
class MemoryStore:
    tasks: dict[TaskId, Task] = field(default_factory=lambda: dict[TaskId, Task]())
    runs: dict[ExecutionRunId, ExecutionRun] = field(
        default_factory=lambda: dict[ExecutionRunId, ExecutionRun]()
    )
    graph_revisions: dict[GraphRevisionId, GraphRevision] = field(
        default_factory=lambda: dict[GraphRevisionId, GraphRevision]()
    )
    nodes: dict[NodeRunId, NodeRun] = field(default_factory=lambda: dict[NodeRunId, NodeRun]())
    invocations: dict[CapabilityInvocationId, CapabilityInvocation] = field(
        default_factory=lambda: dict[CapabilityInvocationId, CapabilityInvocation]()
    )
    checkpoints: dict[CheckpointId, Checkpoint] = field(
        default_factory=lambda: dict[CheckpointId, Checkpoint]()
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
    inbox: dict[InteractionId, InteractionInboxEntry] = field(
        default_factory=lambda: dict[InteractionId, InteractionInboxEntry]()
    )
    schedules: dict[ScheduleId, ScheduleDefinition] = field(
        default_factory=lambda: dict[ScheduleId, ScheduleDefinition]()
    )
    schedule_occurrences: dict[ScheduleOccurrenceId, ScheduleOccurrence] = field(
        default_factory=lambda: dict[ScheduleOccurrenceId, ScheduleOccurrence]()
    )

    def copy(self) -> MemoryStore:
        return MemoryStore(
            tasks=dict(self.tasks),
            runs=dict(self.runs),
            graph_revisions=dict(self.graph_revisions),
            nodes=dict(self.nodes),
            invocations=dict(self.invocations),
            checkpoints=dict(self.checkpoints),
            artifacts=dict(self.artifacts),
            events=dict(self.events),
            context_entries=dict(self.context_entries),
            context_summaries=dict(self.context_summaries),
            inbox=dict(self.inbox),
            schedules=dict(self.schedules),
            schedule_occurrences=dict(self.schedule_occurrences),
        )


class MemoryRepository:
    def __init__(self, store: MemoryStore, factory: MemoryUnitOfWorkFactory) -> None:
        self.store = store
        self.factory = factory

    async def add_schedule(self, schedule: ScheduleDefinition) -> None:
        if schedule.id in self.store.schedules or any(
            item.name == schedule.name for item in self.store.schedules.values()
        ):
            raise RuntimeError("test-only duplicate schedule")
        self.store.schedules[schedule.id] = schedule

    async def get_schedule(self, schedule_id: ScheduleId) -> ScheduleDefinition | None:
        return self.store.schedules.get(schedule_id)

    async def list_schedules(self, limit: int) -> tuple[ScheduleDefinition, ...]:
        return tuple(
            sorted(
                self.store.schedules.values(),
                key=lambda schedule: (schedule.created_at, schedule.id),
                reverse=True,
            )[:limit]
        )

    async def add_schedule_occurrence(
        self, occurrence: ScheduleOccurrence
    ) -> tuple[ScheduleOccurrence, bool]:
        existing = next(
            (
                item
                for item in self.store.schedule_occurrences.values()
                if item.schedule_id == occurrence.schedule_id
                and item.scheduled_for == occurrence.scheduled_for
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.store.schedule_occurrences[occurrence.id] = occurrence
        return occurrence, True

    async def claim_schedule_occurrence(
        self, occurrence: ScheduleOccurrence
    ) -> tuple[ScheduleOccurrence, bool]:
        exact, inserted = await self.add_schedule_occurrence(occurrence)
        if not inserted:
            if (
                exact.status is ScheduleOccurrenceStatus.CLAIMED
                and exact.lease_until <= occurrence.claimed_at
            ):
                recovered = exact.model_copy(
                    update={
                        "claimed_at": occurrence.claimed_at,
                        "lease_until": occurrence.lease_until,
                        "attempt_count": exact.attempt_count + 1,
                    }
                )
                self.store.schedule_occurrences[exact.id] = recovered
                return recovered, True
            return exact, False
        active = next(
            (
                item
                for item in self.store.schedule_occurrences.values()
                if item.id != occurrence.id
                and item.schedule_id == occurrence.schedule_id
                and item.status is ScheduleOccurrenceStatus.CLAIMED
            ),
            None,
        )
        if active is not None:
            self.store.schedule_occurrences.pop(occurrence.id)
            skipped = occurrence.model_copy(
                update={
                    "status": ScheduleOccurrenceStatus.SKIPPED,
                    "finished_at": occurrence.claimed_at,
                }
            )
            self.store.schedule_occurrences[skipped.id] = skipped
            return skipped, False
        return occurrence, True

    async def get_schedule_occurrence(
        self, occurrence_id: ScheduleOccurrenceId
    ) -> ScheduleOccurrence | None:
        return self.store.schedule_occurrences.get(occurrence_id)

    async def list_schedule_occurrences(
        self, schedule_id: ScheduleId, limit: int
    ) -> tuple[ScheduleOccurrence, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.store.schedule_occurrences.values()
                    if item.schedule_id == schedule_id
                ),
                key=lambda item: (item.scheduled_for, item.id),
                reverse=True,
            )[:limit]
        )

    async def update_schedule_occurrence(self, occurrence: ScheduleOccurrence) -> None:
        existing = self.store.schedule_occurrences.get(occurrence.id)
        if existing is None or existing.status is not ScheduleOccurrenceStatus.CLAIMED:
            raise RuntimeError("test-only invalid Schedule occurrence update")
        self.store.schedule_occurrences[occurrence.id] = occurrence

    async def receive_inbox(
        self, entry: InteractionInboxEntry
    ) -> tuple[InteractionInboxEntry, bool]:
        existing = self.store.inbox.get(entry.interaction_id)
        if existing is None and entry.deduplication_correlation_hash is not None:
            matches = tuple(
                candidate
                for candidate in self.store.inbox.values()
                if candidate.deduplication_namespace == entry.deduplication_namespace
                and candidate.deduplication_correlation_hash == entry.deduplication_correlation_hash
            )
            if len(matches) > 1:
                raise RuntimeError("test-only ambiguous inbox deduplication")
            existing = None if not matches else matches[0]
        if existing is None:
            self.store.inbox[entry.interaction_id] = entry
            return entry, True
        same_semantics = existing.semantic_hash == entry.semantic_hash
        updated = existing.model_copy(
            update={
                "delivery_count": existing.delivery_count + 1,
                "last_received_at": max(existing.last_received_at, entry.received_at),
                "last_disposition": (
                    InteractionInboxDisposition.DEDUPLICATED
                    if same_semantics
                    else InteractionInboxDisposition.CONFLICTING
                ),
            }
        )
        self.store.inbox[existing.interaction_id] = updated
        return updated, False

    async def get_inbox(self, interaction_id: InteractionId) -> InteractionInboxEntry | None:
        return self.store.inbox.get(interaction_id)

    async def reclaim_inbox(
        self,
        interaction_id: InteractionId,
        claimed_at: UtcDateTime,
        stale_before: UtcDateTime,
    ) -> InteractionInboxEntry | None:
        existing = self.store.inbox.get(interaction_id)
        if (
            existing is None
            or existing.status is not InteractionInboxStatus.PROCESSING
            or existing.claimed_at > stale_before
            or existing.run_id is not None
        ):
            return None
        updated = existing.model_copy(
            update={
                "claimed_at": claimed_at,
                "last_disposition": InteractionInboxDisposition.ACCEPTED,
            }
        )
        self.store.inbox[interaction_id] = updated
        return updated

    async def route_inbox(
        self,
        interaction_id: InteractionId,
        task_id: TaskId,
        run_id: ExecutionRunId,
        node_run_id: NodeRunId,
    ) -> None:
        existing = self.store.inbox.get(interaction_id)
        if existing is None:
            raise RuntimeError("test-only missing inbox entry")
        identities = (existing.task_id, existing.run_id, existing.node_run_id)
        requested = (task_id, run_id, node_run_id)
        if existing.status is InteractionInboxStatus.ROUTED and identities == requested:
            return
        if existing.status is not InteractionInboxStatus.PROCESSING or any(
            identity is not None for identity in identities
        ):
            raise RuntimeError("test-only invalid inbox route")
        self.store.inbox[interaction_id] = existing.model_copy(
            update={
                "status": InteractionInboxStatus.ROUTED,
                "task_id": task_id,
                "run_id": run_id,
                "node_run_id": node_run_id,
            }
        )

    async def update_inbox(self, entry: InteractionInboxEntry) -> None:
        if entry.interaction_id not in self.store.inbox:
            raise RuntimeError("test-only missing inbox entry")
        self.store.inbox[entry.interaction_id] = entry

    async def list_inbox(self, limit: int) -> tuple[InteractionInboxEntry, ...]:
        return tuple(
            sorted(
                self.store.inbox.values(),
                key=lambda entry: (entry.received_at, entry.interaction_id),
                reverse=True,
            )[:limit]
        )

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

    async def set_run_graph_revision(
        self,
        run_id: ExecutionRunId,
        expected_revision_id: GraphRevisionId | None,
        revision_id: GraphRevisionId,
    ) -> None:
        run = self.store.runs[run_id]
        if run.graph_revision_id != expected_revision_id:
            raise RuntimeError("test-only Run Graph revision conflict")
        revision = self.store.graph_revisions[revision_id]
        if revision.task_id != run.task_id:
            raise RuntimeError("test-only Run Graph revision Task mismatch")
        self.store.runs[run_id] = run.model_copy(update={"graph_revision_id": revision_id})

    async def add_graph_revision(self, revision: GraphRevision) -> None:
        if revision.id in self.store.graph_revisions:
            raise RuntimeError("test-only duplicate Graph revision")
        current = await self.get_current_graph_revision(revision.task_id)
        if (current is None) != (revision.previous_revision_id is None):
            raise RuntimeError("test-only invalid Graph revision predecessor")
        if current is not None and revision.previous_revision_id != current.id:
            raise RuntimeError("test-only stale Graph revision predecessor")
        self.store.graph_revisions[revision.id] = revision

    async def get_graph_revision(self, revision_id: GraphRevisionId) -> GraphRevision | None:
        return self.store.graph_revisions.get(revision_id)

    async def list_graph_revisions(self, task_id: TaskId) -> tuple[GraphRevision, ...]:
        return tuple(
            sorted(
                (
                    revision
                    for revision in self.store.graph_revisions.values()
                    if revision.task_id == task_id
                ),
                key=lambda revision: (revision.created_at, revision.id),
            )
        )

    async def get_current_graph_revision(self, task_id: TaskId) -> GraphRevision | None:
        revisions = await self.list_graph_revisions(task_id)
        predecessors = {
            revision.previous_revision_id
            for revision in revisions
            if revision.previous_revision_id is not None
        }
        current = tuple(revision for revision in revisions if revision.id not in predecessors)
        if len(current) > 1:
            raise RuntimeError("test-only branched Graph revision history")
        return None if not current else current[0]

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

    async def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.id in self.store.checkpoints:
            raise RuntimeError("test-only duplicate Checkpoint")
        self.store.checkpoints[checkpoint.id] = checkpoint

    async def get_checkpoint(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        return self.store.checkpoints.get(checkpoint_id)

    async def update_checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.id not in self.store.checkpoints:
            raise RuntimeError("test-only missing Checkpoint")
        self.store.checkpoints[checkpoint.id] = checkpoint

    async def list_checkpoints(self, run_id: ExecutionRunId) -> tuple[Checkpoint, ...]:
        return tuple(
            sorted(
                (
                    checkpoint
                    for checkpoint in self.store.checkpoints.values()
                    if checkpoint.run_id == run_id
                ),
                key=lambda checkpoint: (checkpoint.created_at, checkpoint.id),
            )
        )

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

    async def find_event(self, event_type: str, metadata_match: SafeMetadata) -> Event | None:
        matches = tuple(
            event
            for event in self.store.events.values()
            if event.event_type == event_type
            and all(
                event.metadata.root.get(key) == value for key, value in metadata_match.root.items()
            )
        )
        if len(matches) > 1:
            raise RuntimeError("test-only ambiguous Event metadata lookup")
        return None if not matches else matches[0]

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
            graph_revision=(
                None
                if run.graph_revision_id is None
                else self.store.graph_revisions[run.graph_revision_id]
            ),
            nodes=tuple(node for node in self.store.nodes.values() if node.run_id == run_id),
            invocations=tuple(
                invocation
                for invocation in self.store.invocations.values()
                if invocation.run_id == run_id
            ),
            checkpoints=await self.list_checkpoints(run_id),
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
