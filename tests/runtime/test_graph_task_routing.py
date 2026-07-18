"""Durable Runtime routing between the fixed Agent and Task graph paths."""

from __future__ import annotations

from anban.capability import CapabilityRegistry
from anban.core import TaskGraphSpec
from anban.runtime import (
    AgentOutcomeStatus,
    ExecutionQueryService,
    PersistentRuntime,
    TaskExecutionRoute,
    TaskRouteEvaluator,
)
from tests.core.test_graph import action, node_output
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_graph_routing import route_turn
from tests.runtime.test_persistent_runtime import TransactionCheckingModel, final_turn, load_run


def one_action_graph() -> TaskGraphSpec:
    node = action("work_unit", outputs=("result",))
    return TaskGraphSpec(
        nodes=(node,),
        entry_node=node.id,
        terminal_nodes=(node.id,),
        outputs={"result": node_output(node.id, "result")},
    )


async def test_simple_route_preserves_fixed_agent_and_has_no_revision() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [
            route_turn(TaskExecutionRoute.FIXED_AGENT.value),
            final_turn("Fixed path final."),
        ],
    )

    result = await PersistentRuntime(
        model,
        CapabilityRegistry(),
        factory,
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    ).execute("Complete one simple bounded request.")

    aggregate = await load_run(factory, result.run_id)
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "Fixed path final."
    assert result.outcome.model_turn_count == 2
    assert aggregate.run.graph_revision_id is None
    assert aggregate.graph_revision is None
    assert [node.node_name for node in aggregate.nodes] == ["general_agent"]
    route = next(event for event in aggregate.events if event.event_type == "agent.route_selected")
    assert route.metadata.root["route"] == TaskExecutionRoute.FIXED_AGENT.value
    assert route.metadata.root["graph_selected"] is False
    assert len(str(route.metadata.root["rationale_hash"])) == 64
    assert not any(event.event_type.startswith("graph.") for event in aggregate.events)


async def test_complex_route_persists_revision_and_executes_graph_nodes() -> None:
    factory = MemoryUnitOfWorkFactory()
    spec = one_action_graph()
    model = TransactionCheckingModel(
        factory,
        [
            route_turn(TaskExecutionRoute.TASK_GRAPH.value, spec.model_dump(mode="json")),
            final_turn('{"result":"Graph path final."}'),
        ],
    )

    result = await PersistentRuntime(
        model,
        CapabilityRegistry(),
        factory,
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    ).execute("Complete one structured graph request.")

    aggregate = await load_run(factory, result.run_id)
    restarted_detail = await ExecutionQueryService(factory).show(result.run_id)
    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.final_text == "Graph path final."
    assert result.outcome.model_turn_count == 2
    assert aggregate.graph_revision is not None
    assert aggregate.run.graph_revision_id == aggregate.graph_revision.id
    assert aggregate.graph_revision.spec == spec
    assert restarted_detail.graph_revision is not None
    assert restarted_detail.graph_revision.spec_hash == aggregate.graph_revision.spec_hash
    assert [node.node_name for node in aggregate.nodes] == ["general_agent", "work_unit"]
    assert all(node.status.value == "succeeded" for node in aggregate.nodes)
    assert {event.event_type for event in aggregate.events} >= {
        "agent.route_selected",
        "graph.revision_created",
        "run.graph_revision_linked",
        "run.succeeded",
        "run.final",
    }
    audit_types = {entry.event_type for entry in restarted_detail.observability.audit}
    assert {
        "agent.route_selected",
        "graph.revision_created",
        "run.graph_revision_linked",
    } <= audit_types
    route_audit = next(
        entry
        for entry in restarted_detail.observability.audit
        if entry.event_type == "agent.route_selected"
    )
    assert route_audit.metadata.root["route"] == TaskExecutionRoute.TASK_GRAPH.value
    assert route_audit.metadata.root["graph_spec_hash"] == aggregate.graph_revision.spec_hash
    assert tuple(event.sequence for event in aggregate.events) == tuple(
        range(1, len(aggregate.events) + 1)
    )


async def test_invalid_graph_action_output_is_durable_failure_not_fallback_success() -> None:
    factory = MemoryUnitOfWorkFactory()
    spec = one_action_graph()
    model = TransactionCheckingModel(
        factory,
        [
            route_turn(TaskExecutionRoute.TASK_GRAPH.value, spec.model_dump(mode="json")),
            final_turn("not a JSON action result"),
        ],
    )

    result = await PersistentRuntime(
        model,
        CapabilityRegistry(),
        factory,
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    ).execute("Keep invalid graph execution explicit.")

    aggregate = await load_run(factory, result.run_id)
    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.details.root["reason"] == "action_output_invalid"
    assert aggregate.graph_revision is not None
    assert [node.status.value for node in aggregate.nodes] == ["succeeded", "failed"]
    assert aggregate.run.status.value == "failed"
    assert aggregate.task.status.value == "failed"
    assert any(event.event_type == "run.error" for event in aggregate.events)
    assert not any(event.event_type == "run.final" for event in aggregate.events)


async def test_invalid_route_response_is_a_durable_root_failure() -> None:
    factory = MemoryUnitOfWorkFactory()
    invalid = route_turn(
        TaskExecutionRoute.FIXED_AGENT.value,
        one_action_graph().model_dump(mode="json"),
    )

    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [invalid]),
        CapabilityRegistry(),
        factory,
        route_evaluator=TaskRouteEvaluator(),
        response_repair_retries=0,
    ).execute("Reject an invalid Main Agent route.")

    aggregate = await load_run(factory, result.run_id)
    assert result.persisted is True
    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code.value == "model_response_invalid"
    assert aggregate.run.graph_revision_id is None
    assert aggregate.nodes[0].status.value == "failed"
    assert aggregate.run.status.value == "failed"
    assert not any(event.event_type == "agent.route_selected" for event in aggregate.events)
