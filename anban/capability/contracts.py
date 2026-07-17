"""Provider-independent contracts for governed executable Capabilities."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from anban.capability.schema import SchemaDefinitionError, validate_input_schema
from anban.core.errors import ErrorInfo
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ExecutionRunId,
    NodeRunId,
)
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import UtcDateTime


class CapabilityValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityKind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"


class CapabilityResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CapabilityDescriptor(CapabilityValue):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=1, max_length=1024)
    input_schema: dict[str, JsonValue]
    kind: CapabilityKind = CapabilityKind.TOOL
    available: bool = True

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return validate_safe_text(value, label="Capability description", max_length=1024)

    @model_validator(mode="after")
    def validate_schema(self) -> Self:
        try:
            validate_input_schema(self.input_schema)
        except SchemaDefinitionError as exc:
            raise ValueError("Capability input schema is invalid") from exc
        return self


class InvocationContext(CapabilityValue):
    """Authoritative identity and bounds supplied only by Runtime."""

    run_id: ExecutionRunId
    node_run_id: NodeRunId
    invocation_id: CapabilityInvocationId
    deadline_at: UtcDateTime
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)


class ArtifactReference(CapabilityValue):
    id: ArtifactId
    uri: str = Field(min_length=1, max_length=512, pattern=r"^anban://artifact/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)


class CapabilityResult(CapabilityValue):
    status: CapabilityResultStatus
    observation: str | None = Field(default=None, max_length=1_048_576)
    artifacts: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)
    error: ErrorInfo | None = None
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is CapabilityResultStatus.COMPLETED:
            if self.observation is None or self.error is not None:
                raise ValueError("completed Capability requires an observation and no error")
        elif self.error is None:
            raise ValueError("non-completed Capability requires an error")
        return self


class CapabilityHandler(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult: ...

    async def cancel(self, context: InvocationContext) -> None: ...


class CapabilityPort(Protocol):
    def search(self, query: str | None = None) -> tuple[CapabilityDescriptor, ...]: ...

    def describe(self, name: str) -> CapabilityDescriptor: ...

    async def invoke(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult: ...

    async def cancel(self, context: InvocationContext) -> None: ...
