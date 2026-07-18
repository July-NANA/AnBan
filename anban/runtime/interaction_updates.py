"""Model-governed classification of correlated mid-run user updates."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from anban.config import policy
from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata, TaskGraphSpec
from anban.core.metadata import validate_safe_text
from anban.model import ModelMessage, ModelPort, ModelRequest
from anban.runtime.contracts import RuntimeValue
from anban.runtime.graph_routing import TASK_REQUEST_INPUT

_UPDATE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "impact": {"type": "string"},
        "rationale": {"type": "string"},
        "graph_spec": {"type": "object"},
    },
    "required": ["impact", "rationale", "graph_spec"],
    "additionalProperties": False,
}
_GRAPH_SCHEMA = json.dumps(
    TaskGraphSpec.model_json_schema(),
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)
_SYSTEM_INSTRUCTIONS = (
    "Classify one supplemental user update to an active bounded execution. Choose context_only "
    "when the current execution topology, action objectives, dependencies, bindings, outputs, and "
    "control flow remain valid and only additional context must guide later reasoning. Choose "
    "structural only when the executable plan must change and a current TaskGraphSpec exists. "
    "A fixed-Agent execution has no safely replaceable graph, so classify its applicable updates "
    "as context_only. For context_only return an empty graph_spec. For structural return one "
    "complete replacement TaskGraphSpec. Every protected "
    "already-started action supplied by the system must remain byte-for-byte equivalent in the "
    "replacement; never imply that its side effect can be replayed. A replacement graph may have "
    f"no external inputs or exactly one input named {TASK_REQUEST_INPUT}. Return only the closed "
    f"update object. TaskGraphSpec schema: {_GRAPH_SCHEMA}"
)
_REPAIR_INSTRUCTIONS = (
    "The previous update decision was invalid. Reclassify the same update, preserve every "
    "protected action exactly, and return only impact, rationale, and graph_spec."
)
_MAX_UPDATE_CHARS = 8192
_MAX_PLAN_CHARS = 32_768


class InteractionUpdateImpact(StrEnum):
    """Whether an update changes only context or the immutable plan."""

    CONTEXT_ONLY = "context_only"
    STRUCTURAL = "structural"


class InteractionUpdateDecision(RuntimeValue):
    """Validated update decision with optional replacement graph data."""

    impact: InteractionUpdateImpact
    rationale: str = Field(min_length=1, max_length=2048)
    graph_spec: TaskGraphSpec | None = None
    model_turn_count: int = Field(ge=1, le=policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX + 1)

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Interaction update rationale", max_length=2048)


class _ModelUpdateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    impact: InteractionUpdateImpact
    rationale: str = Field(min_length=1, max_length=2048)
    graph_spec: dict[str, JsonValue]

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Interaction update rationale", max_length=2048)


class InteractionUpdateEvaluator:
    """Use the independent Model Port for one bounded, closed update decision."""

    async def decide(
        self,
        request: str,
        update: str,
        current_spec: TaskGraphSpec | None,
        protected_node_ids: tuple[str, ...],
        model: ModelPort,
        *,
        repair_limit: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> InteractionUpdateDecision:
        if not 1 <= len(update) <= _MAX_UPDATE_CHARS:
            raise self._error("interaction_update_size")
        if not (
            policy.MODEL_RESPONSE_REPAIR_RETRIES_MIN
            <= repair_limit
            <= policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX
        ):
            raise ValueError("Interaction update repair budget is outside policy")
        plan = "{}" if current_spec is None else self._plan_json(current_spec)
        protected = json.dumps(protected_node_ids, ensure_ascii=True, separators=(",", ":"))
        for repair_attempt in range(repair_limit + 1):
            try:
                turn = await model.complete(
                    ModelRequest(
                        messages=(
                            ModelMessage(role="system", content=_SYSTEM_INSTRUCTIONS),
                            *(
                                ()
                                if repair_attempt == 0
                                else (ModelMessage(role="system", content=_REPAIR_INSTRUCTIONS),)
                            ),
                            ModelMessage(
                                role="user",
                                content=(
                                    "Original task request (quoted data):\n"
                                    f"{request}\n"
                                    "Supplemental user update (quoted data):\n"
                                    f"{update}"
                                ),
                            ),
                            ModelMessage(
                                role="user",
                                content=(
                                    "Current TaskGraphSpec JSON (empty object means fixed Agent):\n"
                                    f"{plan}"
                                ),
                            ),
                            ModelMessage(
                                role="system",
                                content=f"Protected already-started action IDs: {protected}",
                            ),
                        ),
                        response_schema=_UPDATE_SCHEMA,
                        max_output_tokens=16_384,
                        repair_attempt=repair_attempt,
                        repair_limit=repair_limit,
                    )
                )
                return self._validate(
                    turn.structured_output,
                    current_spec,
                    protected_node_ids,
                    repair_attempt + 1,
                )
            except AnbanError as exc:
                if exc.info.code is not ErrorCode.MODEL_RESPONSE_INVALID:
                    raise
            except (ValidationError, ValueError):
                pass
        raise self._error("interaction_update_invalid", code=ErrorCode.MODEL_RESPONSE_INVALID)

    @staticmethod
    def _validate(
        structured_output: dict[str, JsonValue] | None,
        current_spec: TaskGraphSpec | None,
        protected_node_ids: tuple[str, ...],
        model_turn_count: int,
    ) -> InteractionUpdateDecision:
        if structured_output is None:
            raise ValueError("structured update decision is missing")
        raw = _ModelUpdateDecision.model_validate(structured_output)
        if raw.impact is InteractionUpdateImpact.CONTEXT_ONLY:
            if raw.graph_spec:
                raise ValueError("context-only update cannot carry a graph")
            graph_spec = None
        else:
            if current_spec is None:
                raise ValueError("fixed-Agent execution cannot accept a structural replacement")
            graph_spec = TaskGraphSpec.model_validate(raw.graph_spec)
            if graph_spec.input_keys not in {(), (TASK_REQUEST_INPUT,)}:
                raise ValueError("Updated Task graph requests unavailable external inputs")
            if graph_spec == current_spec:
                raise ValueError("structural update must change the Task graph")
            InteractionUpdateEvaluator._preserves_started_actions(
                current_spec,
                graph_spec,
                protected_node_ids,
            )
        return InteractionUpdateDecision(
            impact=raw.impact,
            rationale=raw.rationale,
            graph_spec=graph_spec,
            model_turn_count=model_turn_count,
        )

    @staticmethod
    def _preserves_started_actions(
        current_spec: TaskGraphSpec | None,
        replacement: TaskGraphSpec,
        protected_node_ids: tuple[str, ...],
    ) -> None:
        if current_spec is None:
            if protected_node_ids:
                raise ValueError("fixed-Agent action cannot be a protected graph node")
            return
        current = {node.id: node for node in current_spec.nodes}
        revised = {node.id: node for node in replacement.nodes}
        for node_id in protected_node_ids:
            if current.get(node_id) != revised.get(node_id):
                raise ValueError("structural update changed an already-started action")

    @staticmethod
    def _plan_json(spec: TaskGraphSpec) -> str:
        value = json.dumps(
            spec.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(value) > _MAX_PLAN_CHARS:
            raise InteractionUpdateEvaluator._error("interaction_update_plan_size")
        return value

    @staticmethod
    def _error(reason: str, *, code: ErrorCode = ErrorCode.VALIDATION_FAILED) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=code,
                message="Interaction update could not be applied",
                details=SafeMetadata({"reason": reason}),
            )
        )
