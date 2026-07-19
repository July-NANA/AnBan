"""Schedule domain/SQLAlchemy mapping kept outside Core."""

from anban.core.ids import ScheduleId
from anban.core.schedule import ScheduleDefinition, ScheduleKind
from anban.persistence.models import ScheduleRecord


def schedule_record(schedule: ScheduleDefinition) -> ScheduleRecord:
    return ScheduleRecord(
        id=schedule.id,
        name=schedule.name,
        kind=schedule.kind.value,
        timezone=schedule.timezone,
        content=schedule.content,
        cron_expression=schedule.cron_expression,
        every_seconds=schedule.every_seconds,
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
        anchor_at=record.anchor_at,
        next_occurrence_at=record.next_occurrence_at,
        created_at=record.created_at,
    )
