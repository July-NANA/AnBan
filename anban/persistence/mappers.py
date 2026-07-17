"""Explicit mappings between Core domain values and SQLAlchemy records."""

from __future__ import annotations

from anban.core.errors import ErrorCode
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    EventId,
    ExecutionRunId,
    GraphRevisionId,
    NodeRunId,
    TaskId,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    Event,
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    NodeRunStatus,
    Task,
    TaskStatus,
)
from anban.persistence.models import (
    ArtifactRecord,
    CapabilityInvocationRecord,
    EventRecord,
    ExecutionRunRecord,
    NodeRunRecord,
    TaskRecord,
)


def error_code(value: str | None) -> ErrorCode | None:
    return None if value is None else ErrorCode(value)


def metadata(value: dict[str, object]) -> SafeMetadata:
    return SafeMetadata.model_validate(value)


def task_record(task: Task) -> TaskRecord:
    return TaskRecord(
        id=task.id,
        request=task.request,
        status=task.status.value,
        error_code=None if task.error_code is None else task.error_code.value,
        created_at=task.created_at,
        safe_metadata=dict(task.metadata.root),
    )


def task_domain(record: TaskRecord) -> Task:
    return Task(
        id=TaskId(record.id),
        request=record.request,
        status=TaskStatus(record.status),
        error_code=error_code(record.error_code),
        created_at=record.created_at,
        metadata=metadata(record.safe_metadata),
    )


def run_record(run: ExecutionRun) -> ExecutionRunRecord:
    return ExecutionRunRecord(
        id=run.id,
        task_id=run.task_id,
        status=run.status.value,
        graph_revision_id=run.graph_revision_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        final_text=run.final_text,
        error_code=None if run.error_code is None else run.error_code.value,
        safe_metadata=dict(run.metadata.root),
    )


def run_domain(record: ExecutionRunRecord) -> ExecutionRun:
    return ExecutionRun(
        id=ExecutionRunId(record.id),
        task_id=TaskId(record.task_id),
        status=ExecutionRunStatus(record.status),
        graph_revision_id=(
            None if record.graph_revision_id is None else GraphRevisionId(record.graph_revision_id)
        ),
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        final_text=record.final_text,
        error_code=error_code(record.error_code),
        metadata=metadata(record.safe_metadata),
    )


def node_record(node: NodeRun) -> NodeRunRecord:
    return NodeRunRecord(
        id=node.id,
        run_id=node.run_id,
        node_name=node.node_name,
        status=node.status.value,
        created_at=node.created_at,
        started_at=node.started_at,
        finished_at=node.finished_at,
        error_code=None if node.error_code is None else node.error_code.value,
        safe_metadata=dict(node.metadata.root),
    )


def node_domain(record: NodeRunRecord) -> NodeRun:
    return NodeRun(
        id=NodeRunId(record.id),
        run_id=ExecutionRunId(record.run_id),
        node_name=record.node_name,
        status=NodeRunStatus(record.status),
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_code=error_code(record.error_code),
        metadata=metadata(record.safe_metadata),
    )


def invocation_record(invocation: CapabilityInvocation) -> CapabilityInvocationRecord:
    return CapabilityInvocationRecord(
        id=invocation.id,
        run_id=invocation.run_id,
        node_run_id=invocation.node_run_id,
        capability_name=invocation.capability_name,
        status=invocation.status.value,
        requested_at=invocation.requested_at,
        started_at=invocation.started_at,
        finished_at=invocation.finished_at,
        error_code=None if invocation.error_code is None else invocation.error_code.value,
        safe_metadata=dict(invocation.metadata.root),
    )


def invocation_domain(record: CapabilityInvocationRecord) -> CapabilityInvocation:
    return CapabilityInvocation(
        id=CapabilityInvocationId(record.id),
        run_id=ExecutionRunId(record.run_id),
        node_run_id=NodeRunId(record.node_run_id),
        capability_name=record.capability_name,
        status=CapabilityInvocationStatus(record.status),
        requested_at=record.requested_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_code=error_code(record.error_code),
        metadata=metadata(record.safe_metadata),
    )


def artifact_record(artifact: Artifact) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact.id,
        run_id=artifact.run_id,
        node_run_id=artifact.node_run_id,
        invocation_id=artifact.invocation_id,
        uri=artifact.uri,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        created_at=artifact.created_at,
        safe_metadata=dict(artifact.metadata.root),
    )


def artifact_domain(record: ArtifactRecord) -> Artifact:
    return Artifact(
        id=ArtifactId(record.id),
        run_id=ExecutionRunId(record.run_id),
        node_run_id=None if record.node_run_id is None else NodeRunId(record.node_run_id),
        invocation_id=(
            None if record.invocation_id is None else CapabilityInvocationId(record.invocation_id)
        ),
        uri=record.uri,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
        media_type=record.media_type,
        created_at=record.created_at,
        metadata=metadata(record.safe_metadata),
    )


def event_record(event: Event) -> EventRecord:
    return EventRecord(
        id=event.id,
        run_id=event.run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        node_run_id=event.node_run_id,
        invocation_id=event.invocation_id,
        artifact_id=event.artifact_id,
        safe_metadata=dict(event.metadata.root),
    )


def event_domain(record: EventRecord) -> Event:
    return Event(
        id=EventId(record.id),
        run_id=ExecutionRunId(record.run_id),
        sequence=record.sequence,
        event_type=record.event_type,
        occurred_at=record.occurred_at,
        node_run_id=None if record.node_run_id is None else NodeRunId(record.node_run_id),
        invocation_id=(
            None if record.invocation_id is None else CapabilityInvocationId(record.invocation_id)
        ),
        artifact_id=None if record.artifact_id is None else ArtifactId(record.artifact_id),
        metadata=metadata(record.safe_metadata),
    )
