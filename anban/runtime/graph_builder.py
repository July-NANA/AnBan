"""Generic LangGraph construction from validated TaskGraphSpec data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import JsonValue

from anban.core import (
    TaskGraphEdge,
    TaskGraphEdgeKind,
    TaskGraphNode,
    TaskGraphSpec,
    task_graph_spec_hash,
)


def _merge_node_outputs(
    current: dict[str, dict[str, JsonValue]],
    update: dict[str, dict[str, JsonValue]],
) -> dict[str, dict[str, JsonValue]]:
    """Merge parallel results and retain the latest output from a loop iteration."""

    return {**current, **update}


class TaskGraphRuntimeState(TypedDict):
    """Shared state envelope for dynamically compiled Task graphs."""

    graph_input: dict[str, JsonValue]
    node_outputs: Annotated[dict[str, dict[str, JsonValue]], _merge_node_outputs]


class TaskGraphStateUpdate(TypedDict, total=False):
    """Bounded state update returned by one injected graph node action."""

    node_outputs: dict[str, dict[str, JsonValue]]


TaskGraphNodeAction = Callable[
    [TaskGraphRuntimeState],
    TaskGraphStateUpdate | Awaitable[TaskGraphStateUpdate],
]
TaskGraphNodeActionFactory = Callable[[TaskGraphNode], TaskGraphNodeAction]
TaskGraphRouteAction = Callable[
    [TaskGraphRuntimeState],
    str | Awaitable[str],
]
TaskGraphRouteActionFactory = Callable[
    [TaskGraphNode, tuple[TaskGraphEdge, ...]],
    TaskGraphRouteAction,
]


@dataclass(frozen=True, slots=True)
class CompiledTaskGraph:
    """A real compiled LangGraph plus stable source-spec evidence."""

    spec_hash: str
    graph: CompiledStateGraph[TaskGraphRuntimeState]

    def graph_edges(self) -> tuple[tuple[str, str, bool], ...]:
        """Expose compiled topology without introducing another scheduler."""

        return tuple(
            (edge.source, edge.target, edge.conditional) for edge in self.graph.get_graph().edges
        )


class DynamicTaskGraphBuilder:
    """Compile every validated TaskGraphSpec through one topology-independent path."""

    def compile(
        self,
        spec: TaskGraphSpec,
        *,
        node_action_factory: TaskGraphNodeActionFactory,
        route_action_factory: TaskGraphRouteActionFactory | None = None,
    ) -> CompiledTaskGraph:
        """Register nodes and control edges dynamically, then compile one LangGraph."""

        builder = StateGraph(TaskGraphRuntimeState)
        nodes = {node.id: node for node in spec.nodes}
        for node in spec.nodes:
            builder.add_node(node.id, node_action_factory(node))

        builder.add_edge(START, spec.entry_node)
        join_sources: dict[str, list[str]] = {}
        control_edges: dict[str, list[TaskGraphEdge]] = {}
        direct_kinds = {
            TaskGraphEdgeKind.SEQUENCE,
            TaskGraphEdgeKind.LOOP_BACK,
            TaskGraphEdgeKind.PARALLEL,
        }
        control_kinds = {
            TaskGraphEdgeKind.BRANCH,
            TaskGraphEdgeKind.LOOP_BODY,
            TaskGraphEdgeKind.LOOP_EXIT,
        }
        for edge in spec.edges:
            if edge.kind in direct_kinds:
                builder.add_edge(edge.source, edge.target)
            elif edge.kind is TaskGraphEdgeKind.JOIN:
                join_sources.setdefault(edge.target, []).append(edge.source)
            elif edge.kind in control_kinds:
                control_edges.setdefault(edge.source, []).append(edge)

        for target, sources in join_sources.items():
            builder.add_edge(sources, target)

        if control_edges and route_action_factory is None:
            raise ValueError("Task graph control routing is required")
        if route_action_factory is not None:
            for source, edges in control_edges.items():
                ordered_edges = tuple(edges)
                builder.add_conditional_edges(
                    source,
                    route_action_factory(nodes[source], ordered_edges),
                    {edge.target: edge.target for edge in ordered_edges},
                )

        for terminal in spec.terminal_nodes:
            builder.add_edge(terminal, END)

        spec_hash = task_graph_spec_hash(spec)
        graph = builder.compile(name=f"anban_task_graph_{spec_hash[:16]}")
        return CompiledTaskGraph(spec_hash=spec_hash, graph=graph)
