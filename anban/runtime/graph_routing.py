"""Model-governed selection between the fixed Agent and dynamic Task graph paths."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from anban.config import policy
from anban.core import (
    AnbanError,
    ErrorCode,
    ErrorInfo,
    SafeMetadata,
    TaskGraphSpec,
    TaskGraphValidationReason,
)
from anban.core.metadata import validate_safe_text
from anban.model import ModelMessage, ModelPort, ModelRequest
from anban.runtime.contracts import RuntimeValue

TASK_REQUEST_INPUT = "task_request"
_ROUTE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "route": {"type": "string"},
        "rationale": {"type": "string"},
        "graph_spec": {"type": "object"},
    },
    "required": ["route", "rationale", "graph_spec"],
    "additionalProperties": False,
}
_GRAPH_RESPONSE_GUIDANCE = (
    "TaskGraphSpec is one closed JSON object with version, input_keys, nodes, edges, entry_node, "
    "terminal_nodes, outputs, and budget. Nodes use the closed action, branch, loop, parallel, "
    "join, and subgraph kinds; edges use sequence, branch, loop_body, loop_exit, loop_back, "
    "parallel, and join. Bind inputs and graph outputs only from graph_input or node_output. "
    "Runtime validation remains authoritative for identifiers, dependencies, control shapes, "
    "bindings, reachability, and budgets. If the task already contains one complete valid "
    "TaskGraphSpec JSON object, preserve that exact object as graph_spec instead of redesigning "
    "or restating it."
)
_SYSTEM_INSTRUCTIONS = (
    "Select the lowest-complexity truthful Runtime path for this task. Choose fixed_agent when the "
    "bounded General Agent loop can complete the goal, including when it needs multiple sequential "
    "Capability or Skill calls. Choose task_graph only when explicit multi-node dependencies, "
    "conditional branches, bounded iteration, parallel work with a join, or a nested subgraph is "
    "materially required. For fixed_agent return an empty graph_spec. For task_graph return one "
    "complete TaskGraphSpec matching the supplied schema. A graph may have no external inputs or "
    f"may declare exactly one input named {TASK_REQUEST_INPUT}; no other graph input is available. "
    "Action objectives must describe real work, control flow must use the closed node and edge "
    "meanings, every loop and parallel path must be bounded, and the graph must expose a terminal "
    "answer. Never infer execution success. Return only the closed route object. "
    f"{_GRAPH_RESPONSE_GUIDANCE}"
)
_REPAIR_INSTRUCTIONS = (
    "The previous route response was not a valid closed routing decision or TaskGraphSpec. Decide "
    "again from the same task, preserve the lowest-complexity rule, and return only one JSON "
    "object with exactly route, rationale, and graph_spec. Route must be fixed_agent or "
    "task_graph, rationale must be a non-empty string, and graph_spec must be an empty object for "
    "fixed_agent or the complete valid TaskGraphSpec for task_graph. Do not return an error, "
    "message, or explanatory wrapper."
)


class TaskExecutionRoute(StrEnum):
    FIXED_AGENT = "fixed_agent"
    TASK_GRAPH = "task_graph"


class TaskRouteDecision(RuntimeValue):
    """Validated Main Agent routing fact with optional executable graph data."""

    route: TaskExecutionRoute
    rationale: str = Field(min_length=1, max_length=2048)
    graph_spec: TaskGraphSpec | None = None
    model_turn_count: int = Field(ge=1, le=policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX + 1)

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Task route rationale", max_length=2048)

    @model_validator(mode="after")
    def validate_route_shape(self) -> Self:
        if (self.route is TaskExecutionRoute.TASK_GRAPH) != (self.graph_spec is not None):
            raise ValueError("Task route and graph presence disagree")
        return self


class _ModelRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: TaskExecutionRoute
    rationale: str = Field(min_length=1, max_length=2048)
    graph_spec: dict[str, JsonValue]

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Task route rationale", max_length=2048)


class TaskRouteEvaluator:
    """Use the independent Model Port to select and validate one Runtime path."""

    async def decide(
        self,
        request: str,
        model: ModelPort,
        *,
        repair_limit: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> TaskRouteDecision:
        if not (
            policy.MODEL_RESPONSE_REPAIR_RETRIES_MIN
            <= repair_limit
            <= policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX
        ):
            raise ValueError("Task route repair budget is outside policy")
        last_validation_reason = "response_shape_invalid"
        for repair_attempt in range(repair_limit + 1):
            try:
                turn = await model.complete(
                    ModelRequest(
                        messages=(
                            ModelMessage(role="system", content=_SYSTEM_INSTRUCTIONS),
                            *(
                                ()
                                if repair_attempt == 0
                                else (
                                    ModelMessage(
                                        role="system",
                                        content=_REPAIR_INSTRUCTIONS,
                                    ),
                                )
                            ),
                            ModelMessage(role="user", content=request),
                        ),
                        response_schema=_ROUTE_SCHEMA,
                        max_output_tokens=16_384,
                        repair_attempt=repair_attempt,
                        repair_limit=repair_limit,
                    )
                )
                return self._validate_turn(turn.structured_output, repair_attempt + 1)
            except AnbanError as exc:
                if exc.info.code is not ErrorCode.MODEL_RESPONSE_INVALID:
                    raise
            except (ValidationError, ValueError) as exc:
                last_validation_reason = self._validation_reason(exc)
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.MODEL_RESPONSE_INVALID,
                message="Task route response was invalid",
                details=SafeMetadata(
                    {
                        "reason": "task_route_invalid",
                        "last_validation_reason": last_validation_reason,
                    }
                ),
            )
        )

    @staticmethod
    def _validation_reason(exc: ValidationError | ValueError) -> str:
        rendered = str(exc)
        for reason in TaskGraphValidationReason:
            if reason.value in rendered:
                return reason.value
        stable_messages = {
            "fixed route cannot carry a graph": "fixed_route_graph_present",
            "Task graph requests unavailable external inputs": "external_input_unavailable",
        }
        return next(
            (value for message, value in stable_messages.items() if message in rendered),
            "response_shape_invalid",
        )

    @staticmethod
    def _validate_turn(
        structured_output: dict[str, JsonValue] | None,
        model_turn_count: int,
    ) -> TaskRouteDecision:
        if structured_output is None:
            raise ValueError("structured route is missing")
        decision = _ModelRouteDecision.model_validate(structured_output)
        if decision.route is TaskExecutionRoute.FIXED_AGENT:
            if decision.graph_spec:
                raise ValueError("fixed route cannot carry a graph")
            graph_spec = None
        else:
            graph_spec = TaskGraphSpec.model_validate(decision.graph_spec)
            if graph_spec.input_keys not in {(), (TASK_REQUEST_INPUT,)}:
                raise ValueError("Task graph requests unavailable external inputs")
        return TaskRouteDecision(
            route=decision.route,
            rationale=decision.rationale,
            graph_spec=graph_spec,
            model_turn_count=model_turn_count,
        )
