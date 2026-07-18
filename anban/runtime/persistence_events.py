"""Event facts and pure transition preparation for Runtime persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from anban.core.graph import GraphRevision
from anban.core.ids import ArtifactId, CapabilityInvocationId, CheckpointId, NodeRunId
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRun, Task
from anban.core.persistence import ExecutionRepository


@dataclass(frozen=True)
class EventFact:
    event_type: str
    metadata: SafeMetadata = field(default_factory=SafeMetadata)
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    artifact_id: ArtifactId | None = None
    checkpoint_id: CheckpointId | None = None


@dataclass(frozen=True)
class TaskRouteTransition:
    run: ExecutionRun
    operation: Callable[[ExecutionRepository], Awaitable[None]]
    facts: tuple[EventFact, ...]


def task_route_transition(
    task: Task,
    run: ExecutionRun,
    node_run_id: NodeRunId,
    route: str,
    rationale_hash: str,
    revision: GraphRevision | None,
) -> TaskRouteTransition:
    if revision is not None and (revision.task_id != task.id or run.graph_revision_id is not None):
        raise ValueError("Graph revision cannot be attached to this Run")
    updated_run = run.model_copy(
        update={"graph_revision_id": run.graph_revision_id if revision is None else revision.id}
    )

    async def operation(repository: ExecutionRepository) -> None:
        if revision is not None:
            await repository.add_graph_revision(revision)
            await repository.set_run_graph_revision(run.id, None, revision.id)

    metadata = SafeMetadata(
        {
            "route": route,
            "graph_selected": revision is not None,
            "graph_revision_id": None if revision is None else str(revision.id),
            "graph_spec_hash": None if revision is None else revision.spec_hash,
            "graph_node_count": None if revision is None else len(revision.spec.nodes),
            "rationale_hash": rationale_hash,
        }
    )
    facts = [EventFact("agent.route_selected", metadata, node_run_id=node_run_id)]
    if revision is not None:
        facts.extend(
            (
                EventFact("graph.revision_created", metadata, node_run_id=node_run_id),
                EventFact("run.graph_revision_linked", metadata, node_run_id=node_run_id),
            )
        )
    return TaskRouteTransition(updated_run, operation, tuple(facts))
