"""Generalized Capability recovery and persistence compensation."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
    local_capability_registry,
)
from anban.capability.workspace import WorkspaceBoundary
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from anban.core.metadata import SafeMetadata
from anban.model import ModelTurn, ToolCall
from anban.runtime import AgentOutcomeStatus, ExecutionQueryService, PersistentRuntime
from tests.runtime.test_persistent_runtime import (
    MemoryUnitOfWorkFactory,
    TransactionCheckingCapability,
    TransactionCheckingModel,
    completed_capability,
    final_turn,
    load_run,
)


def action_turn(call_id: str, arguments: dict[str, JsonValue]) -> ModelTurn:
    return ModelTurn(
        tool_calls=(ToolCall(id=call_id, name="test.action", arguments=arguments),),
        finish_reason="tool_calls",
    )


def process_turn(call_id: str, arguments: dict[str, JsonValue]) -> ModelTurn:
    return ModelTurn(
        tool_calls=(ToolCall(id=call_id, name="process.execute", arguments=arguments),),
        finish_reason="tool_calls",
    )


async def test_schema_failure_is_persisted_then_returned_for_model_replanning() -> None:
    factory = MemoryUnitOfWorkFactory()
    capability = TransactionCheckingCapability(factory, completed_capability())
    model = TransactionCheckingModel(
        factory,
        [
            action_turn("invalid-call", {"path": "first-target"}),
            action_turn("corrected-call", {"path": "second-target", "content": "bounded"}),
            final_turn("Replanned successfully."),
        ],
    )

    result = await PersistentRuntime(model, CapabilityRegistry((capability,)), factory).execute(
        "Choose valid arguments after a safe failure."
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert capability.calls == 1
    assert model.calls == 3
    tool_result = next(
        message.tool_result for message in model.requests[1].messages if message.role == "tool"
    )
    assert tool_result is not None
    assert json.loads(tool_result.content) == {
        "status": "failed",
        "error_code": "capability_arguments_invalid",
        "reason": "missing_required_fields",
    }
    aggregate = await load_run(factory, result.run_id)
    assert [item.status.value for item in aggregate.invocations] == ["failed", "succeeded"]
    failed_event = next(
        event for event in aggregate.events if event.event_type == "capability.failed"
    )
    assert failed_event.metadata.root["reason"] == "missing_required_fields"
    observation = await ExecutionQueryService(factory).trace(result.run_id)
    assert observation.complete is True
    assert observation.inconsistencies == ()


@pytest.mark.parametrize("cwd_kind", ["missing", "regular_file"])
async def test_real_process_cwd_failure_can_be_replanned_without_runtime_rewrite(
    tmp_path: Path, cwd_kind: str
) -> None:
    unavailable = tmp_path / f"unavailable-{cwd_kind}"
    if cwd_kind == "regular_file":
        unavailable.write_text("not a directory", encoding="utf-8")
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [
            process_turn(
                f"invalid-{cwd_kind}",
                {"command": sys.executable, "cwd": str(unavailable)},
            ),
            process_turn(
                f"corrected-{cwd_kind}",
                {
                    "command": sys.executable,
                    "args": ["-c", "print('bounded')"],
                    "cwd": ".",
                },
            ),
            final_turn("Process replanned successfully."),
        ],
    )
    result = await PersistentRuntime(
        model,
        local_capability_registry(workspace_root=tmp_path),
        factory,
        artifact_cleanup=WorkspaceBoundary(tmp_path).delete_artifact,
    ).execute("Select a usable working directory after a safe failure.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    aggregate = await load_run(factory, result.run_id)
    assert [item.status.value for item in aggregate.invocations] == ["failed", "succeeded"]
    failed = next(event for event in aggregate.events if event.event_type == "capability.failed")
    assert failed.metadata.root["reason"] == "invalid_cwd"


@pytest.mark.parametrize(
    "unavailable_command", ["missing-executable-alpha", "missing-executable-omega"]
)
async def test_real_missing_executable_can_be_replanned_by_the_model(
    tmp_path: Path, unavailable_command: str
) -> None:
    factory = MemoryUnitOfWorkFactory()
    result = await PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                process_turn("missing-program", {"command": unavailable_command}),
                process_turn(
                    "available-program",
                    {"command": sys.executable, "args": ["-c", "print('ok')"]},
                ),
                final_turn("Used an available program."),
            ],
        ),
        local_capability_registry(workspace_root=tmp_path),
        factory,
        artifact_cleanup=WorkspaceBoundary(tmp_path).delete_artifact,
    ).execute("Select an available executable after a safe failure.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    aggregate = await load_run(factory, result.run_id)
    assert [item.status.value for item in aggregate.invocations] == ["failed", "succeeded"]
    failed = next(event for event in aggregate.events if event.event_type == "capability.failed")
    assert failed.metadata.root["reason"] == "missing_executable"


async def test_unavailable_capability_is_recoverable_but_unknown_capability_is_terminal() -> None:
    recoverable_factory = MemoryUnitOfWorkFactory()
    unavailable = TransactionCheckingCapability(recoverable_factory, completed_capability())
    unavailable.descriptor = unavailable.descriptor.model_copy(update={"available": False})
    recovered = await PersistentRuntime(
        TransactionCheckingModel(
            recoverable_factory,
            [
                action_turn("unavailable-call", {"path": "target-a", "content": "bounded"}),
                final_turn("Used a different plan."),
            ],
        ),
        CapabilityRegistry((unavailable,)),
        recoverable_factory,
    ).execute("Replan when a declared tool is unavailable.")
    assert recovered.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert unavailable.calls == 0

    terminal_factory = MemoryUnitOfWorkFactory()
    unknown = ModelTurn(
        tool_calls=(ToolCall(id="unknown-call", name="unknown.action", arguments={}),),
        finish_reason="tool_calls",
    )
    terminal_model = TransactionCheckingModel(
        terminal_factory, [unknown, final_turn("must not execute")]
    )
    terminal = await PersistentRuntime(
        terminal_model, CapabilityRegistry(), terminal_factory
    ).execute("Do not recover an unregistered Capability.")
    assert terminal.outcome.status is AgentOutcomeStatus.FAILED
    assert terminal.outcome.error is not None
    assert terminal.outcome.error.code is ErrorCode.CAPABILITY_UNKNOWN
    assert terminal_model.calls == 1


@pytest.mark.parametrize("reason", [None, "raw failure for a caller supplied target"])
async def test_error_without_stable_machine_reason_remains_terminal(
    reason: str | None,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    failure = AnbanError(
        ErrorInfo(
            code=ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
            message="Capability arguments are invalid",
            details=SafeMetadata({} if reason is None else {"reason": reason}),
        )
    )
    capability = TransactionCheckingCapability(factory, completed_capability())

    async def fail_without_reason(
        arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        raise failure

    capability.invoke = fail_without_reason  # type: ignore[method-assign]
    model = TransactionCheckingModel(
        factory,
        [
            action_turn("unsafe-call", {"path": "target-b", "content": "bounded"}),
            final_turn("must not execute"),
        ],
    )
    result = await PersistentRuntime(model, CapabilityRegistry((capability,)), factory).execute(
        "Stop when no safe observation exists."
    )
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert model.calls == 1


class SnapshotCapability:
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        sources: tuple[Path, ...],
        *,
        before_return: Callable[[], None] | None = None,
    ) -> None:
        self.boundary = boundary
        self.sources = sources
        self.calls = 0
        self.references: tuple[ArtifactReference, ...] = ()
        self.before_return = before_return
        self.descriptor = CapabilityDescriptor(
            name="test.snapshot",
            description="Create managed snapshots for a bounded test.",
            input_schema={
                "type": "object",
                "properties": {"label": {"type": "string", "minLength": 1}},
                "required": ["label"],
                "additionalProperties": False,
            },
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        self.calls += 1
        references = tuple(
            self.boundary.create_artifact(
                context,
                source.read_bytes(),
                "application/json" if source.suffix == ".json" else "text/plain",
            )
            for source in self.sources
        )
        self.references = references
        if self.before_return is not None:
            self.before_return()
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation="snapshots created",
            artifacts=references,
        )

    async def cancel(self, context: InvocationContext) -> None:
        return None


def snapshot_turn() -> ModelTurn:
    return ModelTurn(
        tool_calls=(
            ToolCall(
                id="snapshot-call",
                name="test.snapshot",
                arguments={"label": "bounded"},
            ),
        ),
        finish_reason="tool_calls",
    )


@pytest.mark.parametrize("failure_mode", ["database", "event"])
async def test_uncommitted_multiple_artifacts_are_cleaned_without_deleting_sources(
    tmp_path: Path, failure_mode: str
) -> None:
    sources = (tmp_path / "alpha.note", tmp_path / "beta.data.json")
    sources[0].write_text("alpha", encoding="utf-8")
    sources[1].write_text('{"value":2}', encoding="utf-8")
    boundary = WorkspaceBoundary(tmp_path)
    capability = SnapshotCapability(boundary, sources)
    factory = MemoryUnitOfWorkFactory()
    if failure_mode == "database":
        factory.fail_add_artifact = True
    else:
        factory.fail_event_type = "artifact.created"
    other_context = InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    other_reference = boundary.create_artifact(
        other_context, b"unrelated", "application/octet-stream"
    )
    other_snapshot = tmp_path / "artifacts" / str(other_context.run_id) / str(other_reference.id)

    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [snapshot_turn(), final_turn()]),
        CapabilityRegistry((capability,)),
        factory,
        artifact_cleanup=boundary.delete_artifact,
    ).execute("Create managed snapshots once.")

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is (
        ErrorCode.PERSISTENCE_WRITE_FAILED
        if failure_mode == "database"
        else ErrorCode.AUDIT_TRACE_WRITE_FAILED
    )
    assert capability.calls == 1
    assert all(source.exists() for source in sources)
    assert other_snapshot.exists()
    assert all(
        not (tmp_path / "artifacts" / str(result.run_id) / str(reference.id)).exists()
        for reference in capability.references
    )
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.artifacts == ()
    assert aggregate.invocations[0].status.value == "failed"
    assert result.outcome.error.details.root["artifact_cleanup_attempted"] == 2
    assert result.outcome.error.details.root["artifact_cleanup_succeeded"] == 2


async def test_unconfirmed_persistence_state_keeps_snapshots_and_reports_uncertainty(
    tmp_path: Path,
) -> None:
    source = tmp_path / "epsilon.bin"
    source.write_bytes(b"bounded")
    boundary = WorkspaceBoundary(tmp_path)
    factory = MemoryUnitOfWorkFactory()

    def make_next_load_fail() -> None:
        factory.fail_next_load = True

    capability = SnapshotCapability(boundary, (source,), before_return=make_next_load_fail)
    factory.fail_event_type = "capability.completed"
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [snapshot_turn(), final_turn()]),
        CapabilityRegistry((capability,)),
        factory,
        artifact_cleanup=boundary.delete_artifact,
    ).execute("Preserve a snapshot while commit state is unknown.")

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.details.root["persistence_state_unconfirmed"] is True
    snapshot = tmp_path / "artifacts" / str(result.run_id) / str(capability.references[0].id)
    assert source.exists()
    assert snapshot.exists()
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.invocations[0].status.value == "running"


async def test_ambiguous_commit_is_confirmed_without_compensation_or_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.commit_before_event_failure_type = "capability.completed"
    capability = TransactionCheckingCapability(factory, completed_capability())
    result = await PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                action_turn("ambiguous-call", {"path": "target-d", "content": "bounded"}),
                final_turn("Confirmed once."),
            ],
        ),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Confirm an ambiguous terminal commit.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert capability.calls == 1
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.invocations[0].status.value == "succeeded"
    assert sum(event.event_type == "capability.completed" for event in aggregate.events) == 1
    assert not any(event.event_type == "capability.failed" for event in aggregate.events)


async def test_partial_artifact_cleanup_is_explicit_and_attempts_every_snapshot(
    tmp_path: Path,
) -> None:
    sources = (tmp_path / "gamma.txt", tmp_path / "delta.json")
    for index, source in enumerate(sources):
        source.write_text(str(index), encoding="utf-8")
    boundary = WorkspaceBoundary(tmp_path)
    capability = SnapshotCapability(boundary, sources)
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "capability.completed"
    attempts = 0

    def partial_cleanup(context: InvocationContext, reference: ArtifactReference) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("test-only cleanup failure")
        boundary.delete_artifact(context, reference)

    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [snapshot_turn(), final_turn()]),
        CapabilityRegistry((capability,)),
        factory,
        artifact_cleanup=partial_cleanup,
    ).execute("Expose a partial cleanup failure safely.")

    assert attempts == 2
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.details.root["artifact_cleanup_failed"] is True
    assert result.outcome.error.details.root["artifact_cleanup_succeeded"] == 1
    assert all(source.exists() for source in sources)


async def test_compensation_failure_is_explicit_and_never_replays_capability() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_types = ["capability.completed", "capability.failed"]
    capability = TransactionCheckingCapability(factory, completed_capability())
    result = await PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                action_turn("one-call", {"path": "target-c", "content": "bounded"}),
                final_turn("must not execute"),
            ],
        ),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Do not replay after compensation fails.")

    assert capability.calls == 1
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.details.root["invocation_compensation_failed"] is True
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.invocations[0].status.value == "running"
    observation = await ExecutionQueryService(factory).trace(result.run_id)
    assert observation.complete is False
    assert "invocation_incomplete" in observation.inconsistencies
