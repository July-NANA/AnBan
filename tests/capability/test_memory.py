"""Durable structured Memory Capability behavior and failure integrity."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from anban.capability import (
    CapabilityRegistry,
    InvocationContext,
    MemoryContextCapability,
    UnifiedCapabilityInventory,
)
from anban.core import (
    AnbanError,
    ContextCompressionBoundary,
    ContextConflictState,
    ContextScope,
    ErrorCode,
    ExecutionRun,
    NodeRun,
    SafeMetadata,
    Task,
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
    new_session_id,
    new_task_id,
    now_utc,
)
from anban.model import ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import TransactionCheckingModel, final_turn


async def persisted_context(
    factory: MemoryUnitOfWorkFactory, *, session: bool = False
) -> tuple[InvocationContext, Task]:
    task = Task(id=new_task_id(), request=f"context task {uuid4().hex}")
    run = ExecutionRun(id=new_execution_run_id(), task_id=task.id)
    node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
    async with factory() as unit:
        await unit.executions.add_task(task)
        await unit.executions.add_run(run)
        await unit.executions.add_node_run(node)
        await unit.commit()
    metadata = SafeMetadata({"session_id": str(new_session_id())}) if session else SafeMetadata()
    return (
        InvocationContext(
            run_id=run.id,
            node_run_id=node.id,
            invocation_id=new_capability_invocation_id(),
            deadline_at=now_utc() + timedelta(minutes=1),
            metadata=metadata,
        ),
        task,
    )


def renewed(context: InvocationContext) -> InvocationContext:
    return context.model_copy(update={"invocation_id": new_capability_invocation_id()})


@pytest.mark.parametrize(
    "content",
    [
        "Retain the newly supplied deployment constraint.",
        "记住这项新的、可复用的任务约束。",
        "Preserve a changed preference for the next bounded decision.",
    ],
)
async def test_memory_retains_semantic_variants_with_source_and_restart(
    content: str,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    context, task = await persisted_context(factory)
    capability = MemoryContextCapability(factory)

    result = await capability.invoke(
        {
            "operation": "remember",
            "scope": "task",
            "kind": "user_fact",
            "content": content,
        },
        context,
    )
    entry_id = json.loads(result.observation or "{}")["entry_id"]

    restarted = MemoryContextCapability(factory)
    recalled = await restarted.invoke({"operation": "read", "scope": "task"}, renewed(context))
    payload = json.loads(recalled.observation or "{}")
    assert payload["entries"][0]["id"] == entry_id
    assert payload["entries"][0]["content"] == content
    assert payload["entries"][0]["source"]["kind"] == "capability"
    detail = await ExecutionQueryService(factory).task_context(task.id)
    assert detail.active_entry_count == 1
    assert detail.entries[0].content_hash
    assert content not in detail.model_dump_json()


async def test_supersede_conflict_expiry_and_compression_preserve_raw_facts() -> None:
    factory = MemoryUnitOfWorkFactory()
    context, task = await persisted_context(factory)
    capability = MemoryContextCapability(factory)

    first = await capability.invoke(
        {"operation": "remember", "scope": "task", "content": "Original fact."},
        context,
    )
    first_id = json.loads(first.observation or "{}")["entry_id"]
    second = await capability.invoke(
        {
            "operation": "remember",
            "scope": "task",
            "content": "Authoritative replacement fact.",
            "relationship": "supersedes",
            "related_entry_id": first_id,
        },
        renewed(context),
    )
    second_id = json.loads(second.observation or "{}")["entry_id"]
    conflict = await capability.invoke(
        {
            "operation": "remember",
            "scope": "task",
            "content": "A conflicting observation requiring resolution.",
            "relationship": "conflicts_with",
            "related_entry_id": second_id,
        },
        renewed(context),
    )
    conflict_id = json.loads(conflict.observation or "{}")["entry_id"]
    compressed = await capability.invoke(
        {
            "operation": "compress",
            "scope": "task",
            "content": "Replacement and conflicting observation remain explicitly unresolved.",
            "covered_entry_ids": [second_id, conflict_id],
        },
        renewed(context),
    )
    assert compressed.metadata.root["original_entries_retained"] is True
    await capability.invoke(
        {
            "operation": "expire",
            "scope": "task",
            "related_entry_id": first_id,
        },
        renewed(context),
    )

    async with factory() as unit:
        entries = await unit.executions.list_context_entries(ContextScope.TASK, task.id)
    assert len(entries) == 3
    assert entries[0].state is ContextConflictState.EXPIRED
    assert all(entry.state is ContextConflictState.SUPERSEDED for entry in entries[1:])
    detail = await ExecutionQueryService(factory).task_context(task.id)
    assert detail.active_entry_count == 0
    assert len(detail.summaries) == 1
    assert set(map(str, detail.summaries[0].covered_entry_ids)) == {second_id, conflict_id}


async def test_failed_compression_and_bounds_leave_authoritative_entries_unchanged() -> None:
    factory = MemoryUnitOfWorkFactory()
    context, task = await persisted_context(factory)
    capability = MemoryContextCapability(
        factory,
        protected_values=("protected-canary",),
        boundary=ContextCompressionBoundary(max_active_entries=2),
    )
    identifiers: list[str] = []
    for content in ("First retained fact.", "Second retained fact."):
        result = await capability.invoke(
            {"operation": "remember", "scope": "task", "content": content},
            renewed(context),
        )
        identifiers.append(json.loads(result.observation or "{}")["entry_id"])

    with pytest.raises(AnbanError) as invalid_summary:
        await capability.invoke(
            {
                "operation": "compress",
                "scope": "task",
                "content": "Invalid coverage must roll back.",
                "covered_entry_ids": [identifiers[0], str(uuid4())],
            },
            renewed(context),
        )
    assert invalid_summary.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    with pytest.raises(AnbanError):
        await capability.invoke(
            {
                "operation": "remember",
                "scope": "task",
                "content": "Third entry exceeds the active bound.",
            },
            renewed(context),
        )
    with pytest.raises(AnbanError):
        await capability.invoke(
            {
                "operation": "remember",
                "scope": "task",
                "content": "protected-canary",
            },
            renewed(context),
        )

    async with factory() as unit:
        entries = await unit.executions.list_context_entries(ContextScope.TASK, task.id)
        summaries = await unit.executions.list_context_summaries(ContextScope.TASK, task.id)
    assert len(entries) == 2
    assert all(entry.state is ContextConflictState.ACTIVE for entry in entries)
    assert summaries == ()


async def test_session_context_requires_runtime_identity_and_survives_new_capability() -> None:
    factory = MemoryUnitOfWorkFactory()
    task_context, _ = await persisted_context(factory)
    capability = MemoryContextCapability(factory)
    with pytest.raises(AnbanError) as missing:
        await capability.invoke({"operation": "read", "scope": "session"}, task_context)
    assert missing.value.info.details.root["reason"] == "session_context_unavailable"

    session_context, _ = await persisted_context(factory, session=True)
    await capability.invoke(
        {
            "operation": "remember",
            "scope": "session",
            "content": "Session preference retained across Application reconstruction.",
        },
        session_context,
    )
    recalled = await MemoryContextCapability(factory).invoke(
        {"operation": "read", "scope": "session"}, renewed(session_context)
    )
    assert "Session preference" in (recalled.observation or "")


def memory_assessment() -> ModelTurn:
    return ModelTurn(
        structured_output={
            "strategy": ExecutionStrategy.USE_CAPABILITY.value,
            "target": "memory.context",
            "rationale": "Durable context is the lowest-complexity reliable path.",
            "confidence": 0.93,
            "missing_condition": "",
            "substantial_temporary_code": False,
            "complex_domain_workflow": False,
            "high_improvisation_risk": False,
            "low_implementation_confidence": False,
            "repeated_reusable_need": False,
            "existing_process_path_unreasonable": False,
        },
        finish_reason="stop",
    )


async def test_ordinary_runtime_invocation_persists_memory_audit_without_raw_content() -> None:
    factory = MemoryUnitOfWorkFactory()
    memory = MemoryContextCapability(factory)
    registry = CapabilityRegistry((memory,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    content = f"Durable runtime fact {uuid4().hex}."
    model = TransactionCheckingModel(
        factory,
        [
            memory_assessment(),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="remember",
                        name="memory.context",
                        arguments={
                            "operation": "remember",
                            "scope": "task",
                            "content": content,
                        },
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="read",
                        name="memory.context",
                        arguments={"operation": "read", "scope": "task"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            final_turn("The durable fact was retained and recalled."),
        ],
    )
    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Retain and recall one bounded context fact.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    trace = await ExecutionQueryService(factory).trace(result.run_id)
    event_types = [event.event_type for event in trace.audit]
    assert event_types.count("context.recorded") == 1
    assert event_types.count("context.read") == 1
    assert content not in trace.model_dump_json()


async def test_failed_memory_operation_is_audited_and_can_replan_without_success_fabrication() -> (
    None
):
    factory = MemoryUnitOfWorkFactory()
    memory = MemoryContextCapability(factory)
    registry = CapabilityRegistry((memory,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    model = TransactionCheckingModel(
        factory,
        [
            memory_assessment(),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="invalid-memory",
                        name="memory.context",
                        arguments={"operation": "remember", "scope": "task"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            final_turn("The invalid request was not stored."),
        ],
    )
    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Attempt one invalid bounded context write and report it truthfully.")

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    aggregate = await ExecutionQueryService(factory).show(result.run_id)
    assert aggregate.invocations[0].status.value == "failed"
    trace = await ExecutionQueryService(factory).trace(result.run_id)
    assert [event.event_type for event in trace.audit].count("context.operation_failed") == 1
    async with factory() as unit:
        assert (
            await unit.executions.list_context_entries(ContextScope.TASK, aggregate.task.id) == ()
        )
