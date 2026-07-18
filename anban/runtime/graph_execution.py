"""Bounded execution semantics for dynamically compiled Task graphs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from pydantic import JsonValue, TypeAdapter, ValidationError

from anban.core import (
    TaskGraphCondition,
    TaskGraphConditionOperator,
    TaskGraphEdge,
    TaskGraphEdgeKind,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
    TaskGraphValueBinding,
    TaskGraphValueSource,
)
from anban.runtime.graph_builder import (
    DynamicTaskGraphBuilder,
    TaskGraphNodeAction,
    TaskGraphRouteAction,
    TaskGraphRuntimeState,
    TaskGraphStateUpdate,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_ResultT = TypeVar("_ResultT")


class TaskGraphExecutionFailureReason(StrEnum):
    """Stable failure categories emitted by generic graph execution."""

    INPUT_INVALID = "input_invalid"
    BINDING_UNAVAILABLE = "binding_unavailable"
    ACTION_FAILED = "action_failed"
    ACTION_OUTPUT_INVALID = "action_output_invalid"
    CONDITION_INVALID = "condition_invalid"
    ROUTE_AMBIGUOUS = "route_ambiguous"
    NODE_BUDGET_EXCEEDED = "node_budget_exceeded"
    LOOP_BUDGET_EXCEEDED = "loop_budget_exceeded"
    NODE_TIMED_OUT = "node_timed_out"
    GRAPH_TIMED_OUT = "graph_timed_out"
    RECOVERY_STATE_INVALID = "recovery_state_invalid"


class TaskGraphExecutionError(RuntimeError):
    """Explicit graph failure without provider output or input disclosure."""

    def __init__(self, reason: TaskGraphExecutionFailureReason) -> None:
        self.reason = reason
        super().__init__(f"Task graph execution failed: {reason.value}")


TaskGraphActionExecutor = Callable[
    [TaskGraphNode, dict[str, JsonValue]],
    Awaitable[dict[str, JsonValue]],
]


@dataclass(frozen=True, slots=True)
class TaskGraphExecutionResult:
    """Successful bounded graph outputs and the complete latest node values."""

    outputs: dict[str, JsonValue]
    node_outputs: dict[str, dict[str, JsonValue]]
    node_execution_count: int


@dataclass(slots=True)
class _NodeBudget:
    limit: int
    parent: _NodeBudget | None = None
    count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def consume(self) -> None:
        if self.parent is not None:
            await self.parent.consume()
        async with self._lock:
            if self.count >= self.limit:
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.NODE_BUDGET_EXCEEDED)
            self.count += 1


@dataclass(slots=True)
class _ExecutionContext:
    budget: _NodeBudget
    action_semaphores: tuple[asyncio.Semaphore, ...]
    loop_iterations: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    _loop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def enter_loop_body(self, node: TaskGraphNode) -> None:
        if node.max_iterations is None:
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.CONDITION_INVALID)
        async with self._loop_lock:
            iterations = self.loop_iterations.get(node.id, 0)
            if iterations >= node.max_iterations:
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.LOOP_BUDGET_EXCEEDED)
            self.loop_iterations[node.id] = iterations + 1


class TaskGraphExecutor:
    """Execute all closed TaskGraphSpec semantics through one dynamic Builder."""

    def __init__(self, builder: DynamicTaskGraphBuilder | None = None) -> None:
        self._builder = builder or DynamicTaskGraphBuilder()

    async def execute(
        self,
        spec: TaskGraphSpec,
        graph_input: dict[str, JsonValue],
        *,
        action_executor: TaskGraphActionExecutor,
    ) -> TaskGraphExecutionResult:
        """Run one validated spec with bounded real action execution."""

        inputs = self._validate_graph_input(spec, graph_input)
        root_budget = _NodeBudget(spec.budget.max_node_executions)
        return await self._bounded(
            self._execute_spec(
                spec,
                inputs,
                action_executor=action_executor,
                parent_budget=None,
                parent_semaphores=(),
                root_budget=root_budget,
            ),
            timeout_seconds=spec.budget.timeout_seconds,
            reason=TaskGraphExecutionFailureReason.GRAPH_TIMED_OUT,
        )

    async def _execute_spec(
        self,
        spec: TaskGraphSpec,
        graph_input: dict[str, JsonValue],
        *,
        action_executor: TaskGraphActionExecutor,
        parent_budget: _NodeBudget | None,
        parent_semaphores: tuple[asyncio.Semaphore, ...],
        root_budget: _NodeBudget,
    ) -> TaskGraphExecutionResult:
        budget = (
            root_budget
            if parent_budget is None
            else _NodeBudget(
                spec.budget.max_node_executions,
                parent=parent_budget,
            )
        )
        local_semaphore = asyncio.Semaphore(spec.budget.max_parallelism)
        context = _ExecutionContext(
            budget=budget,
            action_semaphores=(*parent_semaphores, local_semaphore),
        )

        def node_action_factory(node: TaskGraphNode) -> TaskGraphNodeAction:
            async def execute_node(state: TaskGraphRuntimeState) -> TaskGraphStateUpdate:
                await context.budget.consume()
                node_input = self._resolve_node_inputs(node, state)
                output = await self._execute_node(
                    spec,
                    node,
                    node_input,
                    state,
                    context=context,
                    action_executor=action_executor,
                    root_budget=root_budget,
                )
                return {"node_outputs": {node.id: output}}

            return execute_node

        def route_action_factory(
            node: TaskGraphNode,
            edges: tuple[TaskGraphEdge, ...],
        ) -> TaskGraphRouteAction:
            async def route(state: TaskGraphRuntimeState) -> str:
                node_input = self._resolve_node_inputs(node, state)
                if node.kind is TaskGraphNodeKind.BRANCH:
                    return self._select_branch(node_input, edges)
                if node.kind is TaskGraphNodeKind.LOOP:
                    condition_name = node.condition_input
                    if condition_name is None or condition_name not in node_input:
                        raise TaskGraphExecutionError(
                            TaskGraphExecutionFailureReason.CONDITION_INVALID
                        )
                    if bool(node_input[condition_name]):
                        await context.enter_loop_body(node)
                        edge_kind = TaskGraphEdgeKind.LOOP_BODY
                    else:
                        edge_kind = TaskGraphEdgeKind.LOOP_EXIT
                    return next(edge.target for edge in edges if edge.kind is edge_kind)
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.CONDITION_INVALID)

            return route

        compiled = self._builder.compile(
            spec,
            node_action_factory=node_action_factory,
            route_action_factory=route_action_factory,
        )
        state = await compiled.graph.ainvoke(
            {"graph_input": graph_input, "node_outputs": {}},
            {"recursion_limit": spec.budget.max_node_executions + 2},
        )
        outputs = self._resolve_graph_outputs(spec, state)
        return TaskGraphExecutionResult(
            outputs=outputs,
            node_outputs=state["node_outputs"],
            node_execution_count=budget.count,
        )

    async def _execute_node(
        self,
        spec: TaskGraphSpec,
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
        state: TaskGraphRuntimeState,
        *,
        context: _ExecutionContext,
        action_executor: TaskGraphActionExecutor,
        root_budget: _NodeBudget,
    ) -> dict[str, JsonValue]:
        if node.kind is TaskGraphNodeKind.ACTION:
            output = await self._execute_action(
                node,
                node_input,
                context=context,
                action_executor=action_executor,
            )
            return self._validate_action_output(node, output)
        if node.kind is TaskGraphNodeKind.SUBGRAPH:
            if node.subgraph is None:
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID)
            result = await self._bounded(
                self._execute_spec(
                    node.subgraph,
                    node_input,
                    action_executor=action_executor,
                    parent_budget=context.budget,
                    parent_semaphores=context.action_semaphores,
                    root_budget=root_budget,
                ),
                timeout_seconds=node.subgraph.budget.timeout_seconds,
                reason=TaskGraphExecutionFailureReason.GRAPH_TIMED_OUT,
            )
            return self._validate_action_output(node, result.outputs)
        if node.kind is TaskGraphNodeKind.LOOP:
            return self._loop_outputs(spec, node, state)
        return {name: node_input[name] for name in node.outputs}

    async def _execute_action(
        self,
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
        *,
        context: _ExecutionContext,
        action_executor: TaskGraphActionExecutor,
    ) -> dict[str, JsonValue]:
        acquired: list[asyncio.Semaphore] = []
        try:
            for semaphore in context.action_semaphores:
                await semaphore.acquire()
                acquired.append(semaphore)
            try:
                invocation = action_executor(node, node_input)
                if node.timeout_seconds is None:
                    return await invocation
                return await self._bounded(
                    invocation,
                    timeout_seconds=node.timeout_seconds,
                    reason=TaskGraphExecutionFailureReason.NODE_TIMED_OUT,
                )
            except (TaskGraphExecutionError, asyncio.CancelledError):
                raise
            except Exception as exc:
                raise TaskGraphExecutionError(
                    TaskGraphExecutionFailureReason.ACTION_FAILED
                ) from exc
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()

    @staticmethod
    async def _bounded(
        operation: Awaitable[_ResultT],
        *,
        timeout_seconds: int,
        reason: TaskGraphExecutionFailureReason,
    ) -> _ResultT:
        task = asyncio.ensure_future(operation)
        done, _ = await asyncio.wait((task,), timeout=timeout_seconds)
        if task in done:
            return await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise TaskGraphExecutionError(reason)

    @staticmethod
    def _validate_graph_input(
        spec: TaskGraphSpec,
        graph_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        try:
            validated = _JSON_OBJECT.validate_python(graph_input, strict=True)
        except ValidationError as exc:
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.INPUT_INVALID) from exc
        if set(validated) != set(spec.input_keys):
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.INPUT_INVALID)
        return validated

    def _resolve_node_inputs(
        self,
        node: TaskGraphNode,
        state: TaskGraphRuntimeState,
    ) -> dict[str, JsonValue]:
        return {
            name: self._resolve_binding(binding, state) for name, binding in node.inputs.items()
        }

    def _resolve_graph_outputs(
        self,
        spec: TaskGraphSpec,
        state: TaskGraphRuntimeState,
    ) -> dict[str, JsonValue]:
        outputs: dict[str, JsonValue] = {}
        for name, binding in spec.outputs.items():
            try:
                outputs[name] = self._resolve_binding(binding, state)
            except TaskGraphExecutionError as exc:
                if exc.reason is not TaskGraphExecutionFailureReason.BINDING_UNAVAILABLE:
                    raise
        return outputs

    @staticmethod
    def _resolve_binding(
        binding: TaskGraphValueBinding,
        state: TaskGraphRuntimeState,
    ) -> JsonValue:
        if binding.source is TaskGraphValueSource.GRAPH_INPUT:
            if binding.key in state["graph_input"]:
                return state["graph_input"][binding.key]
        elif binding.node_id is not None:
            output = state["node_outputs"].get(binding.node_id, {})
            if binding.key in output:
                return output[binding.key]
        if binding.fallback_value is not None:
            return binding.fallback_value
        raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.BINDING_UNAVAILABLE)

    @staticmethod
    def _validate_action_output(
        node: TaskGraphNode,
        output: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        try:
            validated = _JSON_OBJECT.validate_python(output, strict=True)
        except ValidationError as exc:
            raise TaskGraphExecutionError(
                TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID
            ) from exc
        if set(validated) != set(node.outputs):
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID)
        return validated

    @staticmethod
    def _loop_outputs(
        spec: TaskGraphSpec,
        node: TaskGraphNode,
        state: TaskGraphRuntimeState,
    ) -> dict[str, JsonValue]:
        feedback_source = next(
            edge.source
            for edge in spec.edges
            if edge.kind is TaskGraphEdgeKind.LOOP_BACK and edge.target == node.id
        )
        feedback = state["node_outputs"].get(feedback_source, {})
        return {name: feedback[name] for name in node.outputs if name in feedback}

    def _select_branch(
        self,
        node_input: dict[str, JsonValue],
        edges: tuple[TaskGraphEdge, ...],
    ) -> str:
        matches = [
            edge.target
            for edge in edges
            if edge.condition is not None and self._condition_matches(edge.condition, node_input)
        ]
        if len(matches) != 1:
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ROUTE_AMBIGUOUS)
        return matches[0]

    @staticmethod
    def _condition_matches(
        condition: TaskGraphCondition,
        node_input: dict[str, JsonValue],
    ) -> bool:
        if condition.input_name not in node_input:
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.CONDITION_INVALID)
        actual = node_input[condition.input_name]
        expected = condition.compare_value
        operator = condition.operator
        if operator is TaskGraphConditionOperator.EQUALS:
            return actual == expected
        if operator is TaskGraphConditionOperator.NOT_EQUALS:
            return actual != expected
        if operator is TaskGraphConditionOperator.TRUTHY:
            return bool(actual)
        if operator is TaskGraphConditionOperator.FALSY:
            return not bool(actual)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or isinstance(expected, bool)
            or not isinstance(expected, (int, float))
        ):
            raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.CONDITION_INVALID)
        if operator is TaskGraphConditionOperator.GREATER_THAN:
            return actual > expected
        if operator is TaskGraphConditionOperator.GREATER_THAN_OR_EQUAL:
            return actual >= expected
        if operator is TaskGraphConditionOperator.LESS_THAN:
            return actual < expected
        return actual <= expected
