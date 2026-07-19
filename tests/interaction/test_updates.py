"""Durable mid-run supplemental input through the ordinary Interaction service."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anban.core import (
    AnbanError,
    ContextScope,
    ErrorCode,
    ErrorInfo,
    SafeMetadata,
    TaskGraphEdge,
    TaskGraphEdgeKind,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
)
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
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
    TaskRouteEvaluator,
)
from tests.core.test_graph import action, node_output
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_capability_recovery import process_turn
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


def supplemental(
    key: CorrelationKey,
    content: str,
    *,
    input_kind: InteractionInputKind = InteractionInputKind.SUPPLEMENTAL_INPUT,
    source: str = "message.adapter",
    delivery: str | None = None,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source=source,
        input_kind=input_kind,
        content=content,
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=key,
            deduplication_key=(
                None
                if delivery is None
                else CorrelationKey(
                    purpose=CorrelationPurpose.DEDUPLICATION,
                    namespace="external.delivery",
                    value=delivery,
                )
            ),
        ),
    )


def parallel_update_graph() -> TaskGraphSpec:
    nodes = (
        action("prepare_input", outputs=("item",)),
        TaskGraphNode(
            id="fan_out",
            kind=TaskGraphNodeKind.PARALLEL,
            dependencies=("prepare_input",),
        ),
        action(
            "a_derive_result",
            dependencies=("fan_out",),
            inputs={"item": node_output("prepare_input", "item")},
            outputs=("derived",),
        ),
        action(
            "z_active_effect",
            dependencies=("fan_out",),
            inputs={"item": node_output("prepare_input", "item")},
            outputs=("effect",),
        ),
        TaskGraphNode(
            id="join_results",
            kind=TaskGraphNodeKind.JOIN,
            dependencies=("a_derive_result", "z_active_effect"),
            inputs={
                "derived": node_output("a_derive_result", "derived"),
                "effect": node_output("z_active_effect", "effect"),
            },
        ),
        action(
            "publish_result",
            dependencies=("join_results",),
            inputs={
                "derived": node_output("a_derive_result", "derived"),
                "effect": node_output("z_active_effect", "effect"),
            },
            outputs=("result",),
        ),
    )
    return TaskGraphSpec(
        nodes=nodes,
        edges=(
            TaskGraphEdge(source="prepare_input", target="fan_out"),
            TaskGraphEdge(
                source="fan_out",
                target="a_derive_result",
                kind=TaskGraphEdgeKind.PARALLEL,
            ),
            TaskGraphEdge(
                source="fan_out",
                target="z_active_effect",
                kind=TaskGraphEdgeKind.PARALLEL,
            ),
            TaskGraphEdge(
                source="a_derive_result",
                target="join_results",
                kind=TaskGraphEdgeKind.JOIN,
            ),
            TaskGraphEdge(
                source="z_active_effect",
                target="join_results",
                kind=TaskGraphEdgeKind.JOIN,
            ),
            TaskGraphEdge(source="join_results", target="publish_result"),
        ),
        entry_node="prepare_input",
        terminal_nodes=("publish_result",),
        outputs={"result": node_output("publish_result", "result")},
    )


@pytest.mark.parametrize(
    ("input_kind", "source", "update"),
    [
        (
            InteractionInputKind.USER_MESSAGE,
            "conversation.adapter",
            "Reply with the completed result using the newly requested concise format.",
        ),
        (
            InteractionInputKind.SUPPLEMENTAL_INPUT,
            "message.adapter",
            "Report the completed result using the newly requested concise format.",
        ),
        (
            InteractionInputKind.HUMAN_INPUT,
            "operator.adapter",
            "Use the supplied human direction and report the result concisely.",
        ),
    ],
)
async def test_human_origin_input_survives_restart_and_completes_without_replay(
    tmp_path: Path,
    input_kind: InteractionInputKind,
    source: str,
    update: str,
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
        ),
        unit_of_work=factory,
    )
    waiting = await initial.start_async(
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Complete the background work and report it clearly.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

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
        ),
        unit_of_work=factory,
    )

    delivery = f"delivery-{input_kind.value}"
    result = await restarted.submit(
        supplemental(
            waiting.resume_key,
            update,
            input_kind=input_kind,
            source=source,
            delivery=delivery,
        )
    )
    calls_after_result = restarted_model.calls
    duplicate = await InteractionService(
        PersistentRuntime(
            restarted_model,
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            response_repair_retries=0,
        ),
        unit_of_work=factory,
    ).submit(
        supplemental(
            waiting.resume_key,
            update,
            input_kind=input_kind,
            source=source,
            delivery=delivery,
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert duplicate.run_id == result.run_id
    assert result.outcome.final_text == final
    assert restarted_model.calls == calls_after_result
    assert (tmp_path / "update-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        entries = await unit.executions.list_context_entries(ContextScope.TASK, waiting.task_id)
    assert aggregate is not None
    assert aggregate.graph_revision is None
    assert [entry.content for entry in entries] == [update]
    assert entries[0].metadata.root["input_kind"] == input_kind.value
    assert entries[0].metadata.root["source"] == source
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("interaction.resume_bound") == 1
    assert event_types.count("interaction.routed") == 2
    assert event_types.count("interaction.inbox_routed") == 2
    assert event_types.count("interaction.update_received") == 1
    assert event_types.count("interaction.context_applied") == 1
    assert event_types.count("run.recovery_completed") == 1
    assert waiting.resume_key.value not in str(aggregate.events)
    assert delivery not in str(aggregate.events)
    routed = [
        event
        for event in aggregate.events
        if event.event_type == "interaction.routed"
        and event.metadata.root.get("interaction_route") == "resume_eligible_run"
    ]
    assert len(routed) == 1
    assert routed[0].metadata.root["input_kind"] == input_kind.value
    assert routed[0].metadata.root["source"] == source
    inbox = await restarted.inbox()
    assert len(inbox) == 2
    update_delivery = next(item for item in inbox if item.route == "resume_eligible_run")
    assert update_delivery.input_kind == input_kind.value
    assert update_delivery.delivery_count == 2
    assert update_delivery.status.value == "processed"
    assert all(item.status.value == "processed" for item in inbox), [
        (item.route, item.status.value, item.node_run_id) for item in inbox
    ]
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
    assert event_types.count("graph.result_reused") == 1
    assert event_types.count("graph.result_invalidated") == 0
    assert event_types.count("run.recovery_completed") == 1
    assert event_types.count("model.repair_requested") == 3


async def test_structural_update_reexecutes_only_invalidated_pure_result(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    original = parallel_update_graph()
    revised_values = original.model_dump(mode="json")
    revised_values["nodes"][2]["objective"] = (
        "Derive the result again under the supplemental structural requirement."
    )
    revised = TaskGraphSpec.model_validate(revised_values)
    initial_registry = registry(tmp_path)
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    route_turn("task_graph", original.model_dump(mode="json")),
                    *direct_action_turns('{"item":"prepared-once"}'),
                    *direct_action_turns('{"derived":"obsolete-result"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    background_turn(
                        "parallel-update",
                        "import time;from pathlib import Path;time.sleep(.15);"
                        "p=Path('parallel-update-count.txt');"
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
            content="Run two bounded branches and publish their joined result.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)

    active_final = '{"effect":"completed-once"}'
    published = '{"result":"replanned-result"}'
    restarted_registry = registry(tmp_path)
    restarted = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    update_turn("structural", revised.model_dump(mode="json")),
                    *direct_action_turns('{"derived":"recomputed-result"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    final_turn(active_final),
                    completion_turn(final_text=active_final),
                    *direct_action_turns(published),
                ],
            ),
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            route_evaluator=TaskRouteEvaluator(),
            response_repair_retries=0,
        )
    )

    result = await restarted.submit(
        supplemental(
            waiting.resume_key,
            "Recompute the independent derived branch with the new requirement.",
        )
    )

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "replanned-result"
    assert (tmp_path / "parallel-update-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
    assert aggregate is not None
    derived = [
        node
        for node in aggregate.nodes
        if node.metadata.root.get("graph_node_id") == "a_derive_result"
    ]
    assert [node.output for node in derived] == [
        {"derived": "obsolete-result"},
        {"derived": "recomputed-result"},
    ]
    invalidated = [
        event for event in aggregate.events if event.event_type == "graph.result_invalidated"
    ]
    assert [event.node_run_id for event in invalidated] == [derived[0].id]
    assert invalidated[0].metadata.root["will_reexecute"] is True
    assert invalidated[0].metadata.root["side_effect_detected"] is False
    reused = [event for event in aggregate.events if event.event_type == "graph.result_reused"]
    assert len(reused) == 1
    assert reused[0].metadata.root["graph_node_id"] == "prepare_input"
    observation = await ExecutionQueryService(factory).trace(waiting.run_id)
    assert observation.complete is True
    assert observation.inconsistencies == ()
    audit_types = [event.event_type for event in observation.audit]
    assert audit_types.count("graph.result_reused") == 1
    assert audit_types.count("graph.result_invalidated") == 1


async def test_structural_update_rejects_reexecution_of_completed_capability_result(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    original = parallel_update_graph()
    revised_values = original.model_dump(mode="json")
    revised_values["nodes"][2]["objective"] = (
        "Repeat the completed side effect under a changed objective."
    )
    revised = TaskGraphSpec.model_validate(revised_values)
    initial_registry = registry(tmp_path)
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    route_turn("task_graph", original.model_dump(mode="json")),
                    *direct_action_turns('{"item":"prepared-once"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    process_turn(
                        "completed-derived",
                        {
                            "command": sys.executable,
                            "args": [
                                "-c",
                                "from pathlib import Path;"
                                "p=Path('completed-result-count.txt');"
                                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
                            ],
                        },
                    ),
                    final_turn('{"derived":"side-effect-result"}'),
                    completion_turn(final_text='{"derived":"side-effect-result"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    background_turn(
                        "unsafe-update",
                        "import time;time.sleep(.15);print('active-complete')",
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
            content="Perform one completed effect while an independent branch remains active.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)
    restarted_registry = registry(tmp_path)
    restarted = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [update_turn("structural", revised.model_dump(mode="json"))],
            ),
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            route_evaluator=TaskRouteEvaluator(),
            response_repair_retries=0,
        )
    )

    with pytest.raises(AnbanError) as captured:
        await restarted.submit(
            supplemental(
                waiting.resume_key,
                "Change the already completed effect so it would have to execute again.",
            )
        )

    assert captured.value.info.details.root["reason"] == "graph_result_invalidation_unsafe"
    assert (tmp_path / "completed-result-count.txt").read_text() == "1"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        revisions = await unit.executions.list_graph_revisions(waiting.task_id)
        entries = await unit.executions.list_context_entries(ContextScope.TASK, waiting.task_id)
    assert aggregate is not None
    assert len(revisions) == 1
    assert aggregate.run.graph_revision_id == revisions[0].id
    assert entries == ()
    checkpoint = next(item for item in aggregate.checkpoints if item.id == waiting.checkpoint_id)
    assert checkpoint.status.value == "waiting"
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("graph.result_invalidation_rejected") == 1
    assert "graph.result_invalidated" not in event_types
    rejected = next(
        event
        for event in aggregate.events
        if event.event_type == "graph.result_invalidation_rejected"
    )
    assert rejected.metadata.root["will_reexecute"] is True
    assert rejected.metadata.root["side_effect_detected"] is True
    assert rejected.metadata.root["side_effect_replayed"] is False
    assert rejected.metadata.root["graph_revision_id"] is None


async def test_structural_update_rejects_changed_input_ancestry_of_active_action(
    tmp_path: Path,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    original = three_action_graph()
    revised_values = original.model_dump(mode="json")
    revised_values["nodes"][0]["objective"] = "Prepare a different input for active work."
    revised = TaskGraphSpec.model_validate(revised_values)
    initial_registry = registry(tmp_path)
    initial = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [
                    route_turn("task_graph", original.model_dump(mode="json")),
                    *direct_action_turns('{"seed":"stable-input"}'),
                    assessment_turn(ExecutionStrategy.USE_PROCESS, "process.execute"),
                    background_turn(
                        "active-ancestry",
                        "import time;time.sleep(.15);print('active-complete')",
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
            content="Run the graph using one prepared input.",
        )
    )
    assert isinstance(waiting, CorrelatedWaitingExecution)
    await initial.detach_async(waiting.checkpoint_id)
    restarted_registry = registry(tmp_path)
    restarted = InteractionService(
        PersistentRuntime(
            TransactionCheckingModel(
                factory,
                [update_turn("structural", revised.model_dump(mode="json"))],
            ),
            restarted_registry,
            factory,
            sufficiency=sufficiency(restarted_registry),
            route_evaluator=TaskRouteEvaluator(),
            response_repair_retries=0,
        )
    )

    with pytest.raises(AnbanError) as captured:
        await restarted.submit(
            supplemental(
                waiting.resume_key,
                "Change the input that the active action has already consumed.",
            )
        )

    assert captured.value.info.details.root["reason"] == "graph_result_invalidation_unsafe"
    async with factory() as unit:
        aggregate = await unit.executions.load_run(waiting.run_id)
        revisions = await unit.executions.list_graph_revisions(waiting.task_id)
    assert aggregate is not None
    assert len(revisions) == 1
    rejection = next(
        event
        for event in aggregate.events
        if event.event_type == "graph.result_invalidation_rejected"
    )
    assert rejection.node_run_id == waiting.node_run_id
    assert rejection.metadata.root["result_validity_reason"] == (
        "active_input_or_definition_changed"
    )
    assert rejection.metadata.root["side_effect_replayed"] is False
