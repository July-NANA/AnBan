"""Closed model-governed classification of mid-run Interaction updates."""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from anban.core import AnbanError, TaskGraphSpec
from anban.model import ModelRequest, ModelTurn
from anban.runtime.interaction_updates import (
    InteractionUpdateEvaluator,
    InteractionUpdateImpact,
)
from tests.core.test_graph import loop_graph


class UpdateModel:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = turns
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        return self.turns.pop(0)


def update_turn(impact: str, graph_spec: dict[str, JsonValue] | None = None) -> ModelTurn:
    return ModelTurn(
        structured_output={
            "impact": impact,
            "rationale": "The supplemental requirement has a bounded effect on the plan.",
            "graph_spec": graph_spec or {},
        },
        finish_reason="stop",
    )


def revised_loop(current: TaskGraphSpec) -> TaskGraphSpec:
    values = current.model_dump(mode="json")
    values["nodes"][3]["objective"] = "Publish the result with the supplemental requirement."
    return TaskGraphSpec.model_validate(values)


async def test_context_only_update_retains_the_current_revision() -> None:
    current = loop_graph()
    model = UpdateModel([update_turn("context_only")])

    decision = await InteractionUpdateEvaluator().decide(
        "Complete the original bounded task.",
        "Use the newly supplied presentation preference.",
        current,
        ("seed",),
        model,
    )

    assert decision.impact is InteractionUpdateImpact.CONTEXT_ONLY
    assert decision.graph_spec is None
    assert model.requests[0].response_schema is not None


async def test_structural_update_accepts_a_new_revision_and_preserves_started_action() -> None:
    current = loop_graph()
    revised = revised_loop(current)
    model = UpdateModel([update_turn("structural", revised.model_dump(mode="json"))])

    decision = await InteractionUpdateEvaluator().decide(
        "Complete the original bounded task.",
        "Add a required publishing step to the remaining plan.",
        current,
        ("seed",),
        model,
    )

    assert decision.impact is InteractionUpdateImpact.STRUCTURAL
    assert decision.graph_spec == revised


async def test_structural_update_cannot_rewrite_an_already_started_action() -> None:
    current = loop_graph()
    changed = revised_loop(current).model_dump(mode="json")
    changed["nodes"][0]["objective"] = "Replay a different operation, which is forbidden."
    invalid = TaskGraphSpec.model_validate(changed)
    turn = update_turn("structural", invalid.model_dump(mode="json"))

    with pytest.raises(AnbanError) as captured:
        await InteractionUpdateEvaluator().decide(
            "Complete the original bounded task.",
            "Change future work without replay.",
            current,
            ("seed",),
            UpdateModel([turn, turn]),
            repair_limit=1,
        )

    assert captured.value.info.details.root["reason"] == "interaction_update_invalid"


async def test_structural_update_rejects_fixed_agent_execution() -> None:
    turn = update_turn("structural", loop_graph().model_dump(mode="json"))

    with pytest.raises(AnbanError) as captured:
        await InteractionUpdateEvaluator().decide(
            "Complete the original bounded task.",
            "Replace the fixed execution plan.",
            None,
            (),
            UpdateModel([turn]),
            repair_limit=0,
        )

    assert captured.value.info.details.root["reason"] == "interaction_update_invalid"
