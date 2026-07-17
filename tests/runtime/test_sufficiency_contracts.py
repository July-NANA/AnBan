"""Capability sufficiency and strategy selection contract tests."""

import pytest
from pydantic import ValidationError

from anban.runtime import (
    AgentDecision,
    CapabilitySufficiencyAssessment,
    ExecutionStrategy,
    SkillAcquisitionJustification,
    SufficiencyCandidate,
)


def candidate(
    strategy: ExecutionStrategy,
    *,
    target: str | None = None,
    available: bool = True,
) -> SufficiencyCandidate:
    return SufficiencyCandidate(
        strategy=strategy,
        target=target,
        available=available,
        rationale="The path was evaluated against the current bounded inventory.",
        confidence=0.8 if available else 0.2,
        missing_conditions=() if available else ("A required runtime condition is missing.",),
        risk_summary="Risk is bounded and explicit.",
        side_effect_summary="Side effects are known before selection.",
    )


def assessment(**updates: object) -> CapabilitySufficiencyAssessment:
    values: dict[str, object] = {
        "sufficient": True,
        "candidates": (
            candidate(ExecutionStrategy.DIRECT_ANSWER),
            candidate(ExecutionStrategy.USE_PROCESS),
        ),
        "selected": AgentDecision(
            strategy=ExecutionStrategy.USE_PROCESS,
            rationale="The bounded Process path is sufficient.",
            confidence=0.8,
        ),
        "rationale": "Existing paths can complete the goal without acquisition.",
        "confidence": 0.8,
        "risk_summary": "The selected path has a bounded risk profile.",
        "side_effect_summary": "The selected path may perform a declared local side effect.",
    }
    values.update(updates)
    return CapabilitySufficiencyAssessment.model_validate(values)


def test_sufficient_assessment_selects_an_evaluated_existing_path() -> None:
    result = assessment()
    assert result.sufficient is True
    assert result.selected.strategy is ExecutionStrategy.USE_PROCESS
    assert CapabilitySufficiencyAssessment.model_validate_json(result.model_dump_json()) == result


def test_missing_skill_alone_cannot_justify_acquisition() -> None:
    with pytest.raises(ValidationError):
        assessment(
            sufficient=False,
            candidates=(candidate(ExecutionStrategy.USE_PROCESS, available=False),),
            selected=AgentDecision(
                strategy=ExecutionStrategy.ACQUIRE_SKILL,
                rationale="No matching Skill was found.",
            ),
            missing_conditions=("No matching ready Skill was found.",),
            should_acquire_skill=True,
        )


@pytest.mark.parametrize(
    "justification",
    [
        SkillAcquisitionJustification(complex_domain_workflow=True),
        SkillAcquisitionJustification(high_improvisation_risk=True),
        SkillAcquisitionJustification(low_implementation_confidence=True),
    ],
)
def test_general_insufficiency_can_select_bounded_skill_acquisition(
    justification: SkillAcquisitionJustification,
) -> None:
    result = assessment(
        sufficient=False,
        candidates=(candidate(ExecutionStrategy.USE_PROCESS, available=False),),
        selected=AgentDecision(
            strategy=ExecutionStrategy.ACQUIRE_SKILL,
            rationale="Existing execution paths are demonstrably insufficient.",
        ),
        missing_conditions=("A reusable domain workflow is required.",),
        should_acquire_skill=True,
        acquisition=justification,
    )
    assert result.should_acquire_skill is True


@pytest.mark.parametrize(
    ("flag", "strategy"),
    [
        ("requires_clarification", ExecutionStrategy.CLARIFY),
        ("must_fail", ExecutionStrategy.FAIL),
    ],
)
def test_insufficient_assessment_has_one_explicit_non_success_resolution(
    flag: str, strategy: ExecutionStrategy
) -> None:
    result = assessment(
        sufficient=False,
        candidates=(
            candidate(ExecutionStrategy.USE_CAPABILITY, target="generic.action", available=False),
        ),
        selected=AgentDecision(
            strategy=strategy,
            rationale="No safe executable path is currently available.",
        ),
        missing_conditions=("A required input or capability is unavailable.",),
        **{flag: True},
    )
    assert result.selected.strategy is strategy


def test_selected_path_must_appear_in_unique_candidates() -> None:
    duplicated = candidate(ExecutionStrategy.USE_PROCESS)
    with pytest.raises(ValidationError):
        assessment(candidates=(duplicated, duplicated))
    with pytest.raises(ValidationError):
        assessment(
            selected=AgentDecision(
                strategy=ExecutionStrategy.USE_CAPABILITY,
                target="unlisted.action",
                rationale="This path was not assessed.",
            )
        )


def test_candidate_availability_and_missing_conditions_are_consistent() -> None:
    with pytest.raises(ValidationError):
        SufficiencyCandidate(
            strategy=ExecutionStrategy.USE_PROCESS,
            available=True,
            rationale="The candidate shape is contradictory.",
            missing_conditions=("Unexpected missing condition.",),
            risk_summary="Risk is bounded.",
            side_effect_summary="Side effects are bounded.",
        )
