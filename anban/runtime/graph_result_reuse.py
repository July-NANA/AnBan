"""Conservative result validity across immutable Task graph revisions."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field

from anban.core.graph import TaskGraphEdge, TaskGraphSpec
from anban.core.ids import NodeRunId
from anban.core.models import CapabilityInvocation, NodeRun, NodeRunStatus
from anban.runtime.contracts import RuntimeValue


class GraphResultDisposition(StrEnum):
    """How one concrete completed NodeRun relates to a replacement graph."""

    REUSED = "reused"
    INVALIDATED = "invalidated"


class GraphResultValidityReason(StrEnum):
    """Stable, audit-safe reasons for a reuse decision."""

    UNCHANGED = "definition_and_inputs_unchanged"
    NODE_CHANGED = "node_changed"
    NODE_REMOVED = "node_removed"
    DEPENDENCY_CHANGED = "dependency_changed"
    UPSTREAM_CHANGED = "upstream_changed"


class GraphResultDecision(RuntimeValue):
    """One result occurrence, independent of repeated graph node execution."""

    node_run_id: NodeRunId
    graph_node_id: str = Field(min_length=1, max_length=64)
    disposition: GraphResultDisposition
    reason: GraphResultValidityReason
    will_reexecute: bool
    side_effect_detected: bool


class GraphResultPlan(RuntimeValue):
    """Closed validity plan applied atomically with one graph revision."""

    decisions: tuple[GraphResultDecision, ...]
    active_graph_node_id: str = Field(min_length=1, max_length=64)
    active_node_stable: bool
    unsafe_reexecution: bool

    @property
    def accepted(self) -> bool:
        return self.active_node_stable and not self.unsafe_reexecution


class GraphResultReuseEvaluator:
    """Compare graph semantics without relying on prompts or scenario literals."""

    def plan(
        self,
        current: TaskGraphSpec,
        replacement: TaskGraphSpec,
        nodes: tuple[NodeRun, ...],
        invocations: tuple[CapabilityInvocation, ...],
        active_graph_node_id: str,
        invalidated_node_run_ids: frozenset[NodeRunId] = frozenset(),
    ) -> GraphResultPlan:
        stable, reasons = self._stable_nodes(current, replacement)
        replacement_ids = {node.id for node in replacement.nodes}
        side_effect_nodes = {invocation.node_run_id for invocation in invocations}
        decisions: list[GraphResultDecision] = []
        for node in nodes:
            graph_node_id = node.metadata.root.get("graph_node_id")
            if (
                node.id in invalidated_node_run_ids
                or node.status is not NodeRunStatus.SUCCEEDED
                or node.output is None
                or not isinstance(graph_node_id, str)
            ):
                continue
            reused = graph_node_id in stable
            decisions.append(
                GraphResultDecision(
                    node_run_id=node.id,
                    graph_node_id=graph_node_id,
                    disposition=(
                        GraphResultDisposition.REUSED
                        if reused
                        else GraphResultDisposition.INVALIDATED
                    ),
                    reason=(
                        GraphResultValidityReason.UNCHANGED
                        if reused
                        else reasons.get(
                            graph_node_id,
                            GraphResultValidityReason.NODE_REMOVED,
                        )
                    ),
                    will_reexecute=not reused and graph_node_id in replacement_ids,
                    side_effect_detected=node.id in side_effect_nodes,
                )
            )
        return GraphResultPlan(
            decisions=tuple(decisions),
            active_graph_node_id=active_graph_node_id,
            active_node_stable=active_graph_node_id in stable,
            unsafe_reexecution=any(
                decision.disposition is GraphResultDisposition.INVALIDATED
                and decision.will_reexecute
                and decision.side_effect_detected
                for decision in decisions
            ),
        )

    @classmethod
    def _stable_nodes(
        cls,
        current: TaskGraphSpec,
        replacement: TaskGraphSpec,
    ) -> tuple[set[str], dict[str, GraphResultValidityReason]]:
        current_nodes = {node.id: node for node in current.nodes}
        replacement_nodes = {node.id: node for node in replacement.nodes}
        current_incoming = cls._incoming(current)
        replacement_incoming = cls._incoming(replacement)
        stable: set[str] = set()
        reasons: dict[str, GraphResultValidityReason] = {}
        for node_id, node in current_nodes.items():
            revised = replacement_nodes.get(node_id)
            if revised is None:
                reasons[node_id] = GraphResultValidityReason.NODE_REMOVED
            elif revised != node:
                reasons[node_id] = GraphResultValidityReason.NODE_CHANGED
            elif current_incoming[node_id] != replacement_incoming[node_id]:
                reasons[node_id] = GraphResultValidityReason.DEPENDENCY_CHANGED
            else:
                stable.add(node_id)

        changed = True
        while changed:
            changed = False
            for node_id in tuple(stable):
                if any(source not in stable for source in cls._incoming_sources(current, node_id)):
                    stable.remove(node_id)
                    reasons[node_id] = GraphResultValidityReason.UPSTREAM_CHANGED
                    changed = True
        return stable, reasons

    @staticmethod
    def _incoming(spec: TaskGraphSpec) -> dict[str, frozenset[str]]:
        incoming = {node.id: set[str]() for node in spec.nodes}
        for edge in spec.edges:
            incoming[edge.target].add(GraphResultReuseEvaluator._edge_key(edge))
        return {node_id: frozenset(edges) for node_id, edges in incoming.items()}

    @staticmethod
    def _incoming_sources(spec: TaskGraphSpec, node_id: str) -> frozenset[str]:
        return frozenset(edge.source for edge in spec.edges if edge.target == node_id)

    @staticmethod
    def _edge_key(edge: TaskGraphEdge) -> str:
        return json.dumps(
            edge.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
