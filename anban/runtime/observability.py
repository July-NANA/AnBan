"""Safe Audit and Trace projections over the authoritative Event stream."""

from __future__ import annotations

from pydantic import Field

from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ExecutionRunId,
    NodeRunId,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import Event, UtcDateTime
from anban.core.persistence import ExecutionRunAggregate
from anban.runtime.contracts import RuntimeValue

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
_AUDIT_EVENT_PREFIXES = (
    "model.",
    "skill.",
    "capability.",
    "artifact.",
    "run.final",
    "run.error",
)
_EVENT_METADATA_ALLOWLIST = frozenset(
    {
        "argument_count",
        "arguments_hash",
        "artifact_count",
        "arguments_type",
        "capability_call_count",
        "capability_name",
        "choice_count",
        "command",
        "content_empty",
        "content_present",
        "content_type",
        "content_hash",
        "cwd_scope",
        "entry_count",
        "diagnostic_reason",
        "duration_ms",
        "error_category",
        "error_code",
        "exit_code",
        "finish_reason",
        "function_name_present",
        "input_tokens",
        "method",
        "media_type",
        "message_role",
        "model",
        "model_turn_count",
        "omitted_line_count",
        "output_tokens",
        "provider",
        "repair_attempt",
        "repair_attempts_exhausted",
        "repairable",
        "response_variant",
        "result_kind",
        "size_bytes",
        "status_code",
        "stderr_hash",
        "stderr_size",
        "skill_slug",
        "skill_root",
        "skill_version",
        "stdout_hash",
        "stdout_size",
        "timed_out",
        "cancelled",
        "tool_call_count",
        "tool_call_id_present",
        "tool_call_type",
        "tool_calls_present",
        "transport_retry_count",
        "transport_retry_limit",
        "turn_number",
    }
)


class TraceEntry(RuntimeValue):
    sequence: int = Field(ge=1)
    event_type: str
    occurred_at: UtcDateTime
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    artifact_id: ArtifactId | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class AuditEntry(TraceEntry):
    """Security-relevant Trace entry selected from the same Event fact."""


class RunObservability(RuntimeValue):
    run_id: ExecutionRunId
    trace: tuple[TraceEntry, ...]
    audit: tuple[AuditEntry, ...]
    complete: bool
    inconsistencies: tuple[str, ...]


def project_observability(aggregate: ExecutionRunAggregate) -> RunObservability:
    ordered = tuple(sorted(aggregate.events, key=lambda event: (event.sequence, event.id)))
    trace = tuple(trace_entry(event) for event in ordered)
    audit = tuple(
        AuditEntry.model_validate(entry.model_dump())
        for event, entry in zip(ordered, trace, strict=True)
        if event.event_type.startswith(_AUDIT_EVENT_PREFIXES)
    )
    inconsistencies = inspect_consistency(aggregate, ordered)
    return RunObservability(
        run_id=aggregate.run.id,
        trace=trace,
        audit=audit,
        complete=not inconsistencies,
        inconsistencies=inconsistencies,
    )


def trace_entry(event: Event) -> TraceEntry:
    return TraceEntry(
        sequence=event.sequence,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        node_run_id=event.node_run_id,
        invocation_id=event.invocation_id,
        artifact_id=event.artifact_id,
        metadata=SafeMetadata(
            {
                key: value
                for key, value in event.metadata.root.items()
                if key in _EVENT_METADATA_ALLOWLIST
            }
        ),
    )


def inspect_consistency(
    aggregate: ExecutionRunAggregate, events: tuple[Event, ...]
) -> tuple[str, ...]:
    issues: set[str] = set()
    expected = tuple(range(1, len(events) + 1))
    if tuple(event.sequence for event in events) != expected:
        issues.add("event_sequence_invalid")
    if any(event.run_id != aggregate.run.id for event in events):
        issues.add("event_run_mismatch")

    nodes = {node.id: node for node in aggregate.nodes}
    invocations = {invocation.id: invocation for invocation in aggregate.invocations}
    artifacts = {artifact.id: artifact for artifact in aggregate.artifacts}
    if any(node.run_id != aggregate.run.id for node in nodes.values()):
        issues.add("node_run_mismatch")
    if any(
        invocation.run_id != aggregate.run.id or invocation.node_run_id not in nodes
        for invocation in invocations.values()
    ):
        issues.add("invocation_correlation_invalid")
    if any(
        artifact.run_id != aggregate.run.id
        or artifact.node_run_id is not None
        and artifact.node_run_id not in nodes
        or artifact.invocation_id is not None
        and artifact.invocation_id not in invocations
        for artifact in artifacts.values()
    ):
        issues.add("artifact_correlation_invalid")
    for event in events:
        if event.event_type.startswith(("node.", "model.")) and event.node_run_id is None:
            issues.add("event_node_missing")
        if event.event_type.startswith(("capability.", "skill.")) and event.invocation_id is None:
            issues.add("event_invocation_missing")
        if event.event_type == "artifact.created" and event.artifact_id is None:
            issues.add("event_artifact_missing")
        if event.node_run_id is not None and event.node_run_id not in nodes:
            issues.add("event_node_missing")
        if event.invocation_id is not None:
            invocation = invocations.get(event.invocation_id)
            if invocation is None:
                issues.add("event_invocation_missing")
            elif (
                invocation.run_id != aggregate.run.id
                or event.node_run_id is not None
                and invocation.node_run_id != event.node_run_id
            ):
                issues.add("event_invocation_mismatch")
        if event.artifact_id is not None:
            artifact = artifacts.get(event.artifact_id)
            if artifact is None:
                issues.add("event_artifact_missing")
            elif artifact.run_id != aggregate.run.id:
                issues.add("event_artifact_mismatch")

    if aggregate.task.status.value not in _TERMINAL:
        issues.add("task_incomplete")
    if aggregate.run.status.value not in _TERMINAL:
        issues.add("run_incomplete")
    if not aggregate.nodes or any(node.status.value not in _TERMINAL for node in aggregate.nodes):
        issues.add("node_incomplete")
    if any(invocation.status.value not in _TERMINAL for invocation in aggregate.invocations):
        issues.add("invocation_incomplete")
    final_type = "run.final" if aggregate.run.status.value == "succeeded" else "run.error"
    if not any(event.event_type == final_type for event in events):
        issues.add("terminal_event_missing")
    return tuple(sorted(issues))
