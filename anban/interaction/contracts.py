"""Transport-neutral Interaction input and external correlation contracts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from anban.core.ids import InteractionId, new_interaction_id
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import UtcDateTime, now_utc

_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SYSTEM_IDENTITY_FIELDS = frozenset(
    {
        "artifact_id",
        "capability_invocation_id",
        "checkpoint_id",
        "event_id",
        "execution_run_id",
        "graph_revision_id",
        "interaction_id",
        "invocation_id",
        "node_run_id",
        "run_id",
        "session_id",
        "task_id",
    }
)
_SYSTEM_ENVELOPE_FIELDS = _SYSTEM_IDENTITY_FIELDS | {"id", "received_at", "source"}
_ADAPTER_ATTESTATION_FIELDS = frozenset(
    {
        "webhook_auth_version",
        "webhook_authenticated",
        "webhook_clock_skew_seconds",
        "webhook_endpoint",
        "webhook_event_hash",
    }
)


class InteractionValue(BaseModel):
    """Strict immutable base for Interaction-owned values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InteractionInputKind(StrEnum):
    """Closed semantic input kinds shared by every future Interaction Adapter."""

    USER_MESSAGE = "user_message"
    SUPPLEMENTAL_INPUT = "supplemental_input"
    HUMAN_INPUT = "human_input"
    ASYNC_CAPABILITY_RESULT = "async_capability_result"
    MCP_RESULT = "mcp_result"
    SUBAGENT_RESULT = "subagent_result"
    WEBHOOK_EVENT = "webhook_event"
    SCHEDULE_OCCURRENCE = "schedule_occurrence"


class InteractionRoute(StrEnum):
    """Requested routing meaning; it is not proof that a resumable Run exists."""

    NEW_TASK = "new_task"
    RESUME_ELIGIBLE_RUN = "resume_eligible_run"


class CorrelationPurpose(StrEnum):
    """The only meanings an external correlation key may carry."""

    RESUME = "resume"
    DEDUPLICATION = "deduplication"


class CorrelationFailureReason(StrEnum):
    """Fail-closed outcomes required of a later durable correlation resolver."""

    MALFORMED = "malformed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    CONFLICTING = "conflicting"
    INELIGIBLE = "ineligible"


class CorrelationKey(InteractionValue):
    """Bounded external identity that never aliases a system-owned domain identity."""

    purpose: CorrelationPurpose
    namespace: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=256)
    expires_at: UtcDateTime | None = None

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        if not _NAMESPACE_PATTERN.fullmatch(value):
            raise ValueError("Correlation namespace must be a bounded logical name")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return validate_safe_text(value, label="Correlation value", max_length=256)

    @property
    def fingerprint(self) -> str:
        """Return a stable safe projection without exposing the external value."""

        material = f"{self.purpose.value}\x00{self.namespace}\x00{self.value}".encode()
        return hashlib.sha256(material).hexdigest()

    def is_expired_at(self, received_at: UtcDateTime) -> bool:
        return self.expires_at is not None and self.expires_at <= received_at


class InteractionCorrelation(InteractionValue):
    """New-work versus resume intent plus a separate optional idempotency identity."""

    route: InteractionRoute = InteractionRoute.NEW_TASK
    resume_key: CorrelationKey | None = None
    deduplication_key: CorrelationKey | None = None

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        if self.route is InteractionRoute.NEW_TASK and self.resume_key is not None:
            raise ValueError("New Task input cannot carry a resume correlation")
        if self.route is InteractionRoute.RESUME_ELIGIBLE_RUN and self.resume_key is None:
            raise ValueError("Run resumption requires an external resume correlation")
        if self.resume_key is not None:
            _require_purpose(self.resume_key, CorrelationPurpose.RESUME, "resume")
        if self.deduplication_key is not None:
            _require_purpose(
                self.deduplication_key,
                CorrelationPurpose.DEDUPLICATION,
                "deduplication",
            )
        if (
            self.resume_key is not None
            and self.deduplication_key is not None
            and self.resume_key.namespace == self.deduplication_key.namespace
            and self.resume_key.value == self.deduplication_key.value
        ):
            raise ValueError("Resume and deduplication correlations must be distinct")
        return self

    @property
    def keys(self) -> tuple[CorrelationKey, ...]:
        return tuple(key for key in (self.resume_key, self.deduplication_key) if key is not None)


class InteractionEnvelope(InteractionValue):
    """A system-normalized input before new-Task creation or eligible-Run lookup."""

    id: InteractionId
    source: str = Field(default="cli", min_length=1, max_length=64)
    input_kind: InteractionInputKind = InteractionInputKind.USER_MESSAGE
    content: str = Field(min_length=1, max_length=32_768)
    received_at: UtcDateTime = Field(default_factory=now_utc)
    correlation: InteractionCorrelation = Field(default_factory=InteractionCorrelation)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if not _SOURCE_PATTERN.fullmatch(value):
            raise ValueError("Interaction source must be a bounded logical name")
        return value

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        forged = _SYSTEM_IDENTITY_FIELDS.intersection(self.metadata.root)
        if forged:
            raise ValueError("Interaction metadata cannot supply system-owned identities")
        if any(key.is_expired_at(self.received_at) for key in self.correlation.keys):
            raise ValueError(f"Interaction correlation is {CorrelationFailureReason.EXPIRED.value}")
        return self

    @classmethod
    def from_external(
        cls,
        payload: Mapping[str, object],
        *,
        source: str,
    ) -> Self:
        """Normalize an untrusted payload while assigning all system-owned fields."""

        forged = _SYSTEM_ENVELOPE_FIELDS.intersection(payload)
        if forged:
            raise ValueError("External input cannot supply system-owned envelope fields")
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            supplied_metadata = cast(Mapping[object, object], metadata)
            if any(
                isinstance(key, str) and key in _ADAPTER_ATTESTATION_FIELDS
                for key in supplied_metadata
            ):
                raise ValueError("External input cannot supply Adapter attestations")
        return cls.model_validate(
            {
                **dict(payload),
                "id": new_interaction_id(),
                "source": source,
                "received_at": now_utc(),
            }
        )


def _require_purpose(
    key: CorrelationKey,
    expected: CorrelationPurpose,
    label: str,
) -> None:
    if key.purpose is not expected:
        raise ValueError(f"{label.capitalize()} key purpose must be {expected.value}")
