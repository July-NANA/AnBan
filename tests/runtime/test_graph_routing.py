"""Model-governed fixed-Agent versus dynamic-graph routing decisions."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import JsonValue

from anban.core import AnbanError, ErrorCode, ErrorInfo, TaskGraphSpec
from anban.model import ModelRequest, ModelTurn
from anban.runtime import (
    TASK_REQUEST_INPUT,
    TaskExecutionRoute,
    TaskRouteEvaluator,
)
from tests.core.test_graph import branch_graph, loop_graph, parallel_subgraph_graph


class ScriptedRouteModel:
    def __init__(self, turns: list[ModelTurn | AnbanError]) -> None:
        self.turns = turns
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, AnbanError):
            raise turn
        return turn


def route_turn(
    route: str,
    graph_spec: dict[str, JsonValue] | None = None,
) -> ModelTurn:
    return ModelTurn(
        structured_output={
            "route": route,
            "rationale": "Select the lowest-complexity reliable execution topology.",
            "graph_spec": graph_spec or {},
        },
        finish_reason="stop",
    )


def request_input_branch() -> TaskGraphSpec:
    payload = branch_graph().model_dump(mode="json")
    payload["input_keys"] = [TASK_REQUEST_INPUT]
    payload["nodes"][0]["inputs"]["payload"]["key"] = TASK_REQUEST_INPUT
    return TaskGraphSpec.model_validate(payload)


async def test_simple_task_selects_fixed_agent_without_graph_data() -> None:
    model = ScriptedRouteModel([route_turn(TaskExecutionRoute.FIXED_AGENT.value)])

    decision = await TaskRouteEvaluator().decide("Answer one bounded question.", model)

    assert decision.route is TaskExecutionRoute.FIXED_AGENT
    assert decision.graph_spec is None
    assert decision.model_turn_count == 1
    assert model.requests[0].response_schema is not None


@pytest.mark.parametrize(
    "spec_factory",
    (request_input_branch, loop_graph, parallel_subgraph_graph),
)
async def test_complex_task_accepts_distinct_validated_graph_topologies(
    spec_factory: Callable[[], TaskGraphSpec],
) -> None:
    spec = spec_factory()
    model = ScriptedRouteModel(
        [route_turn(TaskExecutionRoute.TASK_GRAPH.value, spec.model_dump(mode="json"))]
    )

    decision = await TaskRouteEvaluator().decide("Coordinate the structured work.", model)

    assert decision.route is TaskExecutionRoute.TASK_GRAPH
    assert decision.graph_spec == spec
    assert decision.model_turn_count == 1


async def test_invalid_graph_is_repaired_without_falling_back_to_success() -> None:
    invalid = branch_graph().model_dump(mode="json")
    valid = loop_graph()
    model = ScriptedRouteModel(
        [
            route_turn(TaskExecutionRoute.TASK_GRAPH.value, invalid),
            route_turn(TaskExecutionRoute.TASK_GRAPH.value, valid.model_dump(mode="json")),
        ]
    )

    decision = await TaskRouteEvaluator().decide("Execute a bounded iterative plan.", model)

    assert decision.graph_spec == valid
    assert decision.model_turn_count == 2
    assert [request.repair_attempt for request in model.requests] == [0, 1]


async def test_invalid_route_exhaustion_and_nonrepairable_model_failure_are_explicit() -> None:
    invalid_fixed = route_turn(
        TaskExecutionRoute.FIXED_AGENT.value,
        loop_graph().model_dump(mode="json"),
    )
    model = ScriptedRouteModel([invalid_fixed, invalid_fixed])

    with pytest.raises(AnbanError) as invalid:
        await TaskRouteEvaluator().decide("Reject an invalid route.", model, repair_limit=1)
    assert invalid.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert invalid.value.info.details.root["reason"] == "task_route_invalid"
    assert invalid.value.info.details.root["last_validation_reason"] == "fixed_route_graph_present"

    unavailable = AnbanError(
        ErrorInfo(code=ErrorCode.MODEL_TRANSPORT_FAILED, message="Model transport failed")
    )
    with pytest.raises(AnbanError) as failed:
        await TaskRouteEvaluator().decide(
            "Keep transport failure explicit.",
            ScriptedRouteModel([unavailable]),
        )
    assert failed.value.info.code is ErrorCode.MODEL_TRANSPORT_FAILED
