"""Structured sufficiency evaluation against real replaceable inventory facts."""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityResult,
    InventoryKind,
    InvocationContext,
    SkillPackage,
    UnifiedCapabilityInventory,
)
from anban.core.errors import AnbanError, ErrorCode
from anban.model import ModelRequest, ModelTurn
from anban.runtime import CapabilitySufficiencyEvaluator, ExecutionStrategy


class NeverInvokedHandler:
    def __init__(self, name: str, kind: InventoryKind) -> None:
        self.descriptor = CapabilityDescriptor(
            name=name,
            description="Perform a bounded dynamically named operation.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            inventory_kind=kind,
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        raise AssertionError("sufficiency evaluation invoked a Capability")

    async def cancel(self, context: InvocationContext) -> None:
        raise AssertionError("sufficiency evaluation cancelled a Capability")


class DecisionModel:
    def __init__(self, output: dict[str, JsonValue]) -> None:
        self.output = output
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        return ModelTurn(structured_output=self.output, finish_reason="stop")


def decision(
    strategy: ExecutionStrategy,
    *,
    target: str = "",
    missing_condition: str = "",
    **updates: JsonValue,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "strategy": strategy.value,
        "target": target,
        "rationale": "The current inventory supports this general strategy selection.",
        "confidence": 0.82,
        "missing_condition": missing_condition,
        "substantial_temporary_code": False,
        "complex_domain_workflow": False,
        "high_improvisation_risk": False,
        "low_implementation_confidence": False,
        "repeated_reusable_need": False,
        "existing_process_path_unreasonable": False,
    }
    payload.update(updates)
    return payload


def evaluator(
    *handlers: NeverInvokedHandler,
    model_available: bool = True,
    skills: tuple[SkillPackage, ...] = (),
) -> CapabilitySufficiencyEvaluator:
    inventory = UnifiedCapabilityInventory(
        CapabilityRegistry(handlers),
        skills,
        model_available=model_available,
    )
    return CapabilitySufficiencyEvaluator(inventory)


def dynamic_skill() -> SkillPackage:
    name = f"skill-{uuid4().hex[:12]}"
    instructions = f"---\nname: {name}\ndescription: Execute a dynamic workflow.\n---\n"
    return SkillPackage(
        slug=f"@fixture/{name}",
        name=name,
        description="Execute a dynamic workflow through ordinary activation.",
        skill_root=f"skills/@fixture/{name}",
        content_hash=hashlib.sha256(instructions.encode()).hexdigest(),
        instructions=instructions,
    )


async def test_direct_answer_assesses_every_inventory_category() -> None:
    model = DecisionModel(decision(ExecutionStrategy.DIRECT_ANSWER))
    result = await evaluator(
        NeverInvokedHandler(f"fixture.{uuid4().hex}", InventoryKind.CAPABILITY),
        NeverInvokedHandler(f"fixture.{uuid4().hex}", InventoryKind.PROCESS),
        skills=(dynamic_skill(),),
    ).assess(f"Explain a bounded concept using task {uuid4().hex}.", model)

    assert result.sufficient
    assert result.selected.strategy is ExecutionStrategy.DIRECT_ANSWER
    strategies = {candidate.strategy for candidate in result.candidates}
    assert strategies >= {
        ExecutionStrategy.DIRECT_ANSWER,
        ExecutionStrategy.USE_CAPABILITY,
        ExecutionStrategy.DELEGATE,
    }
    request = model.requests[0]
    assert request.response_schema is not None
    assert request.tools == ()
    inventory_context = request.messages[-1].content or ""
    for kind in InventoryKind:
        assert f'"kind":"{kind.value}"' in inventory_context


async def test_ready_process_is_selected_without_any_skill() -> None:
    process_name = f"fixture.{uuid4().hex}"
    model = DecisionModel(decision(ExecutionStrategy.USE_PROCESS, target=process_name))
    result = await evaluator(NeverInvokedHandler(process_name, InventoryKind.PROCESS)).assess(
        f"Transform the generated input {uuid4().hex} with a local program.", model
    )

    assert result.sufficient
    assert result.selected.strategy is ExecutionStrategy.USE_PROCESS
    assert result.selected.target == process_name
    assert not result.should_acquire_skill


@pytest.mark.parametrize(
    ("kind", "strategy"),
    [
        (InventoryKind.MCP, ExecutionStrategy.USE_CAPABILITY),
        (InventoryKind.MEMORY, ExecutionStrategy.USE_CAPABILITY),
        (InventoryKind.SUB_AGENT, ExecutionStrategy.DELEGATE),
    ],
)
async def test_structured_platform_paths_use_inventory_targets(
    kind: InventoryKind,
    strategy: ExecutionStrategy,
) -> None:
    target = f"fixture.{uuid4().hex}"
    model = DecisionModel(decision(strategy, target=target))
    result = await evaluator(NeverInvokedHandler(target, kind)).assess(
        f"Use a dynamically supplied {kind.value} path.", model
    )

    assert result.sufficient
    assert result.selected.strategy is strategy
    assert result.selected.target == target


async def test_unique_ready_target_may_be_inferred_from_structured_decision() -> None:
    process_name = f"fixture.{uuid4().hex}"
    model = DecisionModel(decision(ExecutionStrategy.USE_PROCESS))
    result = await evaluator(NeverInvokedHandler(process_name, InventoryKind.PROCESS)).assess(
        "Use the single bounded process path.", model
    )

    assert result.selected.target == process_name


async def test_missing_skill_alone_never_authorizes_acquisition() -> None:
    model = DecisionModel(
        decision(
            ExecutionStrategy.ACQUIRE_SKILL,
            missing_condition="No matching ready Skill is present.",
        )
    )
    with pytest.raises(AnbanError) as failure:
        await evaluator().assess("Complete an unfamiliar domain workflow.", model)
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert failure.value.info.details.root["reason"] == "skill_acquisition_unjustified"


async def test_general_insufficiency_can_authorize_skill_acquisition() -> None:
    process_name = f"fixture.{uuid4().hex}"
    model = DecisionModel(
        decision(
            ExecutionStrategy.ACQUIRE_SKILL,
            missing_condition="A governed reusable domain workflow is required.",
            complex_domain_workflow=True,
            existing_process_path_unreasonable=True,
        )
    )
    result = await evaluator(NeverInvokedHandler(process_name, InventoryKind.PROCESS)).assess(
        "Complete a complex reusable domain workflow.", model
    )

    assert not result.sufficient
    assert result.should_acquire_skill
    assert result.acquisition.complex_domain_workflow


@pytest.mark.parametrize(
    ("strategy", "flag"),
    [
        (ExecutionStrategy.CLARIFY, "requires_clarification"),
        (ExecutionStrategy.FAIL, "must_fail"),
    ],
)
async def test_clarify_and_fail_are_explicit_resolutions(
    strategy: ExecutionStrategy,
    flag: str,
) -> None:
    model = DecisionModel(
        decision(
            strategy,
            missing_condition="A required condition is not currently available.",
        )
    )
    result = await evaluator().assess(f"Resolve dynamic goal {uuid4().hex}.", model)

    assert not result.sufficient
    assert getattr(result, flag) is True


async def test_unavailable_or_unknown_selection_fails_closed() -> None:
    for target in ("mcp:runtime", f"unknown:{uuid4().hex}"):
        model = DecisionModel(decision(ExecutionStrategy.USE_CAPABILITY, target=target))
        with pytest.raises(AnbanError) as failure:
            await evaluator().assess("Use a structured protocol path.", model)
        assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID


async def test_same_strategy_with_multiple_targets_requires_exact_selection() -> None:
    first, second = (f"fixture.{uuid4().hex}" for _ in range(2))
    model = DecisionModel(decision(ExecutionStrategy.USE_CAPABILITY))
    with pytest.raises(AnbanError) as failure:
        await evaluator(
            NeverInvokedHandler(first, InventoryKind.CAPABILITY),
            NeverInvokedHandler(second, InventoryKind.CAPABILITY),
        ).assess("Use one of several structured operations.", model)
    assert failure.value.info.details.root["reason"] == "selection_not_unique"
