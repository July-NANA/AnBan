"""Authoritative durable Interaction inbox lifecycle facts."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from anban.core.errors import ErrorCode
from anban.core.ids import ExecutionRunId, InteractionId, NodeRunId, TaskId
from anban.core.models import DomainModel, UtcDateTime


class InteractionInboxStatus(StrEnum):
    """Closed lifecycle for one normalized external delivery."""

    PROCESSING = "processing"
    ROUTED = "routed"
    PROCESSED = "processed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class InteractionInboxDisposition(StrEnum):
    """Last protocol-level decision for an inbox delivery attempt."""

    ACCEPTED = "accepted"
    DEDUPLICATED = "deduplicated"
    CONFLICTING = "conflicting"
    EXPIRED = "expired"
    REJECTED = "rejected"


class InteractionInboxEntry(DomainModel):
    """Durable normalized input plus safe correlation and routing outcome."""

    interaction_id: InteractionId
    source: str = Field(min_length=1, max_length=64)
    input_kind: str = Field(min_length=1, max_length=64)
    route: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=32_768)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resume_namespace: str | None = Field(default=None, max_length=64)
    resume_correlation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    deduplication_namespace: str | None = Field(default=None, max_length=64)
    deduplication_correlation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    received_at: UtcDateTime
    expires_at: UtcDateTime | None = None
    status: InteractionInboxStatus
    claimed_at: UtcDateTime
    task_id: TaskId | None = None
    run_id: ExecutionRunId | None = None
    node_run_id: NodeRunId | None = None
    outcome_status: str | None = Field(default=None, max_length=32)
    error_code: ErrorCode | None = None
    failure_reason: str | None = Field(default=None, max_length=64)
    finished_at: UtcDateTime | None = None
    delivery_count: int = Field(default=1, ge=1)
    last_received_at: UtcDateTime
    last_disposition: InteractionInboxDisposition

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if hashlib.sha256(self.content.encode()).hexdigest() != self.content_hash:
            raise ValueError("Inbox content hash does not match content")
        if (self.resume_namespace is None) != (self.resume_correlation_hash is None):
            raise ValueError("Inbox resume correlation is incomplete")
        if (self.deduplication_namespace is None) != (self.deduplication_correlation_hash is None):
            raise ValueError("Inbox deduplication correlation is incomplete")
        identities = (self.task_id, self.run_id, self.node_run_id)
        if any(identity is not None for identity in identities) and not all(
            identity is not None for identity in identities
        ):
            raise ValueError("Inbox route identities must be complete")
        if (
            self.status
            in {
                InteractionInboxStatus.ROUTED,
                InteractionInboxStatus.PROCESSED,
            }
            and self.run_id is None
        ):
            raise ValueError("Routed inbox entry requires Run identities")
        terminal = self.status in {
            InteractionInboxStatus.PROCESSED,
            InteractionInboxStatus.REJECTED,
            InteractionInboxStatus.EXPIRED,
        }
        if terminal != (self.finished_at is not None):
            raise ValueError("Inbox terminal timestamp disagrees with status")
        if self.status is InteractionInboxStatus.PROCESSED and self.outcome_status is None:
            raise ValueError("Processed inbox entry requires an outcome")
        if (
            self.status
            in {
                InteractionInboxStatus.REJECTED,
                InteractionInboxStatus.EXPIRED,
            }
            and self.failure_reason is None
        ):
            raise ValueError("Rejected inbox entry requires a failure reason")
        if self.last_received_at < self.received_at:
            raise ValueError("Inbox last receipt cannot precede its first receipt")
        if self.expires_at is not None and self.expires_at <= self.received_at:
            raise ValueError("Inbox expiry must follow authoritative receipt")
        return self
