"""Contract tests for the provider-neutral v0.5 Main Agent state."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from anban.core import ErrorCode, ErrorInfo, ExecutionRunId, TaskId
from anban.runtime import (
    AgentDecision,
    AgentObservation,
    CompletionAssessment,
    ExecutionStrategy,
    MainAgentPhase,
    MainAgentState,
    ObservationStatus,
    ReplanDecision,
)


def identities() -> tuple[TaskId, ExecutionRunId]:
    return TaskId(uuid4()), ExecutionRunId(uuid4())


def test_strategy_vocabulary_is_closed_and_serializable() -> None:
    assert {item.value for item in ExecutionStrategy} == {
        "direct_answer",
        "use_capability",
        "activate_skill",
        "use_process",
        "acquire_skill",
        "delegate",
        "clarify",
        "fail",
    }
    decision = AgentDecision(
        strategy=ExecutionStrategy.USE_CAPABILITY,
        target="document.transform",
        arguments={"input": "bounded"},
        rationale="The available structured capability matches the requested operation.",
        confidence=0.8,
    )
    assert AgentDecision.model_validate_json(decision.model_dump_json()) == decision
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"strategy": "unknown", "rationale": "Unsupported strategy."})


@pytest.mark.parametrize(
    ("strategy", "target"),
    [
        (ExecutionStrategy.USE_CAPABILITY, "generic.action"),
        (ExecutionStrategy.ACTIVATE_SKILL, "@owner/skill-name"),
        (ExecutionStrategy.DELEGATE, "agent.delegate"),
    ],
)
def test_executable_decisions_use_one_generic_target(
    strategy: ExecutionStrategy, target: str
) -> None:
    assert (
        AgentDecision(
            strategy=strategy,
            target=target,
            rationale="The selected generic target is currently available.",
        ).target
        == target
    )


def test_terminal_and_direct_decisions_cannot_smuggle_execution() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            strategy=ExecutionStrategy.DIRECT_ANSWER,
            target="hidden.execute",
            rationale="This must be rejected.",
        )
    with pytest.raises(ValidationError):
        AgentDecision(
            strategy=ExecutionStrategy.FAIL,
            arguments={"pretend": True},
            rationale="This must be rejected.",
        )


def test_state_distinguishes_execution_observation_and_success() -> None:
    task_id, run_id = identities()
    decision = AgentDecision(
        strategy=ExecutionStrategy.USE_PROCESS,
        rationale="A bounded terminal operation is sufficient.",
    )
    observation = AgentObservation(
        sequence=1,
        strategy=decision.strategy,
        status=ObservationStatus.COMPLETED,
        summary="The real operation completed and returned bounded output.",
        retry_safe=False,
        side_effect_completed=True,
    )
    state = MainAgentState(
        task_id=task_id,
        run_id=run_id,
        phase=MainAgentPhase.OBSERVING,
        goal="Complete a newly supplied bounded task.",
        decisions=(decision,),
        observations=(observation,),
    )
    terminal = MainAgentState(
        **state.model_dump(exclude={"phase", "completion"}),
        phase=MainAgentPhase.TERMINAL,
        completion=CompletionAssessment(
            complete=True,
            rationale="The observation satisfies the original goal.",
            confidence=0.9,
            final_text="The bounded task completed successfully.",
        ),
    )
    assert MainAgentState.model_validate_json(terminal.model_dump_json()) == terminal


def test_state_rejects_out_of_order_observations_and_ambiguous_terminal_facts() -> None:
    task_id, run_id = identities()
    observation = AgentObservation(
        sequence=2,
        strategy=ExecutionStrategy.USE_PROCESS,
        status=ObservationStatus.FAILED,
        summary="The process failed explicitly.",
        retry_safe=True,
        side_effect_completed=False,
    )
    with pytest.raises(ValidationError):
        MainAgentState(
            task_id=task_id,
            run_id=run_id,
            phase=MainAgentPhase.OBSERVING,
            goal="Exercise an unseen failure path.",
            observations=(observation,),
        )
    with pytest.raises(ValidationError):
        MainAgentState(
            task_id=task_id,
            run_id=run_id,
            phase=MainAgentPhase.TERMINAL,
            goal="Exercise an unseen terminal path.",
            completion=CompletionAssessment(
                complete=True,
                rationale="The goal is complete.",
                confidence=1,
                final_text="Complete.",
            ),
            terminal_error=ErrorInfo(
                code=ErrorCode.CAPABILITY_UNAVAILABLE,
                message="A capability is unavailable",
            ),
        )


def test_incomplete_completion_and_bounded_replan_are_explicit() -> None:
    incomplete = CompletionAssessment(
        complete=False,
        rationale="One required condition remains unmet.",
        confidence=0.6,
        unmet_conditions=("A compatible capability must become available.",),
    )
    replan = ReplanDecision(
        should_replan=True,
        rationale="One distinct safe path remains.",
        next_strategy=ExecutionStrategy.USE_PROCESS,
        next_target="process.execute",
        remaining_attempts=1,
    )
    assert incomplete.complete is False
    assert replan.next_strategy is ExecutionStrategy.USE_PROCESS
    clarification = ReplanDecision(
        should_replan=False,
        rationale="A required user input is missing.",
        remaining_attempts=0,
        requires_clarification=True,
    )
    failure = ReplanDecision(
        should_replan=False,
        rationale="Every safe path is exhausted.",
        remaining_attempts=0,
        must_fail=True,
    )
    assert clarification.requires_clarification
    assert failure.must_fail
    with pytest.raises(ValidationError):
        ReplanDecision(
            should_replan=True,
            rationale="No budget remains.",
            next_strategy=ExecutionStrategy.FAIL,
            remaining_attempts=0,
        )


@pytest.mark.parametrize("terminal", [ExecutionStrategy.CLARIFY, ExecutionStrategy.FAIL])
def test_clarify_and_fail_cannot_masquerade_as_replan_strategies(
    terminal: ExecutionStrategy,
) -> None:
    with pytest.raises(ValidationError):
        ReplanDecision(
            should_replan=True,
            rationale="Terminal resolution must use its explicit flag.",
            next_strategy=terminal,
            remaining_attempts=1,
        )


def test_contracts_reject_unknown_fields_and_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {
                "strategy": "use_process",
                "rationale": "A bounded operation is sufficient.",
                "provider": "forbidden-provider-field",
            }
        )
    with pytest.raises(ValidationError):
        AgentDecision(
            strategy=ExecutionStrategy.USE_PROCESS,
            rationale="A bounded operation is sufficient.",
            created_at=datetime.now(),
        )
    assert AgentDecision(
        strategy=ExecutionStrategy.USE_PROCESS,
        rationale="A bounded operation is sufficient.",
        created_at=datetime.now(UTC),
    )
