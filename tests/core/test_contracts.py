"""Serialization and invariant tests for v0.1 Core contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import assert_type

import pytest
from pydantic import ValidationError

from anban.core import (
    Artifact,
    CapabilityInvocation,
    Checkpoint,
    CheckpointStatus,
    Event,
    ExecutionRun,
    ExecutionRunId,
    NodeRun,
    SafeMetadata,
    Task,
    TaskId,
    new_artifact_id,
    new_capability_invocation_id,
    new_checkpoint_id,
    new_event_id,
    new_execution_run_id,
    new_graph_revision_id,
    new_node_run_id,
    new_task_id,
)
from anban.core.ids import new_interaction_id
from anban.interaction import InteractionEnvelope


def domain_records() -> tuple[
    Task,
    ExecutionRun,
    NodeRun,
    CapabilityInvocation,
    Artifact,
    Event,
]:
    task = Task(id=new_task_id(), request="Write a bounded validation artifact.")
    run = ExecutionRun(
        id=new_execution_run_id(),
        task_id=task.id,
        graph_revision_id=new_graph_revision_id(),
    )
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
        uri=f"anban://artifact/{run.id}/{new_artifact_id()}",
        sha256="a" * 64,
        size_bytes=10,
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
        metadata=SafeMetadata({"media_type": "text/plain"}),
    )
    return task, run, node, invocation, artifact, event


def test_ids_remain_distinct_for_static_typing() -> None:
    task_id = new_task_id()
    run_id = new_execution_run_id()

    assert_type(task_id, TaskId)
    assert_type(run_id, ExecutionRunId)
    assert task_id != run_id


def test_domain_graph_serializes_and_restores_relationships() -> None:
    task, run, node, invocation, artifact, event = domain_records()

    assert Task.model_validate_json(task.model_dump_json()) == task
    assert ExecutionRun.model_validate_json(run.model_dump_json()) == run
    assert NodeRun.model_validate_json(node.model_dump_json()) == node
    assert CapabilityInvocation.model_validate_json(invocation.model_dump_json()) == invocation
    assert Artifact.model_validate_json(artifact.model_dump_json()) == artifact
    assert Event.model_validate_json(event.model_dump_json()) == event
    assert run.task_id == task.id
    assert node.run_id == run.id
    assert invocation.node_run_id == node.id
    assert artifact.invocation_id == invocation.id
    assert event.artifact_id == artifact.id


def test_checkpoint_requires_correlated_waiting_and_terminal_state() -> None:
    _, run, node, invocation, _, _ = domain_records()
    checkpoint = Checkpoint(
        id=new_checkpoint_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        state_hash="c" * 64,
    )

    assert Checkpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint
    with pytest.raises(ValidationError, match="resume timestamp"):
        Checkpoint.model_validate({**checkpoint.model_dump(), "status": CheckpointStatus.RESUMED})
    with pytest.raises(ValidationError, match="terminal timestamp"):
        Checkpoint.model_validate(
            {
                **checkpoint.model_dump(),
                "status": CheckpointStatus.COMPLETED,
                "resumed_at": checkpoint.created_at,
            }
        )


def test_timestamps_are_normalized_to_utc() -> None:
    task = Task(
        id=new_task_id(),
        request="Normalize time.",
        created_at=datetime(2026, 7, 17, 11, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert task.created_at.utcoffset() == timedelta(0)
    assert task.created_at.hour == 3


def test_naive_timestamps_fail() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Task(
            id=new_task_id(),
            request="Reject naive time.",
            created_at=datetime(2026, 7, 17, 3, 0),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("uri", "/private/tmp/result.txt", "anban://artifact"),
        ("sha256", "not-a-digest", "sha256"),
    ],
)
def test_artifact_invariants_fail(field: str, value: str, message: str) -> None:
    values: dict[str, object] = {
        "id": new_artifact_id(),
        "run_id": new_execution_run_id(),
        "uri": "anban://artifact/run/result",
        "sha256": "a" * 64,
        "size_bytes": 1,
        "media_type": "text/plain",
    }
    values[field] = value
    with pytest.raises(ValidationError, match=message):
        Artifact.model_validate(values)


def test_interaction_envelope_is_transport_only() -> None:
    envelope = InteractionEnvelope(
        id=new_interaction_id(),
        content="Create one Task through Core.",
        metadata=SafeMetadata({"terminal": "tty"}),
    )
    assert envelope.source == "cli"
    assert not hasattr(envelope, "task_id")
