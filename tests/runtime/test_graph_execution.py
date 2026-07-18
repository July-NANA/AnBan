"""Bounded execution semantics for branch, loop, parallel, join, and subgraph nodes."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue

from anban.core import (
    TaskGraphBudget,
    TaskGraphCondition,
    TaskGraphConditionOperator,
    TaskGraphNode,
    TaskGraphSpec,
)
from anban.runtime import (
    TaskGraphExecutionError,
    TaskGraphExecutionFailureReason,
    TaskGraphExecutor,
)
from tests.core.test_graph import action, branch_graph, loop_graph, parallel_subgraph_graph


def branch_with_conditions(
    first: TaskGraphCondition,
    second: TaskGraphCondition,
) -> TaskGraphSpec:
    original = branch_graph()
    conditions = iter((first, second))
    edges = tuple(
        edge.model_copy(update={"condition": next(conditions)})
        if edge.condition is not None
        else edge
        for edge in original.edges
    )
    return TaskGraphSpec.model_validate({**original.model_dump(), "edges": edges})


@pytest.mark.parametrize(
    ("payload", "first", "second", "expected"),
    (
        (
            "alpha",
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.EQUALS,
                compare_value="alpha",
            ),
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.NOT_EQUALS,
                compare_value="alpha",
            ),
            "fast_path",
        ),
        (
            8,
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.GREATER_THAN,
                compare_value=3,
            ),
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.LESS_THAN_OR_EQUAL,
                compare_value=3,
            ),
            "fast_path",
        ),
        (
            2,
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.GREATER_THAN_OR_EQUAL,
                compare_value=5,
            ),
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.LESS_THAN,
                compare_value=5,
            ),
            "careful_path",
        ),
        (
            False,
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.TRUTHY,
            ),
            TaskGraphCondition(
                input_name="route",
                operator=TaskGraphConditionOperator.FALSY,
            ),
            "careful_path",
        ),
    ),
)
async def test_branch_executes_exactly_one_matching_route(
    payload: JsonValue,
    first: TaskGraphCondition,
    second: TaskGraphCondition,
    expected: str,
) -> None:
    visited: list[str] = []

    async def execute(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        visited.append(node.id)
        if node.id == "classify":
            return {"route": node_input["payload"]}
        return {"result": node.id}

    result = await TaskGraphExecutor().execute(
        branch_with_conditions(first, second),
        {"payload": payload},
        action_executor=execute,
    )

    assert visited == ["classify", expected]
    assert result.outputs == {
        "fast_result" if expected == "fast_path" else "careful_result": expected
    }
    assert result.node_execution_count == 3


async def test_loop_rebinds_feedback_and_exposes_latest_output() -> None:
    iterations = 0

    async def execute(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal iterations
        if node.id == "seed":
            return {"seed_value": 1}
        if node.id == "iterate":
            iterations += 1
            return {"continue": iterations < 3, "result": iterations}
        return {"result": node_input["result"]}

    result = await TaskGraphExecutor().execute(loop_graph(), {}, action_executor=execute)

    assert iterations == 3
    assert result.outputs == {"result": 3}
    assert result.node_outputs["bounded_loop"] == {"result": 3}
    assert result.node_execution_count == 9


async def test_parallel_branches_really_overlap_before_join_and_nested_result() -> None:
    active = 0
    maximum_active = 0
    both_started = asyncio.Event()
    lock = asyncio.Lock()

    async def execute(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal active, maximum_active
        if node.id == "prepare":
            return {"item": "value"}
        if node.id in {"nested_action", "direct_branch"}:
            async with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            async with lock:
                active -= 1
            return {"result": node_input["item"]}
        if node.id == "publish":
            return {"result": f"{node_input['nested']}:{node_input['direct']}"}
        return {name: node_input[name] for name in node.outputs}

    result = await TaskGraphExecutor().execute(
        parallel_subgraph_graph(),
        {},
        action_executor=execute,
    )

    assert maximum_active == 2
    assert result.outputs == {"result": "value:value"}
    assert result.node_execution_count == 7
    assert set(result.node_outputs) == {
        "prepare",
        "fan_out",
        "nested_branch",
        "direct_branch",
        "join_results",
        "publish",
    }


async def test_loop_and_node_budgets_fail_closed_without_retry() -> None:
    calls = 0

    async def continue_forever(
        node: TaskGraphNode,
        _node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal calls
        calls += 1
        if node.id == "seed":
            return {"seed_value": 1}
        if node.id == "iterate":
            return {"continue": True, "result": calls}
        return {"result": calls}

    with pytest.raises(TaskGraphExecutionError) as loop_failure:
        await TaskGraphExecutor().execute(loop_graph(), {}, action_executor=continue_forever)
    assert loop_failure.value.reason is TaskGraphExecutionFailureReason.LOOP_BUDGET_EXCEEDED
    assert calls == 6

    constrained = loop_graph().model_copy(
        update={
            "budget": TaskGraphBudget(
                max_node_executions=4,
                max_loop_iterations=7,
            )
        }
    )

    async def stop_once(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if node.id == "seed":
            return {"seed_value": 1}
        if node.id == "iterate":
            return {"continue": False, "result": 1}
        return {"result": node_input["result"]}

    with pytest.raises(TaskGraphExecutionError) as node_failure:
        await TaskGraphExecutor().execute(constrained, {}, action_executor=stop_once)
    assert node_failure.value.reason is TaskGraphExecutionFailureReason.NODE_BUDGET_EXCEEDED


async def test_invalid_input_output_route_and_action_failure_are_explicit() -> None:
    calls = 0

    async def invalid_output(
        _node: TaskGraphNode,
        _node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(TaskGraphExecutionError) as input_failure:
        await TaskGraphExecutor().execute(
            branch_graph(),
            {},
            action_executor=invalid_output,
        )
    assert input_failure.value.reason is TaskGraphExecutionFailureReason.INPUT_INVALID
    assert calls == 0

    with pytest.raises(TaskGraphExecutionError) as output_failure:
        await TaskGraphExecutor().execute(
            branch_graph(),
            {"payload": "route"},
            action_executor=invalid_output,
        )
    assert output_failure.value.reason is TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID
    assert calls == 1

    ambiguous = branch_with_conditions(
        TaskGraphCondition(
            input_name="route",
            operator=TaskGraphConditionOperator.NOT_EQUALS,
            compare_value="one",
        ),
        TaskGraphCondition(
            input_name="route",
            operator=TaskGraphConditionOperator.NOT_EQUALS,
            compare_value="two",
        ),
    )

    async def classify(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if node.id == "classify":
            return {"route": node_input["payload"]}
        return {"result": node.id}

    with pytest.raises(TaskGraphExecutionError) as route_failure:
        await TaskGraphExecutor().execute(
            ambiguous,
            {"payload": "three"},
            action_executor=classify,
        )
    assert route_failure.value.reason is TaskGraphExecutionFailureReason.ROUTE_AMBIGUOUS

    numeric_route = branch_with_conditions(
        TaskGraphCondition(
            input_name="route",
            operator=TaskGraphConditionOperator.GREATER_THAN,
            compare_value=10,
        ),
        TaskGraphCondition(
            input_name="route",
            operator=TaskGraphConditionOperator.LESS_THAN_OR_EQUAL,
            compare_value=10,
        ),
    )
    with pytest.raises(TaskGraphExecutionError) as condition_failure:
        await TaskGraphExecutor().execute(
            numeric_route,
            {"payload": "not_numeric"},
            action_executor=classify,
        )
    assert condition_failure.value.reason is TaskGraphExecutionFailureReason.CONDITION_INVALID

    async def fail(
        _node: TaskGraphNode,
        _node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        raise RuntimeError("untrusted detail")

    with pytest.raises(TaskGraphExecutionError) as action_failure:
        await TaskGraphExecutor().execute(
            branch_graph(),
            {"payload": "route"},
            action_executor=fail,
        )
    assert action_failure.value.reason is TaskGraphExecutionFailureReason.ACTION_FAILED


@pytest.mark.parametrize(
    ("node_timeout", "graph_timeout", "reason"),
    (
        (1, 3, TaskGraphExecutionFailureReason.NODE_TIMED_OUT),
        (None, 1, TaskGraphExecutionFailureReason.GRAPH_TIMED_OUT),
    ),
)
async def test_timeouts_cancel_one_action_without_retry(
    node_timeout: int | None,
    graph_timeout: int,
    reason: TaskGraphExecutionFailureReason,
) -> None:
    node = action("bounded_action", outputs=("result",)).model_copy(
        update={"timeout_seconds": node_timeout}
    )
    spec = TaskGraphSpec(
        nodes=(node,),
        entry_node=node.id,
        terminal_nodes=(node.id,),
        outputs={},
        budget=TaskGraphBudget(timeout_seconds=graph_timeout),
    )
    calls = 0
    cancelled = asyncio.Event()

    async def slow_action(
        _node: TaskGraphNode,
        _node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal calls
        calls += 1
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.set()
        return {"result": "late"}

    with pytest.raises(TaskGraphExecutionError) as failure:
        await TaskGraphExecutor().execute(spec, {}, action_executor=slow_action)

    assert failure.value.reason is reason
    assert calls == 1
    assert cancelled.is_set()
