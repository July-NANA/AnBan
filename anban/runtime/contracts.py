"""Bounded values for the fixed v0.1 General Agent execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anban.capability import ArtifactReference
from anban.config import policy
from anban.core.errors import ErrorInfo
from anban.core.ids import ExecutionRunId, NodeRunId, TaskId
from anban.core.metadata import validate_safe_text


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
            validate_safe_text(self.final_text, label="Agent final text", max_length=32_768)
        elif self.error is None or self.final_text is not None:
            raise ValueError("non-successful Agent outcome requires an error and no final text")
        return self


class ExecutionResult(RuntimeValue):
    task_id: TaskId
    run_id: ExecutionRunId
    node_run_id: NodeRunId
    outcome: AgentOutcome
    persisted: bool
