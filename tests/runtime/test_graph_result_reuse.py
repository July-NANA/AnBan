"""Result validity across graph revisions is conservative and occurrence-specific."""

from __future__ import annotations

from anban.core import SafeMetadata, TaskGraphSpec
from anban.core.ids import (
    ExecutionRunId,
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from anban.core.models import CapabilityInvocation, NodeRun, NodeRunStatus
from anban.runtime.graph_result_reuse import (
    GraphResultDisposition,
    GraphResultReuseEvaluator,
    GraphResultValidityReason,
)
from tests.runtime.test_recovery import three_action_graph


def completed_node(run_id: ExecutionRunId, graph_node_id: str) -> NodeRun:
    return NodeRun(
        id=new_node_run_id(),
        run_id=run_id,
        node_name=graph_node_id,
        status=NodeRunStatus.SUCCEEDED,
        output={"value": graph_node_id},
        metadata=SafeMetadata({"graph_node_id": graph_node_id}),
    )


def revised_objective(current: TaskGraphSpec, index: int) -> TaskGraphSpec:
    values = current.model_dump(mode="json")
    values["nodes"][index]["objective"] = "Use a genuinely revised bounded objective."
    return TaskGraphSpec.model_validate(values)


def test_unchanged_results_are_reused_per_concrete_node_run() -> None:
    current = three_action_graph()
    revised = revised_objective(current, 2)
    run_id = new_execution_run_id()
    first = completed_node(run_id, "prepare_data")

    plan = GraphResultReuseEvaluator().plan(
        current,
        revised,
        (first,),
        (),
        "perform_effect",
    )

    assert plan.accepted
    assert plan.decisions[0].node_run_id == first.id
    assert plan.decisions[0].disposition is GraphResultDisposition.REUSED
    assert plan.decisions[0].reason is GraphResultValidityReason.UNCHANGED


def test_changed_ancestor_transitively_invalidates_downstream_results_and_active_node() -> None:
    current = three_action_graph()
    revised = revised_objective(current, 0)
    run_id = new_execution_run_id()
    downstream = completed_node(run_id, "perform_effect")

    plan = GraphResultReuseEvaluator().plan(
        current,
        revised,
        (downstream,),
        (),
        "publish_result",
    )

    assert not plan.accepted
    assert not plan.active_node_stable
    assert plan.decisions[0].disposition is GraphResultDisposition.INVALIDATED
    assert plan.decisions[0].reason is GraphResultValidityReason.UPSTREAM_CHANGED


def test_changed_side_effect_result_that_would_reexecute_is_unsafe() -> None:
    current = three_action_graph()
    revised = revised_objective(current, 2)
    run_id = new_execution_run_id()
    result = completed_node(run_id, "publish_result")
    invocation = CapabilityInvocation(
        id=new_capability_invocation_id(),
        run_id=run_id,
        node_run_id=result.id,
        capability_name="process.execute",
    )

    plan = GraphResultReuseEvaluator().plan(
        current,
        revised,
        (result,),
        (invocation,),
        "perform_effect",
    )

    assert not plan.accepted
    assert plan.active_node_stable
    assert plan.unsafe_reexecution
    assert plan.decisions[0].will_reexecute
    assert plan.decisions[0].side_effect_detected


def test_removed_side_effect_result_is_invalidated_without_reexecution() -> None:
    current = three_action_graph()
    values = current.model_dump(mode="json")
    values["nodes"] = values["nodes"][:2]
    values["edges"] = values["edges"][:1]
    values["terminal_nodes"] = ["perform_effect"]
    values["outputs"] = {
        "middle": {
            "source": "node_output",
            "key": "middle",
            "node_id": "perform_effect",
            "fallback_value": None,
        }
    }
    revised = TaskGraphSpec.model_validate(values)
    run_id = new_execution_run_id()
    removed = completed_node(run_id, "publish_result")
    invocation = CapabilityInvocation(
        id=new_capability_invocation_id(),
        run_id=run_id,
        node_run_id=removed.id,
        capability_name="process.execute",
    )

    plan = GraphResultReuseEvaluator().plan(
        current,
        revised,
        (removed,),
        (invocation,),
        "perform_effect",
    )

    assert plan.accepted
    assert plan.decisions[0].reason is GraphResultValidityReason.NODE_REMOVED
    assert not plan.decisions[0].will_reexecute
    assert not plan.unsafe_reexecution


def test_previously_invalidated_occurrence_is_not_reconsidered() -> None:
    current = three_action_graph()
    revised = revised_objective(current, 2)
    run_id = new_execution_run_id()
    old = completed_node(run_id, "publish_result")

    plan = GraphResultReuseEvaluator().plan(
        current,
        revised,
        (old,),
        (),
        "perform_effect",
        frozenset({old.id}),
    )

    assert plan.accepted
    assert plan.decisions == ()
