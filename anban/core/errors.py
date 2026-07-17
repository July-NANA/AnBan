"""Stable, safe error vocabulary shared by v0.1 execution boundaries."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from anban.core.metadata import SafeMetadata, validate_safe_text


class ErrorCategory(StrEnum):
    """Coarse failure categories suitable for CLI exit and audit decisions."""

    CONFIGURATION = "configuration"
    VALIDATION = "validation"
    MODEL = "model"
    CAPABILITY = "capability"
    PERSISTENCE = "persistence"
    AUDIT_TRACE = "audit_trace"
    TIMEOUT = "timeout"
    INTERRUPTION = "interruption"


class ErrorCode(StrEnum):
    """Stable machine-readable v0.1 failure codes."""

    CONFIGURATION_MISSING = "configuration_missing"
    VALIDATION_FAILED = "validation_failed"
    INVALID_TRANSITION = "invalid_transition"
    MODEL_REQUEST_FAILED = "model_request_failed"
    MODEL_RESPONSE_INVALID = "model_response_invalid"
    CAPABILITY_UNKNOWN = "capability_unknown"
    CAPABILITY_ARGUMENTS_INVALID = "capability_arguments_invalid"
    CAPABILITY_EXECUTION_FAILED = "capability_execution_failed"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
    PERSISTENCE_WRITE_FAILED = "persistence_write_failed"
    AUDIT_TRACE_WRITE_FAILED = "audit_trace_write_failed"
    EXECUTION_TIMED_OUT = "execution_timed_out"
    EXECUTION_INTERRUPTED = "execution_interrupted"


_ERROR_CATEGORIES: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.CONFIGURATION_MISSING: ErrorCategory.CONFIGURATION,
    ErrorCode.VALIDATION_FAILED: ErrorCategory.VALIDATION,
    ErrorCode.INVALID_TRANSITION: ErrorCategory.VALIDATION,
    ErrorCode.MODEL_REQUEST_FAILED: ErrorCategory.MODEL,
    ErrorCode.MODEL_RESPONSE_INVALID: ErrorCategory.MODEL,
    ErrorCode.CAPABILITY_UNKNOWN: ErrorCategory.CAPABILITY,
    ErrorCode.CAPABILITY_ARGUMENTS_INVALID: ErrorCategory.CAPABILITY,
    ErrorCode.CAPABILITY_EXECUTION_FAILED: ErrorCategory.CAPABILITY,
    ErrorCode.PERSISTENCE_UNAVAILABLE: ErrorCategory.PERSISTENCE,
    ErrorCode.PERSISTENCE_WRITE_FAILED: ErrorCategory.PERSISTENCE,
    ErrorCode.AUDIT_TRACE_WRITE_FAILED: ErrorCategory.AUDIT_TRACE,
    ErrorCode.EXECUTION_TIMED_OUT: ErrorCategory.TIMEOUT,
    ErrorCode.EXECUTION_INTERRUPTED: ErrorCategory.INTERRUPTION,
}


class ErrorInfo(BaseModel):
    """Bounded error data that may cross CLI, Event, Audit, and Trace surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str = Field(min_length=1, max_length=512)
    details: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return validate_safe_text(value, label="error message")

    @computed_field
    @property
    def category(self) -> ErrorCategory:
        return _ERROR_CATEGORIES[self.code]


class AnbanError(Exception):
    """Exception carrying only an explicitly safe structured error."""

    def __init__(self, info: ErrorInfo) -> None:
        self.info = info
        super().__init__(info.message)

    def as_dict(self) -> dict[str, object]:
        return self.info.model_dump(mode="json")


class InvalidTransitionError(AnbanError):
    """Raised when a lifecycle attempts an unlisted transition."""

    def __init__(self, lifecycle: str, current: str, target: str) -> None:
        super().__init__(
            ErrorInfo(
                code=ErrorCode.INVALID_TRANSITION,
                message=f"invalid {lifecycle} lifecycle transition",
                details=SafeMetadata(
                    {"lifecycle": lifecycle, "current_status": current, "target_status": target}
                ),
            )
        )
