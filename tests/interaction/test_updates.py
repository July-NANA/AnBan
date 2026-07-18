"""Durable mid-run supplemental input through the ordinary Interaction service."""

from __future__ import annotations

from pathlib import Path

import pytest

from anban.core import AnbanError, ContextScope, ErrorCode, ErrorInfo, SafeMetadata, TaskGraphSpec
from anban.core.ids import new_interaction_id
from anban.interaction import (
    CorrelatedWaitingExecution,
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
    InteractionService,
)
from anban.runtime import (
    AgentOutcomeStatus,
    ExecutionStrategy,
    PersistentRuntime,
    TaskRouteEvaluator,
)
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_continuation import background_turn
from tests.runtime.test_graph_routing import route_turn
from tests.runtime.test_interaction_updates import update_turn
from tests.runtime.test_persistent_runtime import (
    TransactionCheckingModel,
    assessment_turn,
    completion_turn,
    final_turn,
)
from tests.runtime.test_recovery import (
    direct_action_turns,
    registry,
    sufficiency,
    three_action_graph,
)


def supplemental(key: CorrelationKey, content: str) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source="message.adapter",
        input_kind=InteractionInputKind.SUPPLEMENTAL_INPUT,
        content=content,
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=key,
        ),
    )


async def test_context_update_survives_detach_and_completes_without_replay(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    initial_registry = registry(tmp_path)
    initial_model = TransactionCheckingModel(
        factory,
        [
            assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
            background_turn(
                "interaction-update",
                "import time;from pathlib import Path;time.sleep(.15);"
                "p=Path('update-count.txt');"
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
            ),
        ],
    )
    initial = InteractionService(
        PersistentRuntime(
            initial_model,
            initial_registry,
            factory,
            sufficiency=sufficiency(initial_registry),
            response_repair_retries=0,
        )
    )
    waiting = await initial.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Complete the background work and report it clearly.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

    update = "Report the completed result using the newly requested concise format."
    final = "The original operation completed once and the concise update was applied."
    restarted_registry = registry(tmp_path)
    restarted_model = TransactionCheckingModel(
        factory,
        [
            update_turn("context_only"),
            assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
            final_turn(final),
            completion_turn(final_text=final),
        ],
    )
    restarted = InteractionService(
        PersistentRuntime(
            restarted_model,
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            response_repair_retries=0,
        )
    )

    result = await restarted.submit(supplemental(waiting.resume_key, update))

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == final
    assert (tmp_path / "update-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        entries = await unit.executions.list_context_entries(ContextScope.TASK, waiting.task_id)
    assert aggregate is not None
    assert aggregate.graph_revision is None
    assert [entry.content for entry in entries] == [update]
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("interaction.resume_bound") == 1
    assert event_types.count("interaction.update_received") == 1
    assert event_types.count("interaction.context_applied") == 1
    assert event_types.count("run.recovery_completed") == 1
    assert waiting.resume_key.value not in str(aggregate.events)
    assert any(
        update in (message.content or "")
        for request in restarted_model.requests
        for message in request.messages
    )


async def test_unknown_update_correlation_fails_before_model_or_execution(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    gateway = registry(tmp_path)
    model = TransactionCheckingModel(factory, [])
    service = InteractionService(
        PersistentRuntime(model, gateway, factory, sufficiency=sufficiency(gateway))
    )
    unknown = CorrelationKey(
        purpose=CorrelationPurpose.RESUME,
        namespace="anban.continuation",
        value="unseen-external-correlation",
    )

    with pytest.raises(AnbanError) as captured:
        await service.submit(supplemental(unknown, "Apply an update to unknown work."))

    assert captured.value.info.details.root["reason"] == "unknown"
    assert model.calls == 0


async def test_structural_update_appends_revision_and_reuses_started_graph_actions(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    original = three_action_graph()
    revised_values = original.model_dump(mode="json")
    revised_values["nodes"][2]["objective"] = (
        "Publish the result with the newly supplied structural requirement."
    )
    revised = TaskGraphSpec.model_validate(revised_values)
    initial_registry = registry(tmp_path)
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    route_turn("task_graph", original.model_dump(mode="json")),
                    *direct_action_turns('{"seed":"prepared-once"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    background_turn(
                        "structural-update",
                        "import time;from pathlib import Path;time.sleep(.15);"
                        "p=Path('structural-count.txt');"
                        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
                    ),
                ],
            ),
            initial_registry,
            factory,
            sufficiency=sufficiency(initial_registry),
            route_evaluator=TaskRouteEvaluator(),
            response_repair_retries=0,
        )
    )
    waiting = await initial.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Execute the original three-stage graph.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

    active_final = '{"middle":"recovered-once"}'
    future_final = '{"result":"updated-graph-finished"}'
    repairable = AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="Completion response requires bounded repair.",
            details=SafeMetadata(
                {"diagnostic_reason": "structured_output_invalid", "repairable": True}
            ),
        )
    )
    restarted_registry = registry(tmp_path)
    restarted = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    update_turn("structural", revised.model_dump(mode="json")),
                    repairable,
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    final_turn(active_final),
                    repairable,
                    completion_turn(final_text=active_final),
                    assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
                    final_turn(future_final),
                    repairable,
                    completion_turn(final_text=future_final),
                ],
            ),
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            route_evaluator=TaskRouteEvaluator(),
            response_repair_retries=2,
        )
    )

    result = await restarted.submit(
        supplemental(
            waiting.resume_key,
            "Change the remaining publication step to include the new requirement.",
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "updated-graph-finished"
    assert (tmp_path / "structural-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        revisions = await unit.executions.list_graph_revisions(waiting.task_id)
    assert aggregate is not None
    assert [revision.spec for revision in revisions] == [original, revised]
    assert revisions[1].previous_revision_id == revisions[0].id
    assert aggregate.run.graph_revision_id == revisions[1].id
    graph_nodes = [node for node in aggregate.nodes if node.node_name != "general_agent"]
    assert [node.output for node in graph_nodes] == [
        {"seed": "prepared-once"},
        {"middle": "recovered-once"},
        {"result": "updated-graph-finished"},
    ]
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("interaction.graph_replanned") == 1
    assert event_types.count("graph.revision_created") == 2
    assert event_types.count("run.recovery_completed") == 1
    assert event_types.count("model.repair_requested") == 3
