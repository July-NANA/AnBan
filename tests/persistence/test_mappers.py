"""Deterministic domain/SQLAlchemy mapping tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anban.core import (
    Artifact,
    CapabilityInvocation,
    ContextEntry,
    ContextEntryKind,
    ContextScope,
    ContextSource,
    ContextSourceKind,
    ContextSummary,
    Event,
    ExecutionRun,
    NodeRun,
    SafeMetadata,
    Task,
    new_artifact_id,
    new_capability_invocation_id,
    new_context_entry_id,
    new_context_summary_id,
    new_event_id,
    new_execution_run_id,
    new_node_run_id,
    new_task_id,
)
from anban.persistence.mappers import (
    artifact_domain,
    artifact_record,
    context_coverage_records,
    context_entry_domain,
    context_entry_record,
    context_summary_domain,
    context_summary_record,
    event_domain,
    event_record,
    invocation_domain,
    invocation_record,
    node_domain,
    node_record,
    run_domain,
    run_record,
    task_domain,
    task_record,
)


def domain_records() -> tuple[
    Task,
    ExecutionRun,
    NodeRun,
    CapabilityInvocation,
    Artifact,
    Event,
]:
    task = Task(
        id=new_task_id(),
        request="map records",
        metadata=SafeMetadata({"source": "test"}),
    )
    run = ExecutionRun(id=new_execution_run_id(), task_id=task.id)
    node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
    invocation = CapabilityInvocation(
        id=new_capability_invocation_id(),
        run_id=run.id,
        node_run_id=node.id,
        capability_name="process.execute",
    )
    artifact = Artifact(
        id=new_artifact_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        uri=f"anban://artifact/{run.id}/result",
        sha256="a" * 64,
        size_bytes=1,
        media_type="text/plain",
    )
    event = Event(
        id=new_event_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        artifact_id=artifact.id,
        sequence=1,
        event_type="artifact.created",
    )
    return task, run, node, invocation, artifact, event


def test_every_domain_record_round_trips_through_storage_model() -> None:
    task, run, node, invocation, artifact, event = domain_records()
    assert task_domain(task_record(task)) == task
    assert run_domain(run_record(run)) == run
    assert node_domain(node_record(node)) == node
    assert invocation_domain(invocation_record(invocation)) == invocation
    assert artifact_domain(artifact_record(artifact)) == artifact
    assert event_domain(event_record(event)) == event


def test_context_records_round_trip_with_ordered_summary_coverage() -> None:
    task_id = new_task_id()
    entries = tuple(
        ContextEntry(
            id=new_context_entry_id(),
            scope=ContextScope.TASK,
            task_id=task_id,
            kind=ContextEntryKind.USER_FACT,
            content=content,
            source=ContextSource(
                kind=ContextSourceKind.INTERACTION,
                reference=f"interaction:{index}",
            ),
        )
        for index, content in enumerate(("First durable fact.", "Second durable fact."), start=1)
    )
    summary = ContextSummary(
        id=new_context_summary_id(),
        scope=ContextScope.TASK,
        task_id=task_id,
        covered_entry_ids=tuple(entry.id for entry in entries),
        content="Both durable facts remain covered in source order.",
    )

    assert tuple(context_entry_domain(context_entry_record(entry)) for entry in entries) == entries
    coverage = context_coverage_records(summary)
    assert tuple(item.ordinal for item in coverage) == (1, 2)
    assert tuple(item.entry_id for item in coverage) == summary.covered_entry_ids
    assert (
        context_summary_domain(context_summary_record(summary), summary.covered_entry_ids)
        == summary
    )


def test_unsafe_persisted_metadata_fails_closed() -> None:
    task, *_ = domain_records()
    record = task_record(task)
    record.safe_metadata = {"api_key": "canary"}
    with pytest.raises(ValidationError):
        task_domain(record)
