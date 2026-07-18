"""TaskGraphSpec invariants across distinct unseen topology shapes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from anban.core import (
    TaskGraphBudget,
    TaskGraphCondition,
    TaskGraphConditionOperator,
    TaskGraphEdge,
    TaskGraphEdgeKind,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
    TaskGraphValidationReason,
    TaskGraphValueBinding,
    TaskGraphValueSource,
)


def graph_input(key: str) -> TaskGraphValueBinding:
    return TaskGraphValueBinding(source=TaskGraphValueSource.GRAPH_INPUT, key=key)


def node_output(
    node_id: str,
    key: str,
    *,
    fallback_value: JsonValue | None = None,
) -> TaskGraphValueBinding:
    return TaskGraphValueBinding(
        source=TaskGraphValueSource.NODE_OUTPUT,
        node_id=node_id,
        key=key,
        fallback_value=fallback_value,
    )


def action(
    node_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    inputs: dict[str, TaskGraphValueBinding] | None = None,
    outputs: tuple[str, ...] = (),
    marker: str | None = None,
) -> TaskGraphNode:
    return TaskGraphNode(
        id=node_id,
        kind=TaskGraphNodeKind.ACTION,
        objective=f"Execute bounded objective {marker or uuid4().hex}.",
        dependencies=dependencies,
        inputs=inputs or {},
        outputs=outputs,
    )


def branch_graph() -> TaskGraphSpec:
    marker = uuid4().hex
    nodes = (
        action(
            "classify",
            inputs={"payload": graph_input("payload")},
            outputs=("route",),
            marker=marker,
        ),
        TaskGraphNode(
            id="choose_path",
            kind=TaskGraphNodeKind.BRANCH,
            dependencies=("classify",),
            inputs={"route": node_output("classify", "route")},
            condition_input="route",
        ),
        action(
            "fast_path",
            dependencies=("choose_path",),
            inputs={"route": node_output("classify", "route")},
            outputs=("result",),
            marker=marker,
        ),
        action(
            "careful_path",
            dependencies=("choose_path",),
            inputs={"route": node_output("classify", "route")},
            outputs=("result",),
            marker=marker,
        ),
    )
    edges = (
        TaskGraphEdge(source="classify", target="choose_path"),
        TaskGraphEdge(
            source="choose_path",
            target="fast_path",
            kind=TaskGraphEdgeKind.BRANCH,
            condition=TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.EQUALS,
                compare_value="fast",
            ),
        ),
        TaskGraphEdge(
            source="choose_path",
            target="careful_path",
            kind=TaskGraphEdgeKind.BRANCH,
            condition=TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.NOT_EQUALS,
                compare_value="fast",
            ),
        ),
    )
    return TaskGraphSpec(
        input_keys=("payload",),
        nodes=nodes,
        edges=edges,
        entry_node="classify",
        terminal_nodes=("fast_path", "careful_path"),
        outputs={
            "fast_result": node_output("fast_path", "result"),
            "careful_result": node_output("careful_path", "result"),
        },
    )


def loop_graph() -> TaskGraphSpec:
    marker = uuid4().hex
    nodes = (
        action("seed", outputs=("seed_value",), marker=marker),
        TaskGraphNode(
            id="bounded_loop",
            kind=TaskGraphNodeKind.LOOP,
            dependencies=("seed",),
            inputs={"continue": node_output("iterate", "continue", fallback_value=True)},
            outputs=("latest_result",),
            condition_input="continue",
            max_iterations=5,
        ),
        action(
            "iterate",
            dependencies=("bounded_loop",),
            inputs={"seed_value": node_output("seed", "seed_value")},
            outputs=("continue", "result"),
            marker=marker,
        ),
        action(
            "finish",
            dependencies=("bounded_loop",),
            inputs={"result": node_output("bounded_loop", "latest_result")},
            outputs=("result",),
            marker=marker,
        ),
    )
    return TaskGraphSpec(
        nodes=nodes,
        edges=(
            TaskGraphEdge(source="seed", target="bounded_loop"),
            TaskGraphEdge(
                source="bounded_loop",
                target="iterate",
                kind=TaskGraphEdgeKind.LOOP_BODY,
            ),
            TaskGraphEdge(
                source="iterate",
                target="bounded_loop",
                kind=TaskGraphEdgeKind.LOOP_BACK,
            ),
            TaskGraphEdge(
                source="bounded_loop",
                target="finish",
                kind=TaskGraphEdgeKind.LOOP_EXIT,
            ),
        ),
        entry_node="seed",
        terminal_nodes=("finish",),
        outputs={"result": node_output("finish", "result")},
        budget=TaskGraphBudget(max_loop_iterations=7),
    )


def parallel_subgraph_graph() -> TaskGraphSpec:
    marker = uuid4().hex
    nested = TaskGraphSpec(
        input_keys=("item",),
        nodes=(
            action(
                "nested_action",
                inputs={"item": graph_input("item")},
                outputs=("result",),
                marker=marker,
            ),
        ),
        entry_node="nested_action",
        terminal_nodes=("nested_action",),
        outputs={"result": node_output("nested_action", "result")},
    )
    nodes = (
        action("prepare", outputs=("item",), marker=marker),
        TaskGraphNode(
            id="fan_out",
            kind=TaskGraphNodeKind.PARALLEL,
            dependencies=("prepare",),
        ),
        TaskGraphNode(
            id="nested_branch",
            kind=TaskGraphNodeKind.SUBGRAPH,
            dependencies=("fan_out",),
            inputs={"item": node_output("prepare", "item")},
            outputs=("result",),
            subgraph=nested,
        ),
        action(
            "direct_branch",
            dependencies=("fan_out",),
            inputs={"item": node_output("prepare", "item")},
            outputs=("result",),
            marker=marker,
        ),
        TaskGraphNode(
            id="join_results",
            kind=TaskGraphNodeKind.JOIN,
            dependencies=("nested_branch", "direct_branch"),
            inputs={
                "nested": node_output("nested_branch", "result"),
                "direct": node_output("direct_branch", "result"),
            },
        ),
        action(
            "publish",
            dependencies=("join_results",),
            inputs={
                "nested": node_output("nested_branch", "result"),
                "direct": node_output("direct_branch", "result"),
            },
            outputs=("result",),
            marker=marker,
        ),
    )
    return TaskGraphSpec(
        nodes=nodes,
        edges=(
            TaskGraphEdge(source="prepare", target="fan_out"),
            TaskGraphEdge(
                source="fan_out",
                target="nested_branch",
                kind=TaskGraphEdgeKind.PARALLEL,
            ),
            TaskGraphEdge(
                source="fan_out",
                target="direct_branch",
                kind=TaskGraphEdgeKind.PARALLEL,
            ),
            TaskGraphEdge(
                source="nested_branch",
                target="join_results",
                kind=TaskGraphEdgeKind.JOIN,
            ),
            TaskGraphEdge(
                source="direct_branch",
                target="join_results",
                kind=TaskGraphEdgeKind.JOIN,
            ),
            TaskGraphEdge(source="join_results", target="publish"),
        ),
        entry_node="prepare",
        terminal_nodes=("publish",),
        outputs={"result": node_output("publish", "result")},
        budget=TaskGraphBudget(max_parallelism=2),
    )


@pytest.mark.parametrize("factory", [branch_graph, loop_graph, parallel_subgraph_graph])
def test_three_distinct_graph_topologies_validate_and_round_trip(
    factory: Callable[[], TaskGraphSpec],
) -> None:
    graph = factory()

    restored = TaskGraphSpec.model_validate_json(graph.model_dump_json())

    assert restored == graph
    assert restored.version == "1"
    assert restored.entry_node in {node.id for node in restored.nodes}
    assert set(restored.terminal_nodes) <= {node.id for node in restored.nodes}


def test_contracts_are_closed_and_immutable() -> None:
    graph = branch_graph()
    payload = graph.model_dump(mode="json")
    payload["unknown_execution_switch"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        TaskGraphSpec.model_validate(payload)
    with pytest.raises(ValidationError, match="frozen"):
        graph.entry_node = "careful_path"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("duplicate_node", TaskGraphValidationReason.DUPLICATE_NODE),
        ("unknown_edge", TaskGraphValidationReason.EDGE_NODE_UNKNOWN),
        ("dependency_mismatch", TaskGraphValidationReason.DEPENDENCY_EDGE_MISMATCH),
        ("unknown_output", TaskGraphValidationReason.BINDING_INVALID),
    ],
)
def test_invalid_references_fail_with_stable_semantic_reason(
    case: str,
    reason: TaskGraphValidationReason,
) -> None:
    payload: dict[str, Any] = branch_graph().model_dump(mode="json")
    if case == "duplicate_node":
        payload["nodes"].append(payload["nodes"][0])
    elif case == "unknown_edge":
        payload["edges"][0]["target"] = "unknown_node"
    elif case == "dependency_mismatch":
        payload["nodes"][1]["dependencies"] = []
    else:
        payload["nodes"][2]["inputs"]["route"]["key"] = "unknown_output"

    with pytest.raises(ValidationError, match=reason.value):
        TaskGraphSpec.model_validate(payload)


def test_sequence_cycle_cannot_masquerade_as_a_bounded_loop() -> None:
    nodes = (
        action("start", outputs=("value",)),
        action("first", dependencies=("start", "second"), outputs=("value",)),
        action("second", dependencies=("first",), outputs=("value",)),
        action("finish", dependencies=("second",), outputs=("value",)),
    )

    with pytest.raises(ValidationError, match=TaskGraphValidationReason.UNCONTROLLED_CYCLE.value):
        TaskGraphSpec(
            nodes=nodes,
            edges=(
                TaskGraphEdge(source="start", target="first"),
                TaskGraphEdge(source="first", target="second"),
                TaskGraphEdge(source="second", target="first"),
                TaskGraphEdge(source="second", target="finish"),
            ),
            entry_node="start",
            terminal_nodes=("finish",),
        )


def test_branch_requires_distinct_conditional_routes() -> None:
    payload = branch_graph().model_dump(mode="json")
    payload["edges"][2]["condition"] = payload["edges"][1]["condition"]

    with pytest.raises(
        ValidationError, match=TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value
    ):
        TaskGraphSpec.model_validate(payload)


def test_loop_budget_must_fit_inside_graph_budget() -> None:
    payload = loop_graph().model_dump(mode="json")
    payload["budget"]["max_loop_iterations"] = 4

    with pytest.raises(
        ValidationError, match=TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value
    ):
        TaskGraphSpec.model_validate(payload)


def test_parallel_width_cannot_exceed_graph_budget() -> None:
    payload = parallel_subgraph_graph().model_dump(mode="json")
    payload["budget"]["max_parallelism"] = 1

    with pytest.raises(
        ValidationError, match=TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value
    ):
        TaskGraphSpec.model_validate(payload)


def test_unreachable_node_and_dead_end_are_rejected() -> None:
    payload = branch_graph().model_dump(mode="json")
    payload["nodes"].append(action("orphan", outputs=("result",)).model_dump(mode="json"))

    with pytest.raises(ValidationError, match=TaskGraphValidationReason.UNREACHABLE_NODE.value):
        TaskGraphSpec.model_validate(payload)


def test_condition_operator_shapes_are_closed() -> None:
    with pytest.raises(ValidationError, match="cannot carry"):
        TaskGraphCondition(
            input_name="route",
            operator=TaskGraphConditionOperator.TRUTHY,
            compare_value=True,
        )
    with pytest.raises(ValidationError, match="requires a number"):
        TaskGraphCondition(
            input_name="count",
            operator=TaskGraphConditionOperator.GREATER_THAN,
            compare_value="ten",
        )
