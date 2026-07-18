"""Durable background Process lifecycle through the ordinary Runtime root."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from anban.capability import CapabilityRegistry
from anban.capability.process import ProcessCapability
from anban.capability.workspace import WorkspaceBoundary
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import AgentOutcomeStatus, ExecutionQueryService, PersistentRuntime
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory


class BackgroundProcessModel:
    def __init__(self, factory: MemoryUnitOfWorkFactory, turns: list[ModelTurn]) -> None:
        self._factory = factory
        self._turns = turns

    async def complete(self, request: ModelRequest) -> ModelTurn:
        assert self._factory.active == 0
        return self._turns.pop(0)


async def test_background_process_progress_and_result_survive_fresh_query(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    registry = CapabilityRegistry((ProcessCapability(WorkspaceBoundary(tmp_path)),))
    model = BackgroundProcessModel(
        factory,
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="background-call",
                        name="process.execute",
                        arguments={
                            "command": sys.executable,
                            "args": [
                                "-c",
                                "import time;from pathlib import Path;time.sleep(.2);"
                                "Path('result.txt').write_text('one real execution')",
                            ],
                            "artifacts": [{"path": "result.txt", "media_type": "text/plain"}],
                            "background": True,
                        },
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelTurn(content="The real background result is durable.", finish_reason="stop"),
        ],
    )

    result = await PersistentRuntime(model, registry, factory).execute(
        "Run one real background process and retain its result."
    )

    assert result.persisted
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    async with factory() as unit:
        aggregate = await unit.executions.load_run(result.run_id)
    assert aggregate is not None
    assert len(aggregate.invocations) == 1
    invocation = aggregate.invocations[0]
    assert invocation.status.value == "succeeded"
    assert len(aggregate.artifacts) == 1
    assert aggregate.artifacts[0].invocation_id == invocation.id
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("capability.background_started") == 1
    assert event_types.count("capability.progressed") >= 1
    assert event_types.count("capability.completed") == 1
    background = next(
        event for event in aggregate.events if event.event_type == "capability.background_started"
    )
    progress = next(
        event for event in aggregate.events if event.event_type == "capability.progressed"
    )
    terminal = next(
        event for event in aggregate.events if event.event_type == "capability.completed"
    )
    correlation = str(invocation.id)
    assert background.invocation_id == progress.invocation_id == terminal.invocation_id
    assert background.metadata.root["result_correlation_id"] == correlation
    assert progress.metadata.root["progress_sequence"] == 1
    assert progress.metadata.root["result_correlation_id"] == correlation
    assert terminal.metadata.root["result_correlation_id"] == correlation
    assert (tmp_path / "result.txt").read_text() == "one real execution"

    trace = await ExecutionQueryService(factory).trace(result.run_id)
    assert trace.complete
    assert {
        "capability.background_started",
        "capability.progressed",
        "capability.completed",
    } <= {entry.event_type for entry in trace.audit}
    assert str(tmp_path) not in trace.model_dump_json()


async def test_background_acceptance_event_failure_cancels_without_late_success(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    factory.fail_event_type = "capability.background_started"
    registry = CapabilityRegistry((ProcessCapability(WorkspaceBoundary(tmp_path)),))
    model = BackgroundProcessModel(
        factory,
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        id="cancel-after-persistence-failure",
                        name="process.execute",
                        arguments={
                            "command": sys.executable,
                            "args": [
                                "-c",
                                "import time;from pathlib import Path;time.sleep(.4);"
                                "Path('late.txt').write_text('must not appear')",
                            ],
                            "background": True,
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        ],
    )

    result = await PersistentRuntime(model, registry, factory).execute(
        "Fail closed if background audit persistence fails."
    )

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    await asyncio.sleep(0.5)
    assert not (tmp_path / "late.txt").exists()
    async with factory() as unit:
        aggregate = await unit.executions.load_run(result.run_id)
    assert aggregate is not None
    assert aggregate.invocations[0].status.value == "cancelled"
    assert "capability.cancelled" in {event.event_type for event in aggregate.events}
    assert "capability.completed" not in {event.event_type for event in aggregate.events}
