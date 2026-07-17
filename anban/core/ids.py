"""Strongly typed identifiers for the execution domain."""

from __future__ import annotations

from typing import NewType
from uuid import UUID, uuid4

InteractionId = NewType("InteractionId", UUID)
TaskId = NewType("TaskId", UUID)
ExecutionRunId = NewType("ExecutionRunId", UUID)
NodeRunId = NewType("NodeRunId", UUID)
CapabilityInvocationId = NewType("CapabilityInvocationId", UUID)
ArtifactId = NewType("ArtifactId", UUID)
EventId = NewType("EventId", UUID)
GraphRevisionId = NewType("GraphRevisionId", UUID)


def new_interaction_id() -> InteractionId:
    return InteractionId(uuid4())


def new_task_id() -> TaskId:
    return TaskId(uuid4())


def new_execution_run_id() -> ExecutionRunId:
    return ExecutionRunId(uuid4())


def new_node_run_id() -> NodeRunId:
    return NodeRunId(uuid4())


def new_capability_invocation_id() -> CapabilityInvocationId:
    return CapabilityInvocationId(uuid4())


def new_artifact_id() -> ArtifactId:
    return ArtifactId(uuid4())


def new_event_id() -> EventId:
    return EventId(uuid4())


def new_graph_revision_id() -> GraphRevisionId:
    return GraphRevisionId(uuid4())
