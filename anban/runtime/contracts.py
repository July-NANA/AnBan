"""Bounded values for the fixed v0.1 General Agent execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from anban.capability import ArtifactReference
from anban.config import policy
from anban.core.errors import ErrorInfo
from anban.core.ids import ExecutionRunId, NodeRunId, TaskId
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import UtcDateTime, now_utc


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionStrategy(StrEnum):
    """Provider-neutral ways the Main Agent may attempt a goal."""

    DIRECT_ANSWER = "direct_answer"
    USE_CAPABILITY = "use_capability"
    ACTIVATE_SKILL = "activate_skill"
    USE_PROCESS = "use_process"
    ACQUIRE_SKILL = "acquire_skill"
    DELEGATE = "delegate"
    CLARIFY = "clarify"
    FAIL = "fail"


class MainAgentPhase(StrEnum):
    PLANNING = "planning"
    EXECUTING = "executing"
    OBSERVING = "observing"
    WAITING = "waiting"
    TERMINAL = "terminal"


class ObservationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class AgentDecision(RuntimeValue):
    """One bounded, auditable Main Agent strategy decision."""

    strategy: ExecutionStrategy
    rationale: str = Field(min_length=1, max_length=2048)
    target: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z@][a-z0-9_.@/-]*$",
    )
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    created_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Agent decision rationale", max_length=2048)

    @model_validator(mode="after")
    def validate_strategy_shape(self) -> Self:
        if self.strategy in {
            ExecutionStrategy.DIRECT_ANSWER,
            ExecutionStrategy.CLARIFY,
            ExecutionStrategy.FAIL,
        } and (self.target is not None or self.arguments):
            raise ValueError("terminal and direct strategies cannot select an execution target")
        if (
            self.strategy
            in {
                ExecutionStrategy.USE_CAPABILITY,
                ExecutionStrategy.ACTIVATE_SKILL,
                ExecutionStrategy.DELEGATE,
            }
            and self.target is None
        ):
            raise ValueError("selected execution strategy requires a generic target")
        return self


class SufficiencyCandidate(RuntimeValue):
    """One generic strategy path considered against the current inventory."""

    strategy: ExecutionStrategy
    target: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z@][a-z0-9_.@:/-]*$",
    )
    available: bool
    rationale: str = Field(min_length=1, max_length=1024)
    confidence: float | None = Field(default=None, ge=0, le=1)
    missing_conditions: tuple[str, ...] = Field(default=(), max_length=16)
    risk_summary: str = Field(min_length=1, max_length=512)
    side_effect_summary: str = Field(min_length=1, max_length=512)

    @field_validator("rationale", "risk_summary", "side_effect_summary")
    @classmethod
    def validate_candidate_text(cls, value: str) -> str:
        return validate_safe_text(value, label="Sufficiency candidate text", max_length=1024)

    @field_validator("missing_conditions")
    @classmethod
    def validate_candidate_missing(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            validate_safe_text(value, label="Candidate missing condition", max_length=256)
            for value in values
        )

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available == bool(self.missing_conditions):
            raise ValueError("candidate availability and missing conditions disagree")
        return self


class SkillAcquisitionJustification(RuntimeValue):
    """General reasons that may justify acquisition; a missing matching Skill is insufficient."""

    substantial_temporary_code: bool = False
    complex_domain_workflow: bool = False
    high_improvisation_risk: bool = False
    low_implementation_confidence: bool = False
    repeated_reusable_need: bool = False
    existing_process_path_unreasonable: bool = False

    @property
    def justified(self) -> bool:
        return any(self.model_dump().values())


class CapabilitySufficiencyAssessment(RuntimeValue):
    """Truthful selection over all capability paths known at assessment time."""

    sufficient: bool
    candidates: tuple[SufficiencyCandidate, ...] = Field(min_length=1, max_length=32)
    selected: AgentDecision
    rationale: str = Field(min_length=1, max_length=2048)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=16)
    missing_conditions: tuple[str, ...] = Field(default=(), max_length=32)
    risk_summary: str = Field(min_length=1, max_length=1024)
    side_effect_summary: str = Field(min_length=1, max_length=1024)
    acquisition: SkillAcquisitionJustification = Field(
        default_factory=SkillAcquisitionJustification
    )
    should_acquire_skill: bool = False
    requires_clarification: bool = False
    must_fail: bool = False
    assessed_at: UtcDateTime = Field(default_factory=now_utc)

    @field_validator("rationale", "risk_summary", "side_effect_summary")
    @classmethod
    def validate_assessment_text(cls, value: str) -> str:
        return validate_safe_text(value, label="Sufficiency assessment text", max_length=2048)

    @field_validator("uncertainties", "missing_conditions")
    @classmethod
    def validate_assessment_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            validate_safe_text(value, label="Sufficiency condition", max_length=512)
            for value in values
        )

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        candidate_keys = [(candidate.strategy, candidate.target) for candidate in self.candidates]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("sufficiency candidates must be unique")
        resolutions = (
            int(self.should_acquire_skill) + int(self.requires_clarification) + int(self.must_fail)
        )
        if self.sufficient:
            if resolutions or self.missing_conditions:
                raise ValueError("sufficient assessment cannot request a fallback resolution")
            if self.selected.strategy in {
                ExecutionStrategy.ACQUIRE_SKILL,
                ExecutionStrategy.CLARIFY,
                ExecutionStrategy.FAIL,
            }:
                raise ValueError("sufficient assessment must select an executable existing path")
            if (self.selected.strategy, self.selected.target) not in candidate_keys:
                raise ValueError("selected strategy must be one of the assessed candidates")
        else:
            if resolutions != 1 or not self.missing_conditions:
                raise ValueError("insufficient assessment requires one explicit resolution")
            expected = (
                ExecutionStrategy.ACQUIRE_SKILL
                if self.should_acquire_skill
                else ExecutionStrategy.CLARIFY
                if self.requires_clarification
                else ExecutionStrategy.FAIL
            )
            if self.selected.strategy is not expected:
                raise ValueError("insufficient resolution and selected strategy disagree")
        if self.should_acquire_skill and not self.acquisition.justified:
            raise ValueError("Skill acquisition requires a general insufficiency justification")
        if not self.should_acquire_skill and self.acquisition.justified:
            raise ValueError("acquisition justification cannot be set for another resolution")
        return self


class AgentObservation(RuntimeValue):
    """Safe observation returned from a real strategy attempt."""

    sequence: int = Field(ge=1)
    strategy: ExecutionStrategy
    status: ObservationStatus
    summary: str = Field(min_length=1, max_length=4096)
    retry_safe: bool
    side_effect_completed: bool
    observed_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return validate_safe_text(
            value,
            label="Agent observation summary",
            max_length=4096,
            allow_absolute_paths=True,
        )


class CompletionAssessment(RuntimeValue):
    """Determine whether the original user goal is truthfully complete."""

    complete: bool
    rationale: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0, le=1)
    unmet_conditions: tuple[str, ...] = Field(default=(), max_length=32)
    final_text: str | None = Field(default=None, max_length=32_768)
    assessed_at: UtcDateTime = Field(default_factory=now_utc)

    @field_validator("rationale")
    @classmethod
    def validate_completion_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Completion rationale", max_length=2048)

    @field_validator("unmet_conditions")
    @classmethod
    def validate_unmet_conditions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            validate_safe_text(value, label="Unmet condition", max_length=512) for value in values
        )

    @model_validator(mode="after")
    def validate_completion_shape(self) -> Self:
        if self.complete:
            if self.unmet_conditions or self.final_text is None:
                raise ValueError("complete assessment requires final text and no unmet conditions")
            validate_safe_text(
                self.final_text,
                label="Completion final text",
                max_length=32_768,
                allow_absolute_paths=True,
            )
        elif self.final_text is not None:
            raise ValueError("incomplete assessment cannot contain final text")
        return self


class ReplanDecision(RuntimeValue):
    """A bounded choice to continue, clarify, fail, or select another path."""

    should_replan: bool
    rationale: str = Field(min_length=1, max_length=2048)
    next_strategy: ExecutionStrategy | None = None
    remaining_attempts: int = Field(ge=0, le=32)
    requires_clarification: bool = False
    must_fail: bool = False

    @field_validator("rationale")
    @classmethod
    def validate_replan_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Replan rationale", max_length=2048)

    @model_validator(mode="after")
    def validate_replan_shape(self) -> Self:
        terminal_choices = int(self.requires_clarification) + int(self.must_fail)
        if terminal_choices > 1:
            raise ValueError("replan decision cannot clarify and fail simultaneously")
        if self.should_replan:
            if self.next_strategy is None or terminal_choices or self.remaining_attempts == 0:
                raise ValueError("bounded replan requires a next strategy and remaining budget")
        elif self.next_strategy is not None:
            raise ValueError("non-replan decision cannot select a next strategy")
        return self


class MainAgentState(RuntimeValue):
    """Serializable state shared by fixed and future dynamic Main Agent graphs."""

    task_id: TaskId
    run_id: ExecutionRunId
    phase: MainAgentPhase
    goal: str = Field(min_length=1, max_length=32_768)
    decisions: tuple[AgentDecision, ...] = Field(default=(), max_length=64)
    observations: tuple[AgentObservation, ...] = Field(default=(), max_length=128)
    completion: CompletionAssessment | None = None
    replan: ReplanDecision | None = None
    terminal_error: ErrorInfo | None = None

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        return validate_safe_text(
            value,
            label="Main Agent goal",
            max_length=32_768,
            allow_absolute_paths=True,
        )

    @model_validator(mode="after")
    def validate_phase_shape(self) -> Self:
        terminal_facts = int(self.completion is not None) + int(self.terminal_error is not None)
        if self.phase is MainAgentPhase.TERMINAL and terminal_facts != 1:
            raise ValueError("terminal Main Agent state requires one terminal fact")
        if self.phase is not MainAgentPhase.TERMINAL and terminal_facts:
            raise ValueError("non-terminal Main Agent state cannot contain a terminal fact")
        sequences = [observation.sequence for observation in self.observations]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("Agent observations must use contiguous ordered sequence numbers")
        return self


class AgentOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentLimits(RuntimeValue):
    max_model_turns: int = Field(
        default=policy.AGENT_MAX_MODEL_TURNS_DEFAULT,
        ge=policy.AGENT_MAX_MODEL_TURNS_MIN,
        le=policy.AGENT_MAX_MODEL_TURNS_MAX,
    )
    max_capability_calls: int = Field(
        default=policy.AGENT_MAX_CAPABILITY_CALLS_DEFAULT,
        ge=policy.AGENT_MAX_CAPABILITY_CALLS_MIN,
        le=policy.AGENT_MAX_CAPABILITY_CALLS_MAX,
    )
    total_timeout_seconds: int = Field(
        default=policy.AGENT_TOTAL_TIMEOUT_DEFAULT_SECONDS,
        ge=policy.AGENT_TOTAL_TIMEOUT_MIN_SECONDS,
        le=policy.AGENT_TOTAL_TIMEOUT_MAX_SECONDS,
    )
    repeated_call_limit: int = Field(
        default=policy.AGENT_REPEATED_CALL_LIMIT_DEFAULT,
        ge=policy.AGENT_REPEATED_CALL_LIMIT_MIN,
        le=policy.AGENT_REPEATED_CALL_LIMIT_MAX,
    )

    @field_validator("repeated_call_limit")
    @classmethod
    def validate_repeated_call_limit(cls, value: int) -> int:
        if value == 1:
            raise ValueError("repeated call limit must be zero or at least two")
        return value


class AgentInput(RuntimeValue):
    request: str = Field(min_length=1, max_length=32_768)
    run_id: ExecutionRunId
    node_run_id: NodeRunId


class AgentOutcome(RuntimeValue):
    status: AgentOutcomeStatus
    final_text: str | None = Field(default=None, max_length=32_768)
    error: ErrorInfo | None = None
    model_turn_count: int = Field(ge=0, le=policy.AGENT_MAX_MODEL_TURNS_MAX)
    capability_call_count: int = Field(ge=0, le=policy.AGENT_MAX_CAPABILITY_CALLS_MAX)
    artifacts: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        if self.status is AgentOutcomeStatus.SUCCEEDED:
            if self.final_text is None or self.error is not None:
                raise ValueError("successful Agent outcome requires final text and no error")
            validate_safe_text(
                self.final_text,
                label="Agent final text",
                max_length=32_768,
                allow_absolute_paths=True,
            )
        elif self.error is None or self.final_text is not None:
            raise ValueError("non-successful Agent outcome requires an error and no final text")
        return self


class ExecutionResult(RuntimeValue):
    task_id: TaskId
    run_id: ExecutionRunId
    node_run_id: NodeRunId
    outcome: AgentOutcome
    persisted: bool
