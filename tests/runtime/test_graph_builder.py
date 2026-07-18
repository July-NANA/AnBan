"""Dynamic LangGraph construction across distinct validated topology shapes."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from anban.core import TaskGraphEdge, TaskGraphEdgeKind, TaskGraphNode, TaskGraphSpec
from anban.runtime import (
    DynamicTaskGraphBuilder,
    TaskGraphNodeAction,
    TaskGraphRouteAction,
    TaskGraphRuntimeState,
    TaskGraphStateUpdate,
)
from tests.core.test_graph import branch_graph, loop_graph, parallel_subgraph_graph


def node_actions(visited: list[str]) -> Callable[[TaskGraphNode], TaskGraphNodeAction]:
    def factory(node: TaskGraphNode) -> TaskGraphNodeAction:
        async def execute(_state: TaskGraphRuntimeState) -> TaskGraphStateUpdate:
            visited.append(node.id)
            return {"node_outputs": {node.id: {"kind": node.kind.value}}}

        return execute

    return factory


def control_routes(
    routed: list[str],
) -> Callable[[TaskGraphNode, tuple[TaskGraphEdge, ...]], TaskGraphRouteAction]:
    def factory(
        node: TaskGraphNode,
        edges: tuple[TaskGraphEdge, ...],
    ) -> TaskGraphRouteAction:
        routed.append(node.id)
        selected = next(
            (edge.target for edge in edges if edge.kind is TaskGraphEdgeKind.LOOP_EXIT),
            edges[0].target,
        )

        async def route(_state: TaskGraphRuntimeState) -> str:
            return selected

        return route

    return factory


@pytest.mark.parametrize(
    ("spec_factory", "expected_visited", "expected_router_count"),
    (
        (branch_graph, {"classify", "choose_path", "fast_path"}, 1),
        (loop_graph, {"seed", "bounded_loop", "finish"}, 1),
        (
            parallel_subgraph_graph,
            {
                "prepare",
                "fan_out",
                "nested_branch",
                "direct_branch",
                "join_results",
                "publish",
            },
            0,
        ),
    ),
)
async def test_one_builder_compiles_and_runs_distinct_real_langgraphs(
    spec_factory: Callable[[], TaskGraphSpec],
    expected_visited: set[str],
    expected_router_count: int,
) -> None:
    spec = spec_factory()
    visited: list[str] = []
    routed: list[str] = []

    compiled = DynamicTaskGraphBuilder().compile(
        spec,
        node_action_factory=node_actions(visited),
        route_action_factory=control_routes(routed),
    )
    result = await compiled.graph.ainvoke({"graph_input": {}, "node_outputs": {}})

    assert set(visited) == expected_visited
    assert set(result["node_outputs"]) == expected_visited
    assert len(routed) == expected_router_count
    assert compiled.spec_hash
    assert {node.id for node in spec.nodes}.issubset(
        {source for source, _, _ in compiled.graph_edges()}
        | {target for _, target, _ in compiled.graph_edges()}
    )


def test_compiled_edges_preserve_conditional_and_join_semantics() -> None:
    branch = DynamicTaskGraphBuilder().compile(
        branch_graph(),
        node_action_factory=node_actions([]),
        route_action_factory=control_routes([]),
    )
    parallel = DynamicTaskGraphBuilder().compile(
        parallel_subgraph_graph(),
        node_action_factory=node_actions([]),
    )

    assert {
        (source, target) for source, target, conditional in branch.graph_edges() if conditional
    } == {("choose_path", "fast_path"), ("choose_path", "careful_path")}
    assert {
        source
        for source, target, conditional in parallel.graph_edges()
        if target == "join_results" and not conditional
    } == {"nested_branch", "direct_branch"}


def test_control_graph_fails_closed_without_real_routing() -> None:
    with pytest.raises(ValueError, match="control routing is required"):
        DynamicTaskGraphBuilder().compile(
            branch_graph(),
            node_action_factory=node_actions([]),
        )
