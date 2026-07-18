"""Safe Event metadata for Main Agent assessments and decisions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from anban.core.metadata import SafeMetadata
from anban.runtime.contracts import (
    AgentObservation,
    CapabilitySufficiencyAssessment,
    CompletionAssessment,
    ReplanDecision,
)


@dataclass(frozen=True)
class AgentEventFact:
    event_type: str
    metadata: SafeMetadata


def sufficiency_event_facts(
    assessment: CapabilitySufficiencyAssessment,
) -> tuple[AgentEventFact, ...]:
    metadata = SafeMetadata(
        {
            "strategy": assessment.selected.strategy.value,
            "target": assessment.selected.target,
            "sufficient": assessment.sufficient,
            "candidate_count": len(assessment.candidates),
            "confidence": assessment.confidence,
            "should_acquire_skill": assessment.should_acquire_skill,
            "requires_clarification": assessment.requires_clarification,
            "must_fail": assessment.must_fail,
            "rationale_hash": hashlib.sha256(assessment.rationale.encode()).hexdigest(),
        }
    )
    facts = [AgentEventFact("agent.sufficiency_assessed", metadata)]
    if assessment.should_acquire_skill:
        acquisition = assessment.acquisition
        facts.append(
            AgentEventFact(
                "agent.skill_acquisition_requested",
                SafeMetadata(
                    {
                        "substantial_temporary_code": acquisition.substantial_temporary_code,
                        "complex_domain_workflow": acquisition.complex_domain_workflow,
                        "high_improvisation_risk": acquisition.high_improvisation_risk,
                        "low_implementation_confidence": (
                            acquisition.low_implementation_confidence
                        ),
                        "repeated_reusable_need": acquisition.repeated_reusable_need,
                        "existing_process_path_unreasonable": (
                            acquisition.existing_process_path_unreasonable
                        ),
                    }
                ),
            )
        )
    return tuple(facts)


def observation_event_facts(observation: AgentObservation) -> tuple[AgentEventFact, ...]:
    return (
        AgentEventFact(
            "agent.observed",
            SafeMetadata(
                {
                    "observation_sequence": observation.sequence,
                    "strategy": observation.strategy.value,
                    "target": observation.target,
                    "observation_status": observation.status.value,
                    "retry_safe": observation.retry_safe,
                    "side_effect_completed": observation.side_effect_completed,
                    "summary_hash": hashlib.sha256(observation.summary.encode()).hexdigest(),
                }
            ),
        ),
    )


def completion_event_facts(assessment: CompletionAssessment) -> tuple[AgentEventFact, ...]:
    return (
        AgentEventFact(
            "agent.completion_assessed",
            SafeMetadata(
                {
                    "complete": assessment.complete,
                    "confidence": assessment.confidence,
                    "unmet_condition_count": len(assessment.unmet_conditions),
                    "rationale_hash": hashlib.sha256(assessment.rationale.encode()).hexdigest(),
                    **(
                        {}
                        if assessment.final_text is None
                        else {
                            "final_hash": hashlib.sha256(assessment.final_text.encode()).hexdigest()
                        }
                    ),
                }
            ),
        ),
    )


def replan_event_facts(decision: ReplanDecision) -> tuple[AgentEventFact, ...]:
    metadata = SafeMetadata(
        {
            "should_replan": decision.should_replan,
            "next_strategy": (
                None if decision.next_strategy is None else decision.next_strategy.value
            ),
            "next_target": decision.next_target,
            "remaining_attempts": decision.remaining_attempts,
            "requires_clarification": decision.requires_clarification,
            "must_fail": decision.must_fail,
            "rationale_hash": hashlib.sha256(decision.rationale.encode()).hexdigest(),
        }
    )
    facts = [AgentEventFact("agent.replan_decided", metadata)]
    if decision.requires_clarification:
        facts.append(AgentEventFact("agent.clarification_requested", metadata))
    elif decision.must_fail:
        facts.append(AgentEventFact("agent.failure_selected", metadata))
    return tuple(facts)
