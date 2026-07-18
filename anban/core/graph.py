"""Authoritative, executable-data contracts for bounded Task graphs."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from anban.core.ids import GraphRevisionId, TaskId, new_graph_revision_id
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import DomainModel, UtcDateTime, now_utc

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TaskGraphNodeKind(StrEnum):
    """Closed node semantics understood by the later generic graph builder."""

    ACTION = "action"
    BRANCH = "branch"
    LOOP = "loop"
    PARALLEL = "parallel"
    JOIN = "join"
    SUBGRAPH = "subgraph"


class TaskGraphEdgeKind(StrEnum):
    """Explicit control-flow meanings; loop cycles cannot hide in sequence edges."""

    SEQUENCE = "sequence"
    BRANCH = "branch"
    LOOP_BODY = "loop_body"
    LOOP_EXIT = "loop_exit"
    LOOP_BACK = "loop_back"
    PARALLEL = "parallel"
    JOIN = "join"


class TaskGraphValueSource(StrEnum):
    GRAPH_INPUT = "graph_input"
    NODE_OUTPUT = "node_output"


class TaskGraphConditionOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    TRUTHY = "truthy"
    FALSY = "falsy"


class TaskGraphValidationReason(StrEnum):
    """Stable semantic failure reasons safe for model repair and Audit projection."""

    DUPLICATE_NODE = "duplicate_node"
    DUPLICATE_EDGE = "duplicate_edge"
    ENTRY_UNKNOWN = "entry_unknown"
    TERMINAL_UNKNOWN = "terminal_unknown"
    EDGE_NODE_UNKNOWN = "edge_node_unknown"
    EDGE_KIND_INVALID = "edge_kind_invalid"
    DEPENDENCY_UNKNOWN = "dependency_unknown"
    DEPENDENCY_EDGE_MISMATCH = "dependency_edge_mismatch"
    BINDING_INVALID = "binding_invalid"
    OUTPUT_INVALID = "output_invalid"
    CONTROL_SHAPE_INVALID = "control_shape_invalid"
    UNCONTROLLED_CYCLE = "uncontrolled_cycle"
    LOOP_BACK_INVALID = "loop_back_invalid"
    UNREACHABLE_NODE = "unreachable_node"
    NO_TERMINAL_PATH = "no_terminal_path"
    BUDGET_EXCEEDED = "budget_exceeded"


class GraphRevisionStatus(StrEnum):
    """An immutable revision exists only after its complete spec validates."""

    VALIDATED = "validated"


class TaskGraphValueBinding(DomainModel):
    """One graph input or prior node output bound into a node input."""

    source: TaskGraphValueSource
    key: str = Field(min_length=1, max_length=64)
    node_id: str | None = Field(default=None, min_length=1, max_length=64)
    fallback_value: JsonValue | None = None

    @field_validator("key", "node_id")
    @classmethod
    def validate_names(cls, value: str | None) -> str | None:
        if value is not None and _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Task graph value name is invalid")
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> Self:
        if self.source is TaskGraphValueSource.GRAPH_INPUT:
            if self.node_id is not None or self.fallback_value is not None:
                raise ValueError("Graph input binding cannot select a node or fallback")
        elif self.node_id is None:
            raise ValueError("Node output binding requires a node identity")
        return self


class TaskGraphCondition(DomainModel):
    """Closed comparison over one named input of a control node."""

    input_name: str = Field(min_length=1, max_length=64)
    operator: TaskGraphConditionOperator
    compare_value: JsonValue | None = None

    @field_validator("input_name")
    @classmethod
    def validate_input_name(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Task graph condition input is invalid")
        return value

    @model_validator(mode="after")
    def validate_operator_shape(self) -> Self:
        unary = self.operator in {
            TaskGraphConditionOperator.TRUTHY,
            TaskGraphConditionOperator.FALSY,
        }
        ordered = self.operator in {
            TaskGraphConditionOperator.GREATER_THAN,
            TaskGraphConditionOperator.GREATER_THAN_OR_EQUAL,
            TaskGraphConditionOperator.LESS_THAN,
            TaskGraphConditionOperator.LESS_THAN_OR_EQUAL,
        }
        if unary and self.compare_value is not None:
            raise ValueError("Unary Task graph condition cannot carry a comparison value")
        if not unary and self.compare_value is None:
            raise ValueError("Task graph comparison requires a value")
        if ordered and (
            isinstance(self.compare_value, bool) or not isinstance(self.compare_value, (int, float))
        ):
            raise ValueError("Ordered Task graph comparison requires a number")
        return self


class TaskGraphBudget(DomainModel):
    """Hard graph-wide execution limits consumed by the later Runtime builder."""

    max_node_executions: int = Field(default=256, ge=1, le=4096)
    max_loop_iterations: int = Field(default=16, ge=1, le=128)
    max_parallelism: int = Field(default=8, ge=1, le=32)
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)


class TaskGraphNode(DomainModel):
    """One action or control node in an immutable TaskGraphSpec value."""

    id: str = Field(min_length=1, max_length=64)
    kind: TaskGraphNodeKind
    objective: str | None = Field(default=None, min_length=1, max_length=4096)
    dependencies: tuple[str, ...] = Field(default=(), max_length=32)
    inputs: dict[str, TaskGraphValueBinding] = Field(default_factory=dict, max_length=32)
    outputs: tuple[str, ...] = Field(default=(), max_length=32)
    condition_input: str | None = Field(default=None, min_length=1, max_length=64)
    max_iterations: int | None = Field(default=None, ge=1, le=128)
    subgraph: TaskGraphSpec | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @field_validator("id", "dependencies", "outputs", "condition_input")
    @classmethod
    def validate_identifiers(cls, value: str | tuple[str, ...] | None):
        values = value if isinstance(value, tuple) else (() if value is None else (value,))
        if any(_NAME_PATTERN.fullmatch(item) is None for item in values):
            raise ValueError("Task graph node identifier is invalid")
        return value

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, label="Task graph node objective", max_length=4096)

    @model_validator(mode="after")
    def validate_node_shape(self) -> Self:
        if len(self.dependencies) != len(set(self.dependencies)) or self.id in self.dependencies:
            raise ValueError(TaskGraphValidationReason.DEPENDENCY_UNKNOWN.value)
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError(TaskGraphValidationReason.OUTPUT_INVALID.value)
        if any(_NAME_PATTERN.fullmatch(name) is None for name in self.inputs):
            raise ValueError(TaskGraphValidationReason.BINDING_INVALID.value)
        if self.kind is TaskGraphNodeKind.ACTION:
            if (
                self.objective is None
                or self.condition_input is not None
                or self.max_iterations is not None
                or self.subgraph is not None
            ):
                raise ValueError(TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value)
        elif self.kind is TaskGraphNodeKind.BRANCH:
            self._require_control_input(loop=False)
        elif self.kind is TaskGraphNodeKind.LOOP:
            self._require_control_input(loop=True)
        elif self.kind is TaskGraphNodeKind.SUBGRAPH:
            if (
                self.objective is not None
                or self.condition_input is not None
                or self.max_iterations is not None
                or self.subgraph is None
            ):
                raise ValueError(TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value)
            if set(self.inputs) != set(self.subgraph.input_keys) or set(self.outputs) != set(
                self.subgraph.outputs
            ):
                raise ValueError(TaskGraphValidationReason.BINDING_INVALID.value)
        elif any(
            value is not None
            for value in (self.objective, self.condition_input, self.max_iterations, self.subgraph)
        ):
            raise ValueError(TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value)
        return self

    def _require_control_input(self, *, loop: bool) -> None:
        if (
            self.objective is not None
            or self.condition_input is None
            or self.condition_input not in self.inputs
            or self.subgraph is not None
            or (loop and self.max_iterations is None)
            or (not loop and self.max_iterations is not None)
        ):
            raise ValueError(TaskGraphValidationReason.CONTROL_SHAPE_INVALID.value)


class TaskGraphEdge(DomainModel):
    """One explicit control-flow edge between known graph nodes."""

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    kind: TaskGraphEdgeKind = TaskGraphEdgeKind.SEQUENCE
    condition: TaskGraphCondition | None = None

    @field_validator("source", "target")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Task graph edge node identifier is invalid")
        return value

    @model_validator(mode="after")
    def validate_edge_shape(self) -> Self:
        if self.source == self.target:
            raise ValueError(TaskGraphValidationReason.EDGE_KIND_INVALID.value)
        if (self.kind is TaskGraphEdgeKind.BRANCH) != (self.condition is not None):
            raise ValueError(TaskGraphValidationReason.EDGE_KIND_INVALID.value)
        return self


class TaskGraphSpec(DomainModel):
    """Bounded structured graph data with no executable provider or Python code."""

    version: Literal["1"] = "1"
    input_keys: tuple[str, ...] = Field(default=(), max_length=64)
    nodes: tuple[TaskGraphNode, ...] = Field(min_length=1, max_length=128)
    edges: tuple[TaskGraphEdge, ...] = Field(default=(), max_length=512)
    entry_node: str = Field(min_length=1, max_length=64)
    terminal_nodes: tuple[str, ...] = Field(min_length=1, max_length=32)
    outputs: dict[str, TaskGraphValueBinding] = Field(default_factory=dict, max_length=64)
    budget: TaskGraphBudget = Field(default_factory=TaskGraphBudget)

    @field_validator("entry_node", "input_keys", "terminal_nodes")
    @classmethod
    def validate_names(cls, value: str | tuple[str, ...]):
        values = value if isinstance(value, tuple) else (value,)
        if any(_NAME_PATTERN.fullmatch(item) is None for item in values):
            raise ValueError("Task graph identifier is invalid")
        if isinstance(value, tuple) and len(value) != len(set(value)):
            raise ValueError("Task graph identifiers must be unique")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            self._invalid(TaskGraphValidationReason.DUPLICATE_NODE)
        if self.entry_node not in nodes:
            self._invalid(TaskGraphValidationReason.ENTRY_UNKNOWN)
        if any(node_id not in nodes for node_id in self.terminal_nodes):
            self._invalid(TaskGraphValidationReason.TERMINAL_UNKNOWN)
        if any(_NAME_PATTERN.fullmatch(name) is None for name in self.outputs):
            self._invalid(TaskGraphValidationReason.OUTPUT_INVALID)

        edge_keys = {(edge.source, edge.target, edge.kind) for edge in self.edges}
        if len(edge_keys) != len(self.edges):
            self._invalid(TaskGraphValidationReason.DUPLICATE_EDGE)
        if any(edge.source not in nodes or edge.target not in nodes for edge in self.edges):
            self._invalid(TaskGraphValidationReason.EDGE_NODE_UNKNOWN)

        incoming = {node_id: set[str]() for node_id in nodes}
        outgoing = {node_id: list[TaskGraphEdge]() for node_id in nodes}
        for edge in self.edges:
            outgoing[edge.source].append(edge)
            if edge.kind is not TaskGraphEdgeKind.LOOP_BACK:
                incoming[edge.target].add(edge.source)
            self._validate_edge_kind(edge, nodes)

        if incoming[self.entry_node]:
            self._invalid(TaskGraphValidationReason.DEPENDENCY_EDGE_MISMATCH)
        for node in self.nodes:
            if any(dependency not in nodes for dependency in node.dependencies):
                self._invalid(TaskGraphValidationReason.DEPENDENCY_UNKNOWN)
            if set(node.dependencies) != incoming[node.id]:
                self._invalid(TaskGraphValidationReason.DEPENDENCY_EDGE_MISMATCH)
            if (
                node.timeout_seconds is not None
                and node.timeout_seconds > self.budget.timeout_seconds
            ):
                self._invalid(TaskGraphValidationReason.BUDGET_EXCEEDED)
            self._validate_control_shape(node, nodes, outgoing, self.edges)

        if len(self.nodes) > self.budget.max_node_executions:
            self._invalid(TaskGraphValidationReason.BUDGET_EXCEEDED)
        self._validate_acyclic_without_loop_back(nodes)
        self._validate_loop_backs(nodes, outgoing)
        self._validate_bindings(nodes)
        self._validate_reachability(nodes, outgoing)
        return self

    def _validate_edge_kind(self, edge: TaskGraphEdge, nodes: dict[str, TaskGraphNode]) -> None:
        source = nodes[edge.source]
        target = nodes[edge.target]
        valid = {
            TaskGraphEdgeKind.SEQUENCE: source.kind
            not in {TaskGraphNodeKind.BRANCH, TaskGraphNodeKind.LOOP, TaskGraphNodeKind.PARALLEL},
            TaskGraphEdgeKind.BRANCH: source.kind is TaskGraphNodeKind.BRANCH,
            TaskGraphEdgeKind.LOOP_BODY: source.kind is TaskGraphNodeKind.LOOP,
            TaskGraphEdgeKind.LOOP_EXIT: source.kind is TaskGraphNodeKind.LOOP,
            TaskGraphEdgeKind.LOOP_BACK: target.kind is TaskGraphNodeKind.LOOP,
            TaskGraphEdgeKind.PARALLEL: source.kind is TaskGraphNodeKind.PARALLEL,
            TaskGraphEdgeKind.JOIN: target.kind is TaskGraphNodeKind.JOIN,
        }[edge.kind]
        if not valid:
            self._invalid(TaskGraphValidationReason.EDGE_KIND_INVALID)
        if edge.condition is not None and edge.condition.input_name not in source.inputs:
            self._invalid(TaskGraphValidationReason.BINDING_INVALID)

    def _validate_control_shape(
        self,
        node: TaskGraphNode,
        nodes: dict[str, TaskGraphNode],
        outgoing: dict[str, list[TaskGraphEdge]],
        edges: tuple[TaskGraphEdge, ...],
    ) -> None:
        node_edges = outgoing[node.id]
        if node.id in self.terminal_nodes and node_edges:
            self._invalid(TaskGraphValidationReason.CONTROL_SHAPE_INVALID)
        if node.kind is TaskGraphNodeKind.BRANCH:
            branches = [edge for edge in node_edges if edge.kind is TaskGraphEdgeKind.BRANCH]
            signatures = {
                json.dumps(edge.condition.model_dump(mode="json"), sort_keys=True)
                for edge in branches
                if edge.condition is not None
            }
            if (
                len(branches) < 2
                or len(branches) != len(signatures)
                or len(branches) != len(node_edges)
            ):
                self._invalid(TaskGraphValidationReason.CONTROL_SHAPE_INVALID)
        elif node.kind is TaskGraphNodeKind.LOOP:
            bodies = [edge for edge in node_edges if edge.kind is TaskGraphEdgeKind.LOOP_BODY]
            exits = [edge for edge in node_edges if edge.kind is TaskGraphEdgeKind.LOOP_EXIT]
            backs = [
                edge
                for edge in edges
                if edge.kind is TaskGraphEdgeKind.LOOP_BACK and edge.target == node.id
            ]
            if (
                len(bodies) != 1
                or len(exits) != 1
                or len(backs) != 1
                or len(node_edges) != 2
                or node.max_iterations is None
                or node.max_iterations > self.budget.max_loop_iterations
            ):
                self._invalid(TaskGraphValidationReason.CONTROL_SHAPE_INVALID)
            feedback_node = nodes[backs[0].source]
            if not set(node.outputs).issubset(feedback_node.outputs):
                self._invalid(TaskGraphValidationReason.BINDING_INVALID)
        elif node.kind is TaskGraphNodeKind.PARALLEL:
            branches = [edge for edge in node_edges if edge.kind is TaskGraphEdgeKind.PARALLEL]
            if (
                len(branches) < 2
                or len(branches) != len(node_edges)
                or len(branches) > self.budget.max_parallelism
            ):
                self._invalid(TaskGraphValidationReason.CONTROL_SHAPE_INVALID)
        elif node.kind is TaskGraphNodeKind.JOIN:
            joins = [
                edge
                for edge in edges
                if edge.kind is TaskGraphEdgeKind.JOIN and edge.target == node.id
            ]
            if len(joins) < 2 or len(joins) != len(node.dependencies):
                self._invalid(TaskGraphValidationReason.CONTROL_SHAPE_INVALID)
        if node.kind in {
            TaskGraphNodeKind.BRANCH,
            TaskGraphNodeKind.PARALLEL,
            TaskGraphNodeKind.JOIN,
        } and not set(node.outputs).issubset(node.inputs):
            self._invalid(TaskGraphValidationReason.BINDING_INVALID)

    def _validate_acyclic_without_loop_back(self, nodes: dict[str, TaskGraphNode]) -> None:
        adjacency = {node_id: list[str]() for node_id in nodes}
        indegree = {node_id: 0 for node_id in nodes}
        for edge in self.edges:
            if edge.kind is TaskGraphEdgeKind.LOOP_BACK:
                continue
            adjacency[edge.source].append(edge.target)
            indegree[edge.target] += 1
        ready = [node_id for node_id, count in indegree.items() if count == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for target in adjacency[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(nodes):
            self._invalid(TaskGraphValidationReason.UNCONTROLLED_CYCLE)

    def _validate_loop_backs(
        self,
        nodes: dict[str, TaskGraphNode],
        outgoing: dict[str, list[TaskGraphEdge]],
    ) -> None:
        body_adjacency = {node_id: list[str]() for node_id in nodes}
        for edge in self.edges:
            if edge.kind not in {TaskGraphEdgeKind.LOOP_BACK, TaskGraphEdgeKind.LOOP_EXIT}:
                body_adjacency[edge.source].append(edge.target)
        for edge in self.edges:
            if edge.kind is not TaskGraphEdgeKind.LOOP_BACK:
                continue
            body = next(
                candidate.target
                for candidate in outgoing[edge.target]
                if candidate.kind is TaskGraphEdgeKind.LOOP_BODY
            )
            if edge.source not in self._reachable(body, body_adjacency):
                self._invalid(TaskGraphValidationReason.LOOP_BACK_INVALID)

    def _validate_bindings(self, nodes: dict[str, TaskGraphNode]) -> None:
        loop_back_sources: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.kind is TaskGraphEdgeKind.LOOP_BACK:
                loop_back_sources.setdefault(edge.target, set()).add(edge.source)
        dependency_cache: dict[str, set[str]] = {}

        def dependencies(node_id: str) -> set[str]:
            if node_id not in dependency_cache:
                result: set[str] = set()
                for dependency in nodes[node_id].dependencies:
                    result.add(dependency)
                    result.update(dependencies(dependency))
                dependency_cache[node_id] = result
            return dependency_cache[node_id]

        for node in self.nodes:
            allowed = dependencies(node.id)
            feedback_sources = loop_back_sources.get(node.id, set())
            for binding in node.inputs.values():
                self._validate_binding(binding, nodes)
                if (
                    binding.source is TaskGraphValueSource.NODE_OUTPUT
                    and binding.node_id not in allowed
                    and binding.node_id not in feedback_sources
                ):
                    self._invalid(TaskGraphValidationReason.BINDING_INVALID)
        for binding in self.outputs.values():
            self._validate_binding(binding, nodes)

    def _validate_binding(
        self,
        binding: TaskGraphValueBinding,
        nodes: dict[str, TaskGraphNode],
    ) -> None:
        if binding.source is TaskGraphValueSource.GRAPH_INPUT:
            if binding.key not in self.input_keys:
                self._invalid(TaskGraphValidationReason.BINDING_INVALID)
            return
        if binding.node_id not in nodes or binding.key not in nodes[binding.node_id].outputs:
            self._invalid(TaskGraphValidationReason.BINDING_INVALID)

    def _validate_reachability(
        self,
        nodes: dict[str, TaskGraphNode],
        outgoing: dict[str, list[TaskGraphEdge]],
    ) -> None:
        adjacency = {
            node_id: [edge.target for edge in edges] for node_id, edges in outgoing.items()
        }
        if self._reachable(self.entry_node, adjacency) != set(nodes):
            self._invalid(TaskGraphValidationReason.UNREACHABLE_NODE)
        reverse = {node_id: list[str]() for node_id in nodes}
        for source, targets in adjacency.items():
            for target in targets:
                reverse[target].append(source)
        terminal_reachable: set[str] = set()
        for terminal in self.terminal_nodes:
            terminal_reachable.update(self._reachable(terminal, reverse))
        if terminal_reachable != set(nodes):
            self._invalid(TaskGraphValidationReason.NO_TERMINAL_PATH)

    @staticmethod
    def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency[current])
        return visited

    @staticmethod
    def _invalid(reason: TaskGraphValidationReason) -> None:
        raise ValueError(reason.value)


TaskGraphNode.model_rebuild()


def task_graph_spec_hash(spec: TaskGraphSpec) -> str:
    """Hash one canonical semantic serialization independent of field ordering."""

    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class GraphRevision(DomainModel):
    """Append-only validated TaskGraphSpec content linked into one Task history."""

    id: GraphRevisionId
    task_id: TaskId
    previous_revision_id: GraphRevisionId | None = None
    reason: str = Field(min_length=1, max_length=2048)
    spec: TaskGraphSpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: GraphRevisionStatus = GraphRevisionStatus.VALIDATED
    created_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, label="Graph revision reason", max_length=2048)

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if self.previous_revision_id == self.id:
            raise ValueError("Graph revision cannot reference itself")
        if self.spec_hash != task_graph_spec_hash(self.spec):
            raise ValueError("Graph revision hash does not match its TaskGraphSpec")
        return self

    @classmethod
    def create(
        cls,
        *,
        task_id: TaskId,
        reason: str,
        spec: TaskGraphSpec,
        previous_revision_id: GraphRevisionId | None = None,
        metadata: SafeMetadata | None = None,
    ) -> GraphRevision:
        return cls(
            id=new_graph_revision_id(),
            task_id=task_id,
            previous_revision_id=previous_revision_id,
            reason=reason,
            spec=spec,
            spec_hash=task_graph_spec_hash(spec),
            metadata=metadata or SafeMetadata(),
        )
