"""Mapping between durable Interaction inbox values and PostgreSQL rows."""

from __future__ import annotations

from anban.core.errors import ErrorCode
from anban.core.ids import ExecutionRunId, InteractionId, NodeRunId, TaskId
from anban.core.inbox import (
    InteractionInboxDisposition,
    InteractionInboxEntry,
    InteractionInboxStatus,
)
from anban.persistence.models import InteractionInboxRecord


def inbox_record(entry: InteractionInboxEntry) -> InteractionInboxRecord:
    return InteractionInboxRecord(
        interaction_id=entry.interaction_id,
        source=entry.source,
        input_kind=entry.input_kind,
        route=entry.route,
        content=entry.content,
        content_hash=entry.content_hash,
        semantic_hash=entry.semantic_hash,
        resume_namespace=entry.resume_namespace,
        resume_correlation_hash=entry.resume_correlation_hash,
        deduplication_namespace=entry.deduplication_namespace,
        deduplication_correlation_hash=entry.deduplication_correlation_hash,
        received_at=entry.received_at,
        expires_at=entry.expires_at,
        status=entry.status.value,
        claimed_at=entry.claimed_at,
        task_id=entry.task_id,
        run_id=entry.run_id,
        node_run_id=entry.node_run_id,
        outcome_status=entry.outcome_status,
        error_code=None if entry.error_code is None else entry.error_code.value,
        failure_reason=entry.failure_reason,
        finished_at=entry.finished_at,
        delivery_count=entry.delivery_count,
        last_received_at=entry.last_received_at,
        last_disposition=entry.last_disposition.value,
    )


def inbox_domain(record: InteractionInboxRecord) -> InteractionInboxEntry:
    return InteractionInboxEntry(
        interaction_id=InteractionId(record.interaction_id),
        source=record.source,
        input_kind=record.input_kind,
        route=record.route,
        content=record.content,
        content_hash=record.content_hash,
        semantic_hash=record.semantic_hash,
        resume_namespace=record.resume_namespace,
        resume_correlation_hash=record.resume_correlation_hash,
        deduplication_namespace=record.deduplication_namespace,
        deduplication_correlation_hash=record.deduplication_correlation_hash,
        received_at=record.received_at,
        expires_at=record.expires_at,
        status=InteractionInboxStatus(record.status),
        claimed_at=record.claimed_at,
        task_id=None if record.task_id is None else TaskId(record.task_id),
        run_id=None if record.run_id is None else ExecutionRunId(record.run_id),
        node_run_id=None if record.node_run_id is None else NodeRunId(record.node_run_id),
        outcome_status=record.outcome_status,
        error_code=None if record.error_code is None else ErrorCode(record.error_code),
        failure_reason=record.failure_reason,
        finished_at=record.finished_at,
        delivery_count=record.delivery_count,
        last_received_at=record.last_received_at,
        last_disposition=InteractionInboxDisposition(record.last_disposition),
    )


def replace_inbox_record(record: InteractionInboxRecord, entry: InteractionInboxEntry) -> None:
    replacement = inbox_record(entry)
    for attribute in (
        "status",
        "claimed_at",
        "task_id",
        "run_id",
        "node_run_id",
        "outcome_status",
        "error_code",
        "failure_reason",
        "finished_at",
        "delivery_count",
        "last_received_at",
        "last_disposition",
    ):
        setattr(record, attribute, getattr(replacement, attribute))
