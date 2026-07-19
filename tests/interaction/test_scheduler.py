"""Durable Schedule dispatch through the ordinary Interaction gateway."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from anban.capability import CapabilityRegistry
from anban.core import (
    AnbanError,
    ErrorCode,
    ErrorInfo,
    ExecutionRunId,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
)
from anban.core.models import UtcDateTime
from anban.interaction import (
    InteractionEnvelope,
    InteractionService,
    ScheduleDispatchStatus,
    ScheduleWorkerAdapter,
)
from anban.runtime import ExecutionQueryService, PersistentRuntime, ScheduleService
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import TransactionCheckingModel, final_turn

ANCHOR = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)


class OneShotCompletionFailureScheduleService(ScheduleService):
    def __init__(self, factory: MemoryUnitOfWorkFactory) -> None:
        super().__init__(factory, clock=lambda: ANCHOR)
        self.fail_completion = True

    async def complete_occurrence(
        self,
        occurrence: ScheduleOccurrence,
        *,
        run_id: ExecutionRunId,
        error_code: ErrorCode | None,
        finished_at: UtcDateTime,
    ) -> ScheduleOccurrence:
        if self.fail_completion:
            self.fail_completion = False
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                    message="Test-only ambiguous occurrence completion",
                )
            )
        return await super().complete_occurrence(
            occurrence,
            run_id=run_id,
            error_code=error_code,
            finished_at=finished_at,
        )


async def test_schedule_worker_dispatches_once_and_restart_does_not_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn("Scheduled work completed.")])
    schedules = ScheduleService(factory, clock=lambda: ANCHOR)
    schedule = await schedules.create_interval(
        name="worker-restart",
        every_seconds=15,
        timezone="UTC",
        content="Process a newly varied scheduled object.",
    )
    interactions = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    due = ANCHOR + timedelta(seconds=15)

    first = await ScheduleWorkerAdapter(schedules, interactions, clock=lambda: due).run_once()
    restarted = await ScheduleWorkerAdapter(
        ScheduleService(factory),
        InteractionService(
            PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
        ),
        clock=lambda: due + timedelta(seconds=1),
    ).run_once()

    assert len(first.dispatches) == 1
    dispatch = first.dispatches[0]
    assert dispatch.status is ScheduleDispatchStatus.PROCESSED
    assert dispatch.run_id is not None
    assert restarted.dispatches == ()
    assert model.calls == 1
    occurrences = await ScheduleService(factory).list_occurrences(schedule.id)
    assert len(occurrences) == 1
    assert occurrences[0].status is ScheduleOccurrenceStatus.PROCESSED
    inbox = await interactions.inbox()
    assert len(inbox) == 1
    assert inbox[0].input_kind == "schedule_occurrence"
    trace = await ExecutionQueryService(factory).trace(dispatch.run_id)
    audit_types = tuple(item.event_type for item in trace.audit)
    assert audit_types.index("schedule.occurrence_dispatched") < audit_types.index(
        "interaction.routed"
    )
    event = next(
        item for item in trace.audit if item.event_type == "schedule.occurrence_dispatched"
    )
    assert event.metadata.root["schedule_occurrence_id"] == str(occurrences[0].id)
    assert event.metadata.root["schedule_attempt_count"] == 1
    assert schedule.content not in str(trace)


def test_external_input_cannot_forge_schedule_worker_attestations() -> None:
    with pytest.raises(ValueError, match="cannot supply Adapter attestations"):
        InteractionEnvelope.from_external(
            {
                "input_kind": "schedule_occurrence",
                "content": "Attempt an untrusted Schedule delivery.",
                "metadata": {"schedule_occurrence_id": "untrusted"},
            },
            source="external.adapter",
        )


async def test_completion_ambiguity_reuses_inbox_result_after_lease_without_model_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn("One execution only.")])
    schedules = OneShotCompletionFailureScheduleService(factory)
    schedule = await schedules.create_interval(
        name="retry-same-interaction",
        every_seconds=10,
        timezone="UTC",
        content="Persist one execution before an ambiguous occurrence completion.",
    )
    interactions = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    due = ANCHOR + timedelta(seconds=10)

    first = await ScheduleWorkerAdapter(schedules, interactions, clock=lambda: due).run_once()
    initial = (await ScheduleService(factory).list_occurrences(schedule.id))[0]
    restarted = await ScheduleWorkerAdapter(
        ScheduleService(factory),
        InteractionService(
            PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
        ),
        clock=lambda: initial.lease_until + timedelta(seconds=1),
    ).run_once()

    assert first.dispatches[0].status is ScheduleDispatchStatus.RETRY_PENDING
    assert restarted.dispatches[0].status is ScheduleDispatchStatus.PROCESSED
    final = (await ScheduleService(factory).list_occurrences(schedule.id))[0]
    assert final.id == initial.id
    assert final.interaction_id == initial.interaction_id
    assert final.attempt_count == 2
    assert final.run_id == restarted.dispatches[0].run_id
    assert model.calls == 1
    inbox = await interactions.inbox()
    assert inbox[0].delivery_count == 2
