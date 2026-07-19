"""Reconstruct one terminal execution result from authoritative persisted facts."""

from __future__ import annotations

from anban.capability import ArtifactReference
from anban.core.errors import ErrorCode, ErrorInfo
from anban.core.ids import ExecutionRunId, NodeRunId, TaskId
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRunStatus
from anban.core.persistence import ExecutionRunAggregate
from anban.runtime.contracts import AgentOutcome, AgentOutcomeStatus, ExecutionResult


def reconstruct_terminal_result(
    aggregate: ExecutionRunAggregate,
    task_id: TaskId,
    run_id: ExecutionRunId,
    node_run_id: NodeRunId,
) -> ExecutionResult | None:
    """Return a safe terminal projection without invoking Model or Capability again."""

    if aggregate.run.status in {
        ExecutionRunStatus.CREATED,
        ExecutionRunStatus.RUNNING,
    }:
        return None
    status = AgentOutcomeStatus(aggregate.run.status.value)
    terminal = next(
        (
            event
            for event in reversed(aggregate.events)
            if event.event_type in {"run.final", "run.error"}
        ),
        None,
    )
    if terminal is None:
        return None
    model_turns = terminal.metadata.root.get("model_turn_count")
    capability_calls = terminal.metadata.root.get("capability_call_count")
    if type(model_turns) is not int or type(capability_calls) is not int:
        return None
    error = None
    if status is not AgentOutcomeStatus.SUCCEEDED:
        error = ErrorInfo(
            code=aggregate.run.error_code or ErrorCode.VALIDATION_FAILED,
            message="Persisted Interaction execution did not succeed",
            details=SafeMetadata({"reason": "deduplicated"}),
        )
    if status is AgentOutcomeStatus.SUCCEEDED and aggregate.run.final_text is None:
        return None
    artifacts = tuple(
        ArtifactReference(
            id=artifact.id,
            uri=artifact.uri,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        for artifact in aggregate.artifacts
    )
    return ExecutionResult(
        task_id=task_id,
        run_id=run_id,
        node_run_id=node_run_id,
        outcome=AgentOutcome(
            status=status,
            final_text=aggregate.run.final_text,
            error=error,
            model_turn_count=model_turns,
            capability_call_count=capability_calls,
            artifacts=artifacts,
        ),
        persisted=True,
    )
