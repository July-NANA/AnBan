"""Schedule domain/SQLAlchemy mapping kept outside Core."""

from anban.core.errors import ErrorCode
from anban.core.ids import ExecutionRunId, InteractionId, ScheduleId, ScheduleOccurrenceId
from anban.core.schedule import (
    ScheduleDefinition,
    ScheduleKind,
    ScheduleMissedPolicy,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleOverlapPolicy,
)
from anban.persistence.models import ScheduleOccurrenceRecord, ScheduleRecord


def schedule_record(schedule: ScheduleDefinition) -> ScheduleRecord:
    return ScheduleRecord(
        id=schedule.id,
        name=schedule.name,
        kind=schedule.kind.value,
        timezone=schedule.timezone,
        content=schedule.content,
        cron_expression=schedule.cron_expression,
        every_seconds=schedule.every_seconds,
        missed_policy=schedule.missed_policy.value,
        overlap_policy=schedule.overlap_policy.value,
        anchor_at=schedule.anchor_at,
        next_occurrence_at=schedule.next_occurrence_at,
        created_at=schedule.created_at,
    )


def schedule_domain(record: ScheduleRecord) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=ScheduleId(record.id),
        name=record.name,
        kind=ScheduleKind(record.kind),
        timezone=record.timezone,
        content=record.content,
        cron_expression=record.cron_expression,
        every_seconds=record.every_seconds,
        missed_policy=ScheduleMissedPolicy(record.missed_policy),
        overlap_policy=ScheduleOverlapPolicy(record.overlap_policy),
        anchor_at=record.anchor_at,
        next_occurrence_at=record.next_occurrence_at,
        created_at=record.created_at,
    )


def schedule_occurrence_record(occurrence: ScheduleOccurrence) -> ScheduleOccurrenceRecord:
    return ScheduleOccurrenceRecord(
        id=occurrence.id,
        schedule_id=occurrence.schedule_id,
        interaction_id=occurrence.interaction_id,
        scheduled_for=occurrence.scheduled_for,
        status=occurrence.status.value,
        missed_count=occurrence.missed_count,
        attempt_count=occurrence.attempt_count,
        claimed_at=occurrence.claimed_at,
        lease_until=occurrence.lease_until,
        finished_at=occurrence.finished_at,
        run_id=occurrence.run_id,
        error_code=None if occurrence.error_code is None else occurrence.error_code.value,
    )


def schedule_occurrence_domain(record: ScheduleOccurrenceRecord) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        id=ScheduleOccurrenceId(record.id),
        schedule_id=ScheduleId(record.schedule_id),
        interaction_id=InteractionId(record.interaction_id),
        scheduled_for=record.scheduled_for,
        status=ScheduleOccurrenceStatus(record.status),
        missed_count=record.missed_count,
        attempt_count=record.attempt_count,
        claimed_at=record.claimed_at,
        lease_until=record.lease_until,
        finished_at=record.finished_at,
        run_id=None if record.run_id is None else ExecutionRunId(record.run_id),
        error_code=None if record.error_code is None else ErrorCode(record.error_code),
    )
