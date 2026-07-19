"""Real MCP Tool execution through Runtime persistence and observability."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from anban.capability import (
    CapabilityRegistry,
    UnifiedCapabilityInventory,
    discover_mcp_capabilities,
)
from anban.config import McpConfiguration, McpServerConfiguration
from anban.model import ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import (
    TransactionCheckingModel,
    assessment_turn,
    completion_turn,
    final_turn,
)

_SERVER = Path(__file__).parents[2] / "scripts" / "acceptance" / "mcp_fixture_server.py"


async def test_mcp_tool_uses_ordinary_runtime_persistence_and_trace(tmp_path: Path) -> None:
    tool_name = f"runtime-operation-{uuid4().hex}"
    handlers = await discover_mcp_capabilities(
        McpConfiguration(
            request_timeout_seconds=10,
            output_max_bytes=65_536,
            max_tools_per_server=8,
            servers=(
                McpServerConfiguration(
                    name="runtime",
                    command=sys.executable,
                    args=(str(_SERVER), tool_name, "runtime-mcp-count.txt"),
                    cwd=".",
                    environment={},
                ),
            ),
        ),
        tmp_path,
        protected_values=(),
    )
    registry = CapabilityRegistry(handlers)
    descriptor = registry.search()[0]
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    factory = MemoryUnitOfWorkFactory()
    final = "The real MCP Tool completed through the governed Runtime."
    model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.USE_CAPABILITY, descriptor.name),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="mcp-runtime-call",
                        name=descriptor.name,
                        arguments={"label": "runtime object", "value": 21},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            final_turn(final),
            completion_turn(final_text=final),
        ],
    )

    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Discover and invoke one changed structured MCP Tool.")

    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == final
    assert (tmp_path / "runtime-mcp-count.txt").read_text(encoding="utf-8") == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(result.run_id)
    assert aggregate is not None
    assert len(aggregate.invocations) == 1
    assert aggregate.invocations[0].capability_name == descriptor.name
    assert aggregate.invocations[0].status.value == "succeeded"
    completed = next(
        event for event in aggregate.events if event.event_type == "capability.completed"
    )
    assert completed.metadata.root["mcp_server"] == "runtime"
    assert completed.metadata.root["mcp_structured"] is True
    assert completed.metadata.root["mcp_content_count"] == 1
    assert isinstance(completed.metadata.root["mcp_tool_digest"], str)
    assert str(_SERVER) not in str(aggregate.events)
    trace = await ExecutionQueryService(factory).trace(result.run_id)
    restarted_trace = await ExecutionQueryService(factory).trace(result.run_id)
    assert trace == restarted_trace
    assert trace.complete is True
    assert trace.inconsistencies == ()
    projected = next(event for event in trace.audit if event.event_type == "capability.completed")
    assert projected.metadata.root["mcp_server"] == "runtime"
    assert projected.metadata.root["mcp_structured"] is True
