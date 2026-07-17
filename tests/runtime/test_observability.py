"""Deterministic Event, Audit, Trace, and incomplete-execution projections."""

from __future__ import annotations

import pytest

from anban.capability import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata
from anban.model import ModelTurn, ToolCall
from anban.runtime import AgentOutcomeStatus, EventProjectionService, PersistentRuntime
from tests.runtime.test_persistent_runtime import (
    MemoryUnitOfWorkFactory,
    TransactionCheckingCapability,
    TransactionCheckingModel,
    completed_capability,
    final_turn,
    tool_turn,
)


async def test_success_trace_and_audit_are_stable_after_new_service_instance() -> None:
    factory = MemoryUnitOfWorkFactory()
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [tool_turn(), final_turn()]),
        CapabilityRegistry(
            (TransactionCheckingCapability(factory, completed_capability(artifact=True)),)
        ),
        factory,
    ).execute("Persist one governed result.")

    first = await EventProjectionService(factory).inspect(result.run_id)
    restarted = await EventProjectionService(factory).inspect(result.run_id)

    assert first == restarted
    assert first.complete is True
    assert first.inconsistencies == ()
    assert tuple(entry.sequence for entry in first.trace) == tuple(range(1, len(first.trace) + 1))
    audit_types = {entry.event_type for entry in first.audit}
    assert audit_types >= {
        "model.requested",
        "model.completed",
        "capability.requested",
        "capability.completed",
        "artifact.created",
        "run.final",
    }
    assert "task.created" not in audit_types


@pytest.mark.parametrize(
    ("capability_status", "expected_status"),
    [
        (CapabilityResultStatus.FAILED, AgentOutcomeStatus.FAILED),
        (CapabilityResultStatus.TIMED_OUT, AgentOutcomeStatus.TIMED_OUT),
    ],
)
async def test_failed_and_timed_out_runs_have_complete_explainable_trace(
    capability_status: CapabilityResultStatus,
    expected_status: AgentOutcomeStatus,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    error_code = (
        ErrorCode.EXECUTION_TIMED_OUT
        if capability_status is CapabilityResultStatus.TIMED_OUT
        else ErrorCode.CAPABILITY_EXECUTION_FAILED
    )
    capability = TransactionCheckingCapability(
        factory,
        CapabilityResult(
            status=capability_status,
            error=ErrorInfo(code=error_code, message="Capability failed safely"),
        ),
    )
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [tool_turn()]),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Record a terminal failure.")

    observation = await EventProjectionService(factory).inspect(result.run_id)
    assert result.outcome.status is expected_status
    assert observation.complete is True
    assert "run.error" in {entry.event_type for entry in observation.audit}
    terminal_capability = f"capability.{capability_status.value}"
    assert terminal_capability in {entry.event_type for entry in observation.audit}


async def test_incomplete_invocation_is_detected_after_event_write_failure() -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "capability.completed"
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [tool_turn(), final_turn()]),
        CapabilityRegistry((TransactionCheckingCapability(factory, completed_capability()),)),
        factory,
    ).execute("Do not retry the uncertain side effect.")

    observation = await EventProjectionService(factory).inspect(result.run_id)
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
    assert observation.complete is False
    assert observation.inconsistencies == ("invocation_incomplete",)


async def test_projection_metadata_uses_allowlist_even_for_safe_event_values() -> None:
    factory = MemoryUnitOfWorkFactory()
    result = await PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                ModelTurn(
                    content="Done.",
                    finish_reason="stop",
                    metadata=SafeMetadata(
                        {"provider": "test", "unlisted_fact": "audit-trace-canary"}
                    ),
                )
            ],
        ),
        CapabilityRegistry(),
        factory,
    ).execute("Project safe metadata.")

    persisted_event = next(
        event for event in factory.store.events.values() if event.event_type == "model.completed"
    )
    factory.store.events[persisted_event.id] = persisted_event.model_copy(
        update={
            "metadata": SafeMetadata(
                {**persisted_event.metadata.root, "unlisted_fact": "audit-trace-canary"}
            )
        }
    )

    observation = await EventProjectionService(factory).inspect(result.run_id)
    model_event = next(
        entry for entry in observation.audit if entry.event_type == "model.completed"
    )
    assert model_event.metadata.root["provider"] == "test"
    assert "unlisted_fact" not in model_event.metadata.root
    assert "audit-trace-canary" not in observation.model_dump_json()


async def test_skill_activation_is_distinct_and_uses_logical_reference() -> None:
    factory = MemoryUnitOfWorkFactory()
    capability = TransactionCheckingCapability(
        factory,
        CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation="Activated approved Workspace Skill.",
            metadata=SafeMetadata(
                {
                    "skill_slug": "@steipete/weather",
                    "skill_version": "1.0.0",
                    "skill_source": "anban://skill/@steipete/weather/1.0.0",
                    "content_hash": "a" * 64,
                    "omitted_line_count": 0,
                }
            ),
        ),
    )
    capability.descriptor = CapabilityDescriptor(
        name="skill.activate",
        description="Activate the approved Workspace Skill.",
        kind=CapabilityKind.SKILL,
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    result = await PersistentRuntime(
        TransactionCheckingModel(
            factory,
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(
                            id="skill-call",
                            name="skill.activate",
                            arguments={"name": "@steipete/weather"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                final_turn(),
            ],
        ),
        CapabilityRegistry((capability,)),
        factory,
    ).execute("Activate the approved Skill.")

    observation = await EventProjectionService(factory).inspect(result.run_id)
    skill_event = next(
        entry for entry in observation.audit if entry.event_type == "skill.activated"
    )
    assert skill_event.metadata.root["skill_source"] == ("anban://skill/@steipete/weather/1.0.0")
    assert "capability.completed" in {entry.event_type for entry in observation.audit}


async def test_observability_read_failure_never_returns_partial_success() -> None:
    factory = MemoryUnitOfWorkFactory()
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [final_turn()]),
        CapabilityRegistry(),
        factory,
    ).execute("Persist before inspection.")
    factory.fail_load = True

    with pytest.raises(AnbanError) as raised:
        await EventProjectionService(factory).inspect(result.run_id)
    assert raised.value.info.code is ErrorCode.PERSISTENCE_UNAVAILABLE
