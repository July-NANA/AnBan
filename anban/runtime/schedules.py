"""Deterministic Cron/Interval calculation and durable schedule composition."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from anban.core import (
    AnbanError,
    ErrorCode,
    ErrorInfo,
    SafeMetadata,
    ScheduleDefinition,
    ScheduleId,
    ScheduleKind,
    UnitOfWorkFactory,
    new_schedule_id,
    now_utc,
)
from anban.core.models import UtcDateTime

_CRON_MAX_CHARS = 256
_CRON_FIELDS = 5
_CRON_MAX_YEARS_BETWEEN_MATCHES = 10


def schedule_error(reason: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.VALIDATION_FAILED,
            message="Schedule definition was rejected",
            details=SafeMetadata({"reason": reason}),
        )
    )


def next_cron_occurrence(
    expression: str,
    timezone_name: str,
    after: datetime,
) -> UtcDateTime:
    normalized = " ".join(expression.split())
    if len(normalized) > _CRON_MAX_CHARS or len(normalized.split()) != _CRON_FIELDS:
        raise schedule_error("cron_expression_invalid")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise schedule_error("timezone_unavailable") from None
    if not croniter.is_valid(normalized, strict=True):
        raise schedule_error("cron_expression_invalid")
    localized = after.astimezone(timezone)
    try:
        candidate = croniter(
            normalized,
            localized,
            day_or=True,
            max_years_between_matches=_CRON_MAX_YEARS_BETWEEN_MATCHES,
        ).get_next(datetime)
    except (CroniterError, OverflowError, ValueError):
        raise schedule_error("cron_occurrence_unavailable") from None
    if candidate.tzinfo is None:
        raise schedule_error("cron_occurrence_invalid")
    result = candidate.astimezone(UTC)
    if result <= after.astimezone(UTC):
        raise schedule_error("cron_occurrence_invalid")
    return result


def next_interval_occurrence(every_seconds: int, after: datetime) -> UtcDateTime:
    if not 1 <= every_seconds <= 31_536_000:
        raise schedule_error("interval_invalid")
    return after.astimezone(UTC) + timedelta(seconds=every_seconds)


class ScheduleService:
    """Create and query immutable definitions without dispatching business work."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        clock: Callable[[], UtcDateTime] = now_utc,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def create_cron(
        self,
        *,
        name: str,
        expression: str,
        timezone: str,
        content: str,
    ) -> ScheduleDefinition:
        created_at = self._clock()
        schedule = ScheduleDefinition(
            id=new_schedule_id(),
            name=name,
            kind=ScheduleKind.CRON,
            timezone=timezone,
            content=content,
            cron_expression=expression,
            anchor_at=created_at,
            next_occurrence_at=next_cron_occurrence(expression, timezone, created_at),
            created_at=created_at,
        )
        await self._add(schedule)
        return schedule

    async def create_interval(
        self,
        *,
        name: str,
        every_seconds: int,
        timezone: str,
        content: str,
    ) -> ScheduleDefinition:
        created_at = self._clock()
        schedule = ScheduleDefinition(
            id=new_schedule_id(),
            name=name,
            kind=ScheduleKind.INTERVAL,
            timezone=timezone,
            content=content,
            every_seconds=every_seconds,
            anchor_at=created_at,
            next_occurrence_at=next_interval_occurrence(every_seconds, created_at),
            created_at=created_at,
        )
        await self._add(schedule)
        return schedule

    async def get(self, schedule_id: ScheduleId) -> ScheduleDefinition:
        async with self._unit_of_work() as unit_of_work:
            schedule = await unit_of_work.executions.get_schedule(schedule_id)
        if schedule is None:
            raise schedule_error("schedule_unknown")
        return schedule

    async def list(self, limit: int = 20) -> tuple[ScheduleDefinition, ...]:
        if not 1 <= limit <= 100:
            raise schedule_error("schedule_limit_invalid")
        async with self._unit_of_work() as unit_of_work:
            return await unit_of_work.executions.list_schedules(limit)

    async def _add(self, schedule: ScheduleDefinition) -> None:
        async with self._unit_of_work() as unit_of_work:
            await unit_of_work.executions.add_schedule(schedule)
            await unit_of_work.commit()
