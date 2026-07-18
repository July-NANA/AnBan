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
from anban.core.models import UtcDateTime, now_utc


class CapabilityValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityKind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"


class InventoryKind(StrEnum):
    """All paths evaluated by Main Agent sufficiency without redefining Model as a Capability."""

    MODEL = "model"
    CAPABILITY = "capability"
    SKILL = "skill"
    PROCESS = "process"
    MCP = "mcp"
    MEMORY = "memory"
    SUB_AGENT = "sub_agent"


class AvailabilityStatus(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    DEGRADED = "degraded"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CostLevel(StrEnum):
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SideEffectLevel(StrEnum):
    NONE = "none"
    REVERSIBLE = "reversible"
    EXTERNAL = "external"
    IRREVERSIBLE = "irreversible"


class InventoryBoundary(CapabilityValue):
    risk: RiskLevel
    cost: CostLevel
    side_effects: SideEffectLevel
    summary: str = Field(min_length=1, max_length=1024)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return validate_safe_text(value, label="Inventory boundary summary", max_length=1024)


class CapabilityInventoryItem(CapabilityValue):
    """A bounded description used for selection, never for direct execution."""

    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z@][a-z0-9_.@:/-]*$")
    kind: InventoryKind
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    availability: AvailabilityStatus
    unavailable_reason: str | None = Field(default=None, max_length=512)
    input_schema: dict[str, JsonValue] | None = None
    dependencies: tuple[str, ...] = Field(default=(), max_length=32)
    constraints: tuple[str, ...] = Field(default=(), max_length=32)
    boundary: InventoryBoundary
    version_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("name", "description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_safe_text(value, label="Inventory description", max_length=1024)

    @field_validator("unavailable_reason")
    @classmethod
    def validate_unavailable_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, label="Unavailable reason", max_length=512)

    @field_validator("dependencies", "constraints")
    @classmethod
    def validate_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            validate_safe_text(value, label="Inventory constraint", max_length=256)
            for value in values
        )

    @model_validator(mode="after")
    def validate_inventory_shape(self) -> Self:
        ready = self.availability is AvailabilityStatus.READY
        if ready == (self.unavailable_reason is not None):
            raise ValueError("inventory availability and unavailable reason disagree")
        if self.input_schema is not None:
            try:
                validate_input_schema(self.input_schema)
            except SchemaDefinitionError as exc:
                raise ValueError("Inventory input schema is invalid") from exc
        return self


class CapabilityInventoryQuery(CapabilityValue):
    text: str | None = Field(default=None, max_length=128)
    kinds: tuple[InventoryKind, ...] = Field(default=(), max_length=len(InventoryKind))
    include_unavailable: bool = True
    limit: int = Field(default=32, ge=1, le=128)

    @field_validator("text")
    @classmethod
    def validate_query_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Inventory query text cannot be blank")
        return validate_safe_text(normalized, label="Inventory query", max_length=128)

    @field_validator("kinds")
    @classmethod
    def validate_unique_kinds(cls, values: tuple[InventoryKind, ...]) -> tuple[InventoryKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Inventory query kinds must be unique")
        return values


class CapabilityInventorySnapshot(CapabilityValue):
    items: tuple[CapabilityInventoryItem, ...] = Field(max_length=512)
    generated_at: UtcDateTime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        keys = [item.key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("Inventory item keys must be unique")
        return self


class CapabilityInventoryPort(Protocol):
    """Read-only inventory contract; invocation remains on CapabilityPort and ModelPort."""

    def snapshot(self) -> CapabilityInventorySnapshot: ...

    def search(self, query: CapabilityInventoryQuery) -> tuple[CapabilityInventoryItem, ...]: ...

    def describe(self, key: str) -> CapabilityInventoryItem: ...


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
    inventory_kind: InventoryKind = InventoryKind.CAPABILITY
    available: bool = True

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return validate_safe_text(value, label="Capability description", max_length=1024)

    @model_validator(mode="after")
    def validate_schema(self) -> Self:
        if self.inventory_kind in {InventoryKind.MODEL, InventoryKind.SKILL}:
            raise ValueError("Capability descriptor has an invalid inventory kind")
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
