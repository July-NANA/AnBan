"""Durable Schedule worker Adapter over the ordinary Interaction gateway."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from anban.core import (
    AnbanError,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    ExecutionRunId,
    SafeMetadata,
    ScheduleDefinition,
    ScheduleId,
    ScheduleOccurrence,
    ScheduleOccurrenceId,
    ScheduleOccurrenceStatus,
    now_utc,
)
from anban.core.models import UtcDateTime
from anban.interaction.contracts import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.interaction.service import InteractionService
from anban.runtime.schedules import ScheduleService

_SCHEDULE_DEDUPLICATION_NAMESPACE = "schedule.occurrence"


class ScheduleDispatchStatus(StrEnum):
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_CLAIMED = "already_claimed"
    RETRY_PENDING = "retry_pending"


class ScheduleDispatchResult(BaseModel):
    """Safe projection of one due occurrence without schedule content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_id: ScheduleId
    occurrence_id: ScheduleOccurrenceId
    status: ScheduleDispatchStatus
    attempt_count: int = Field(ge=1, le=100)
    missed_count: int = Field(ge=0, le=10_000)
    run_id: ExecutionRunId | None = None
    error_code: ErrorCode | None = None


class ScheduleWorkerResult(BaseModel):
    """Bounded result for one scan of the configured Schedule definitions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_count: int = Field(ge=0, le=100)
    dispatches: tuple[ScheduleDispatchResult, ...] = Field(default=(), max_length=100)

    @property
    def failed(self) -> bool:
        return any(
            item.status in {ScheduleDispatchStatus.FAILED, ScheduleDispatchStatus.RETRY_PENDING}
            for item in self.dispatches
        )


class ScheduleWorkerAdapter:
    """Claim due occurrences and deliver them through Interaction exactly once."""

    def __init__(
        self,
        schedules: ScheduleService,
        interactions: InteractionService,
        *,
        clock: Callable[[], UtcDateTime] = now_utc,
    ) -> None:
        self._schedules = schedules
        self._interactions = interactions
        self._clock = clock

    async def run_once(self) -> ScheduleWorkerResult:
        schedules = await self._schedules.list(100)
        dispatches: list[ScheduleDispatchResult] = []
        for schedule in schedules:
            occurrence, claimed = await self._schedules.claim_due(schedule, self._clock())
            if occurrence is None:
                continue
            if not claimed:
                status = (
                    ScheduleDispatchStatus.SKIPPED
                    if occurrence.status is ScheduleOccurrenceStatus.SKIPPED
                    else ScheduleDispatchStatus.FAILED
                    if occurrence.status is ScheduleOccurrenceStatus.FAILED
                    else ScheduleDispatchStatus.ALREADY_CLAIMED
                )
                dispatches.append(
                    self._projection(
                        occurrence,
                        status,
                        error_code=occurrence.error_code,
                    )
                )
                continue
            dispatches.append(await self._dispatch(schedule, occurrence))
        return ScheduleWorkerResult(
            schedule_count=len(schedules),
            dispatches=tuple(dispatches),
        )

    async def _dispatch(
        self, schedule: ScheduleDefinition, occurrence: ScheduleOccurrence
    ) -> ScheduleDispatchResult:
        try:
            result = await self._interactions.submit(
                InteractionEnvelope(
                    id=occurrence.interaction_id,
                    source="schedule.worker",
                    input_kind=InteractionInputKind.SCHEDULE_OCCURRENCE,
                    content=schedule.content,
                    received_at=self._clock(),
                    correlation=InteractionCorrelation(
                        route=InteractionRoute.NEW_TASK,
                        deduplication_key=CorrelationKey(
                            purpose=CorrelationPurpose.DEDUPLICATION,
                            namespace=_SCHEDULE_DEDUPLICATION_NAMESPACE,
                            value=str(occurrence.id),
                        ),
                    ),
                    metadata=SafeMetadata(
                        {
                            "schedule_occurrence_id": str(occurrence.id),
                            "schedule_scheduled_for": occurrence.scheduled_for.isoformat(),
                            "schedule_missed_count": occurrence.missed_count,
                            "schedule_attempt_count": occurrence.attempt_count,
                            "schedule_missed_policy": schedule.missed_policy.value,
                            "schedule_overlap_policy": schedule.overlap_policy.value,
                        }
                    ),
                )
            )
            if not result.persisted:
                raise AnbanError(
                    ErrorInfo(
                        code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                        message="Schedule execution was not durably persisted",
                    )
                )
            error_code = None if result.outcome.error is None else result.outcome.error.code
            completed = await self._schedules.complete_occurrence(
                occurrence,
                run_id=result.run_id,
                error_code=error_code,
                finished_at=self._clock(),
            )
            return self._projection(
                completed,
                ScheduleDispatchStatus.PROCESSED,
                run_id=result.run_id,
                error_code=error_code,
            )
        except AnbanError as exc:
            if exc.info.category in {ErrorCategory.PERSISTENCE, ErrorCategory.AUDIT_TRACE}:
                return self._projection(
                    occurrence,
                    ScheduleDispatchStatus.RETRY_PENDING,
                    error_code=exc.info.code,
                )
            failed = await self._schedules.fail_occurrence(
                occurrence,
                error_code=exc.info.code,
                finished_at=self._clock(),
            )
            return self._projection(
                failed,
                ScheduleDispatchStatus.FAILED,
                error_code=exc.info.code,
            )

    @staticmethod
    def _projection(
        occurrence: ScheduleOccurrence,
        status: ScheduleDispatchStatus,
        *,
        run_id: ExecutionRunId | None = None,
        error_code: ErrorCode | None = None,
    ) -> ScheduleDispatchResult:
        return ScheduleDispatchResult(
            schedule_id=occurrence.schedule_id,
            occurrence_id=occurrence.id,
            status=status,
            attempt_count=occurrence.attempt_count,
            missed_count=occurrence.missed_count,
            run_id=run_id,
            error_code=error_code,
        )
