"""Bounded values for the fixed v0.1 General Agent execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anban.capability import ArtifactReference
from anban.core.errors import ErrorInfo
from anban.core.ids import ExecutionRunId, NodeRunId
from anban.core.metadata import validate_safe_text


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentLimits(RuntimeValue):
    max_model_turns: int = Field(default=8, ge=1, le=8)
    max_capability_calls: int = Field(default=8, ge=1, le=8)
    total_timeout_seconds: int = Field(default=180, ge=1, le=180)
    repeated_call_limit: int = Field(default=3, ge=2, le=3)


class AgentInput(RuntimeValue):
    request: str = Field(min_length=1, max_length=32_768)
    run_id: ExecutionRunId
    node_run_id: NodeRunId


class AgentOutcome(RuntimeValue):
    status: AgentOutcomeStatus
    final_text: str | None = Field(default=None, max_length=32_768)
    error: ErrorInfo | None = None
    model_turn_count: int = Field(ge=0, le=8)
    capability_call_count: int = Field(ge=0, le=8)
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
