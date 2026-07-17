"""Authoritative bounded Task and Session context vocabulary."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from anban.core.ids import (
    ArtifactId,
    ContextEntryId,
    ContextSummaryId,
    SessionId,
    TaskId,
)
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import UtcDateTime, now_utc


class ContextValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextScope(StrEnum):
    TASK = "task"
    SESSION = "session"


class ContextEntryKind(StrEnum):
    USER_GOAL = "user_goal"
    USER_FACT = "user_fact"
    SUPPLEMENT = "supplement"
    OBSERVATION = "observation"
    ARTIFACT_REFERENCE = "artifact_reference"


class ContextSourceKind(StrEnum):
    USER = "user"
    INTERACTION = "interaction"
    RUNTIME = "runtime"
    CAPABILITY = "capability"
    ARTIFACT = "artifact"


class ContextSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class ContextConflictState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONFLICTING = "conflicting"
    EXPIRED = "expired"


class ContextSource(ContextValue):
    kind: ContextSourceKind
    reference: str = Field(min_length=1, max_length=256)
    observed_at: UtcDateTime = Field(default_factory=now_utc)

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        return validate_safe_text(value, label="Context source reference", max_length=256)


class ContextEntry(ContextValue):
    id: ContextEntryId
    scope: ContextScope
    task_id: TaskId | None = None
    session_id: SessionId | None = None
    kind: ContextEntryKind
    content: str = Field(min_length=1, max_length=8192)
    source: ContextSource
    sensitivity: ContextSensitivity = ContextSensitivity.INTERNAL
    state: ContextConflictState = ContextConflictState.ACTIVE
    artifact_id: ArtifactId | None = None
    supersedes: ContextEntryId | None = None
    conflicts_with: ContextEntryId | None = None
    created_at: UtcDateTime = Field(default_factory=now_utc)
    expires_at: UtcDateTime | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return validate_safe_text(
            value,
            label="Context content",
            max_length=8192,
            allow_absolute_paths=True,
        )

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if self.scope is ContextScope.TASK:
            if self.task_id is None or self.session_id is not None:
                raise ValueError("Task context requires only a Task identity")
        elif self.session_id is None or self.task_id is not None:
            raise ValueError("Session context requires only a Session identity")
        if self.sensitivity is ContextSensitivity.SECRET:
            raise ValueError("Secret values cannot enter Context")
        if (self.kind is ContextEntryKind.ARTIFACT_REFERENCE) != (self.artifact_id is not None):
            raise ValueError("Artifact context kind and identity must agree")
        if self.state is ContextConflictState.CONFLICTING and self.conflicts_with is None:
            raise ValueError("Conflicting context requires the conflicting entry identity")
        if self.state is ContextConflictState.EXPIRED and self.expires_at is None:
            raise ValueError("Expired context requires an expiry timestamp")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("Context expiry must follow creation")
        if self.id in {self.supersedes, self.conflicts_with}:
            raise ValueError("Context entry cannot reference itself")
        return self


class ContextSummary(ContextValue):
    id: ContextSummaryId
    scope: ContextScope
    task_id: TaskId | None = None
    session_id: SessionId | None = None
    covered_entry_ids: tuple[ContextEntryId, ...] = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=8192)
    created_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("content")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return validate_safe_text(
            value,
            label="Context summary",
            max_length=8192,
            allow_absolute_paths=True,
        )

    @model_validator(mode="after")
    def validate_summary_scope(self) -> Self:
        if len(self.covered_entry_ids) != len(set(self.covered_entry_ids)):
            raise ValueError("Summary entry identities must be unique")
        if self.scope is ContextScope.TASK:
            if self.task_id is None or self.session_id is not None:
                raise ValueError("Task summary requires only a Task identity")
        elif self.session_id is None or self.task_id is not None:
            raise ValueError("Session summary requires only a Session identity")
        return self


class ContextCompressionBoundary(ContextValue):
    max_active_entries: int = Field(default=128, ge=1, le=512)
    max_active_chars: int = Field(default=16_384, ge=1024, le=131_072)
    max_summary_chars: int = Field(default=8192, ge=256, le=32_768)
    preserve_authoritative_facts: bool = True


class TaskContext(ContextValue):
    task_id: TaskId
    entries: tuple[ContextEntry, ...] = Field(default=(), max_length=512)
    summaries: tuple[ContextSummary, ...] = Field(default=(), max_length=64)
    boundary: ContextCompressionBoundary = Field(default_factory=ContextCompressionBoundary)

    @model_validator(mode="after")
    def validate_task_context(self) -> Self:
        _validate_collection(
            ContextScope.TASK,
            self.task_id,
            self.entries,
            self.summaries,
            self.boundary,
        )
        return self


class SessionContext(ContextValue):
    session_id: SessionId
    entries: tuple[ContextEntry, ...] = Field(default=(), max_length=512)
    summaries: tuple[ContextSummary, ...] = Field(default=(), max_length=64)
    boundary: ContextCompressionBoundary = Field(default_factory=ContextCompressionBoundary)

    @model_validator(mode="after")
    def validate_session_context(self) -> Self:
        _validate_collection(
            ContextScope.SESSION,
            self.session_id,
            self.entries,
            self.summaries,
            self.boundary,
        )
        return self


def _validate_collection(
    scope: ContextScope,
    identity: TaskId | SessionId,
    entries: tuple[ContextEntry, ...],
    summaries: tuple[ContextSummary, ...],
    boundary: ContextCompressionBoundary,
) -> None:
    entry_ids = [entry.id for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("Active Context entries must be unique")
    if len(entries) > boundary.max_active_entries:
        raise ValueError("Active Context entry limit exceeded")
    if sum(len(entry.content) for entry in entries) > boundary.max_active_chars:
        raise ValueError("Active Context character limit exceeded")
    for entry in entries:
        entry_identity = entry.task_id if scope is ContextScope.TASK else entry.session_id
        if entry.scope is not scope or entry_identity != identity:
            raise ValueError("Context entry belongs to another scope")
    for summary in summaries:
        summary_identity = summary.task_id if scope is ContextScope.TASK else summary.session_id
        if summary.scope is not scope or summary_identity != identity:
            raise ValueError("Context summary belongs to another scope")
        if len(summary.content) > boundary.max_summary_chars:
            raise ValueError("Context summary character limit exceeded")
