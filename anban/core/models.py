"""Authoritative v0.1 execution domain contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from anban.core.errors import ErrorCode
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    CheckpointId,
    EventId,
    ExecutionRunId,
    GraphRevisionId,
    NodeRunId,
    TaskId,
)
from anban.core.metadata import SafeMetadata

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_datetime(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(utc_datetime)]


def now_utc() -> datetime:
    """Return the current authoritative timestamp."""

    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ExecutionRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class NodeRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CapabilityInvocationStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CheckpointStatus(StrEnum):
    WAITING = "waiting"
    RESUMED = "resumed"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class DomainModel(BaseModel):
    """Strict immutable base for domain values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Task(DomainModel):
    id: TaskId
    request: str = Field(min_length=1, max_length=32_768)
    status: TaskStatus = TaskStatus.CREATED
    error_code: ErrorCode | None = None
    created_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class ExecutionRun(DomainModel):
    id: ExecutionRunId
    task_id: TaskId
    status: ExecutionRunStatus = ExecutionRunStatus.CREATED
    graph_revision_id: GraphRevisionId | None = None
    created_at: UtcDateTime = Field(default_factory=now_utc)
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    final_text: str | None = Field(default=None, max_length=32_768)
    error_code: ErrorCode | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class NodeRun(DomainModel):
    id: NodeRunId
    run_id: ExecutionRunId
    node_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    status: NodeRunStatus = NodeRunStatus.CREATED
    created_at: UtcDateTime = Field(default_factory=now_utc)
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class CapabilityInvocation(DomainModel):
    id: CapabilityInvocationId
    run_id: ExecutionRunId
    node_run_id: NodeRunId
    capability_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    status: CapabilityInvocationStatus = CapabilityInvocationStatus.REQUESTED
    requested_at: UtcDateTime = Field(default_factory=now_utc)
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class Checkpoint(DomainModel):
    """Durable coordination fact for one paused Capability continuation."""

    id: CheckpointId
    run_id: ExecutionRunId
    node_run_id: NodeRunId
    invocation_id: CapabilityInvocationId
    status: CheckpointStatus = CheckpointStatus.WAITING
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: UtcDateTime = Field(default_factory=now_utc)
    resumed_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @model_validator(mode="after")
    def validate_checkpoint_state(self) -> Self:
        terminal = self.status in {
            CheckpointStatus.COMPLETED,
            CheckpointStatus.FAILED,
            CheckpointStatus.CANCELLED,
            CheckpointStatus.TIMED_OUT,
        }
        if (self.resumed_at is not None) != (self.status is not CheckpointStatus.WAITING):
            raise ValueError("Checkpoint resume timestamp disagrees with status")
        if terminal != (self.finished_at is not None):
            raise ValueError("Checkpoint terminal timestamp disagrees with status")
        if (self.error_code is not None) != (
            self.status
            in {
                CheckpointStatus.FAILED,
                CheckpointStatus.CANCELLED,
                CheckpointStatus.TIMED_OUT,
            }
        ):
            raise ValueError("Checkpoint error code disagrees with status")
        return self


class Artifact(DomainModel):
    id: ArtifactId
    run_id: ExecutionRunId
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    uri: str = Field(min_length=1, max_length=512)
    sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)
    created_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if not self.uri.startswith("anban://artifact/"):
            raise ValueError("artifact uri must use the anban://artifact/ scheme")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be lowercase hexadecimal")
        return self


class Event(DomainModel):
    id: EventId
    run_id: ExecutionRunId
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]*$")
    occurred_at: UtcDateTime = Field(default_factory=now_utc)
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    artifact_id: ArtifactId | None = None
    checkpoint_id: CheckpointId | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)
