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
    ExecutionRunId,
    SafeMetadata,
    ScheduleDefinition,
    ScheduleId,
    ScheduleKind,
    ScheduleMissedPolicy,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleOverlapPolicy,
    UnitOfWorkFactory,
    new_schedule_id,
    new_schedule_occurrence_id,
    now_utc,
)
from anban.core.ids import new_interaction_id
from anban.core.models import UtcDateTime

_CRON_MAX_CHARS = 256
_CRON_FIELDS = 5
_CRON_MAX_YEARS_BETWEEN_MATCHES = 10
_OCCURRENCE_LEASE_SECONDS = 300
_MISSED_OCCURRENCES_MAX = 10_000


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


def next_schedule_occurrence(schedule: ScheduleDefinition, after: datetime) -> UtcDateTime:
    if schedule.kind is ScheduleKind.CRON:
        if schedule.cron_expression is None:
            raise schedule_error("cron_expression_invalid")
        return next_cron_occurrence(schedule.cron_expression, schedule.timezone, after)
    if schedule.every_seconds is None:
        raise schedule_error("interval_invalid")
    return next_interval_occurrence(schedule.every_seconds, after)


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
        missed_policy: ScheduleMissedPolicy = ScheduleMissedPolicy.SKIP,
    ) -> ScheduleDefinition:
        created_at = self._clock()
        schedule = ScheduleDefinition(
            id=new_schedule_id(),
            name=name,
            kind=ScheduleKind.CRON,
            timezone=timezone,
            content=content,
            cron_expression=expression,
            missed_policy=missed_policy,
            overlap_policy=ScheduleOverlapPolicy.SKIP,
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
        missed_policy: ScheduleMissedPolicy = ScheduleMissedPolicy.SKIP,
    ) -> ScheduleDefinition:
        created_at = self._clock()
        schedule = ScheduleDefinition(
            id=new_schedule_id(),
            name=name,
            kind=ScheduleKind.INTERVAL,
            timezone=timezone,
            content=content,
            every_seconds=every_seconds,
            missed_policy=missed_policy,
            overlap_policy=ScheduleOverlapPolicy.SKIP,
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

    async def list_occurrences(
        self, schedule_id: ScheduleId, limit: int = 20
    ) -> tuple[ScheduleOccurrence, ...]:
        if not 1 <= limit <= 100:
            raise schedule_error("schedule_limit_invalid")
        async with self._unit_of_work() as unit_of_work:
            schedule = await unit_of_work.executions.get_schedule(schedule_id)
            if schedule is None:
                raise schedule_error("schedule_unknown")
            return await unit_of_work.executions.list_schedule_occurrences(schedule_id, limit)

    async def claim_due(
        self, schedule: ScheduleDefinition, now: UtcDateTime
    ) -> tuple[ScheduleOccurrence | None, bool]:
        latest_items = await self.list_occurrences(schedule.id, 100)
        latest = None if not latest_items else latest_items[0]
        active = next(
            (item for item in latest_items if item.status is ScheduleOccurrenceStatus.CLAIMED),
            None,
        )
        if active is not None and active.lease_until <= now:
            if active.attempt_count >= 100:
                failed = await self.fail_occurrence(
                    active,
                    error_code=ErrorCode.EXECUTION_INTERRUPTED,
                    finished_at=now,
                )
                return failed, False
            candidate = active.model_copy(
                update={
                    "claimed_at": now,
                    "lease_until": now + timedelta(seconds=_OCCURRENCE_LEASE_SECONDS),
                }
            )
            return await self._claim(candidate)
        cursor = (
            schedule.next_occurrence_at
            if latest is None
            else next_schedule_occurrence(schedule, latest.scheduled_for)
        )
        due: list[UtcDateTime] = []
        while cursor <= now:
            due.append(cursor)
            if len(due) > _MISSED_OCCURRENCES_MAX:
                raise schedule_error("missed_occurrence_limit_exceeded")
            cursor = next_schedule_occurrence(schedule, cursor)
        if not due:
            return None, False
        if len(due) > 1 and schedule.missed_policy is ScheduleMissedPolicy.SKIP:
            skipped = self._occurrence(
                schedule,
                scheduled_for=due[-1],
                now=now,
                missed_count=len(due),
                status=ScheduleOccurrenceStatus.SKIPPED,
            )
            async with self._unit_of_work() as unit_of_work:
                stored, _ = await unit_of_work.executions.add_schedule_occurrence(skipped)
                await unit_of_work.commit()
            return stored, False
        candidate = self._occurrence(
            schedule,
            scheduled_for=due[-1],
            now=now,
            missed_count=max(0, len(due) - 1),
            status=ScheduleOccurrenceStatus.CLAIMED,
        )
        return await self._claim(candidate)

    async def complete_occurrence(
        self,
        occurrence: ScheduleOccurrence,
        *,
        run_id: ExecutionRunId,
        error_code: ErrorCode | None,
        finished_at: UtcDateTime,
    ) -> ScheduleOccurrence:
        completed = occurrence.model_copy(
            update={
                "status": ScheduleOccurrenceStatus.PROCESSED,
                "finished_at": finished_at,
                "run_id": run_id,
                "error_code": error_code,
            }
        )
        async with self._unit_of_work() as unit_of_work:
            await unit_of_work.executions.update_schedule_occurrence(completed)
            await unit_of_work.commit()
        return completed

    async def fail_occurrence(
        self,
        occurrence: ScheduleOccurrence,
        *,
        error_code: ErrorCode,
        finished_at: UtcDateTime,
    ) -> ScheduleOccurrence:
        failed = occurrence.model_copy(
            update={
                "status": ScheduleOccurrenceStatus.FAILED,
                "finished_at": finished_at,
                "error_code": error_code,
            }
        )
        async with self._unit_of_work() as unit_of_work:
            await unit_of_work.executions.update_schedule_occurrence(failed)
            await unit_of_work.commit()
        return failed

    async def _add(self, schedule: ScheduleDefinition) -> None:
        async with self._unit_of_work() as unit_of_work:
            await unit_of_work.executions.add_schedule(schedule)
            await unit_of_work.commit()

    async def _claim(self, occurrence: ScheduleOccurrence) -> tuple[ScheduleOccurrence, bool]:
        async with self._unit_of_work() as unit_of_work:
            stored, claimed = await unit_of_work.executions.claim_schedule_occurrence(occurrence)
            await unit_of_work.commit()
        return stored, claimed

    @staticmethod
    def _occurrence(
        schedule: ScheduleDefinition,
        *,
        scheduled_for: UtcDateTime,
        now: UtcDateTime,
        missed_count: int,
        status: ScheduleOccurrenceStatus,
    ) -> ScheduleOccurrence:
        return ScheduleOccurrence(
            id=new_schedule_occurrence_id(),
            schedule_id=schedule.id,
            interaction_id=new_interaction_id(),
            scheduled_for=scheduled_for,
            status=status,
            missed_count=missed_count,
            claimed_at=now,
            lease_until=now + timedelta(seconds=_OCCURRENCE_LEASE_SECONDS),
            finished_at=now if status is ScheduleOccurrenceStatus.SKIPPED else None,
        )
