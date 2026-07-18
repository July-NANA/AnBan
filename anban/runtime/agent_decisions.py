"""Bounded General Agent decision guidance and initial-path checks."""

from __future__ import annotations

import json
from collections.abc import Callable

from anban.capability import CapabilityDescriptor, CapabilityKind
from anban.model import ToolCall
from anban.runtime.contracts import CapabilitySufficiencyAssessment, ExecutionStrategy


def assessment_guidance(assessment: CapabilitySufficiencyAssessment) -> str:
    """Project the bounded assessment into safe, finite model guidance."""

    target = assessment.selected.target or "none"
    candidates = json.dumps(
        [
            {
                "strategy": candidate.strategy.value,
                "target": candidate.target,
                "summary": candidate.rationale[:256],
            }
            for candidate in assessment.candidates
            if candidate.available
        ][:16],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        "Initial bounded sufficiency assessment selected "
        f"strategy={assessment.selected.strategy.value} target={target}. "
        f"Ready inventory candidates={candidates}. "
        "Use candidate facts only according to domain fit and the user's authority. When an "
        "acquisition resolution is selected, any real search and install must use existing "
        "Skills and Capabilities, remain in this Run, and return to the original goal. "
        "Use this as the starting path. Return every real Tool Result to reasoning, choose a "
        "distinct available alternative when an observation disproves the path, and never "
        "repeat an identical completed or uncertain Capability call."
    )


def matches_initial_decision(
    call: ToolCall,
    assessment: CapabilitySufficiencyAssessment,
    describe: Callable[[str], CapabilityDescriptor],
) -> bool:
    """Confirm that the first real action follows a sufficient selected path."""

    selected = assessment.selected
    return matches_strategy_target(call, selected.strategy, selected.target, describe)


def matches_strategy_target(
    call: ToolCall,
    strategy: ExecutionStrategy,
    target: str | None,
    describe: Callable[[str], CapabilityDescriptor],
) -> bool:
    """Confirm one native Tool Call matches a bounded strategy/target pair."""

    if strategy is ExecutionStrategy.DIRECT_ANSWER:
        return False
    if strategy is ExecutionStrategy.ACTIVATE_SKILL:
        descriptor = describe(call.name)
        return descriptor.kind is CapabilityKind.SKILL and call.arguments.get("name") == target
    return call.name == target
