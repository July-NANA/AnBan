from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from anban.core import (
    AnbanError,
    ScheduleKind,
    ScheduleMissedPolicy,
    ScheduleOccurrenceStatus,
    new_execution_run_id,
    new_schedule_id,
)
from anban.runtime import ScheduleService, next_cron_occurrence, next_interval_occurrence
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory


def fixed_clock() -> datetime:
    return datetime(2026, 3, 7, 14, 1, tzinfo=UTC)


def test_cron_occurrence_uses_named_timezone_across_dst_change() -> None:
    occurrence = next_cron_occurrence("0 9 * * *", "America/New_York", fixed_clock())

    assert occurrence == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)


def test_cron_rejects_non_posix_and_impossible_calendar_expression() -> None:
    for expression in ("@daily", "0 0 31 2 *"):
        with pytest.raises(AnbanError) as raised:
            next_cron_occurrence(expression, "UTC", fixed_clock())

        assert raised.value.info.details.root["reason"] == "cron_expression_invalid"


def test_interval_occurrence_is_elapsed_utc_time() -> None:
    assert next_interval_occurrence(90, fixed_clock()) == datetime(
        2026, 3, 7, 14, 2, 30, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_schedule_definitions_survive_fresh_service_and_preserve_kinds() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)

    cron = await service.create_cron(
        name="weekday-report",
        expression="30 9 * * 1-5",
        timezone="Asia/Shanghai",
        content="Prepare the bounded weekday report.",
    )
    interval = await service.create_interval(
        name="health-cycle",
        every_seconds=75,
        timezone="UTC",
        content="Check the bounded health object.",
    )

    restarted = ScheduleService(factory, clock=fixed_clock)
    listed = await restarted.list()
    assert {item.id for item in listed} == {cron.id, interval.id}
    assert (await restarted.get(cron.id)).kind is ScheduleKind.CRON
    assert (await restarted.get(interval.id)).kind is ScheduleKind.INTERVAL
    assert interval.next_occurrence_at == datetime(2026, 3, 7, 14, 2, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_unknown_schedule_and_invalid_query_limit_fail_explicitly() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)

    with pytest.raises(AnbanError) as limit:
        await service.list(0)
    assert limit.value.info.details.root["reason"] == "schedule_limit_invalid"
    with pytest.raises(AnbanError) as unknown:
        await service.list_occurrences(new_schedule_id())
    assert unknown.value.info.details.root["reason"] == "schedule_unknown"


async def test_due_interval_claim_is_durable_and_can_complete_with_one_run() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)
    schedule = await service.create_interval(
        name="durable-claim",
        every_seconds=10,
        timezone="UTC",
        content="Dispatch one durable interval occurrence.",
    )

    occurrence, claimed = await service.claim_due(schedule, fixed_clock() + timedelta(seconds=10))

    assert occurrence is not None
    assert claimed is True
    assert occurrence.status is ScheduleOccurrenceStatus.CLAIMED
    run_id = new_execution_run_id()
    completed = await service.complete_occurrence(
        occurrence,
        run_id=run_id,
        error_code=None,
        finished_at=fixed_clock() + timedelta(seconds=11),
    )
    restarted = ScheduleService(factory, clock=fixed_clock)
    stored = await restarted.list_occurrences(schedule.id)
    assert stored == (completed,)
    assert stored[0].run_id == run_id
    assert stored[0].status is ScheduleOccurrenceStatus.PROCESSED


async def test_overlap_defaults_to_skip_and_expired_claim_recovers_same_identity() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)
    schedule = await service.create_interval(
        name="overlap-skip",
        every_seconds=10,
        timezone="UTC",
        content="Hold one occurrence while the next becomes due.",
    )
    first, claimed = await service.claim_due(schedule, fixed_clock() + timedelta(seconds=10))
    assert first is not None and claimed

    overlap, overlap_claimed = await service.claim_due(
        schedule, fixed_clock() + timedelta(seconds=20)
    )
    assert overlap is not None
    assert overlap_claimed is False
    assert overlap.status is ScheduleOccurrenceStatus.SKIPPED

    recovered, recovered_claimed = await service.claim_due(
        schedule, first.lease_until + timedelta(seconds=1)
    )
    assert recovered is not None
    assert recovered_claimed is True
    assert recovered.id == first.id
    assert recovered.interaction_id == first.interaction_id
    assert recovered.attempt_count == 2


async def test_expired_claim_at_attempt_limit_fails_durably() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)
    schedule = await service.create_interval(
        name="retry-limit",
        every_seconds=10,
        timezone="UTC",
        content="Stop after the bounded occurrence retry limit.",
    )
    occurrence, claimed = await service.claim_due(schedule, fixed_clock() + timedelta(seconds=10))
    assert occurrence is not None and claimed
    factory.store.schedule_occurrences[occurrence.id] = occurrence.model_copy(
        update={"attempt_count": 100}
    )

    failed, reclaimed = await service.claim_due(
        schedule, occurrence.lease_until + timedelta(seconds=1)
    )

    assert failed is not None
    assert reclaimed is False
    assert failed.status is ScheduleOccurrenceStatus.FAILED
    assert failed.error_code is not None


@pytest.mark.parametrize(
    ("policy", "expected_status", "expected_missed", "expected_claimed"),
    [
        (ScheduleMissedPolicy.SKIP, ScheduleOccurrenceStatus.SKIPPED, 3, False),
        (ScheduleMissedPolicy.CATCH_UP_ONCE, ScheduleOccurrenceStatus.CLAIMED, 2, True),
    ],
)
async def test_missed_occurrence_policy_is_explicit_and_bounded(
    policy: ScheduleMissedPolicy,
    expected_status: ScheduleOccurrenceStatus,
    expected_missed: int,
    expected_claimed: bool,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)
    schedule = await service.create_interval(
        name=f"missed-{policy.value}",
        every_seconds=10,
        timezone="UTC",
        content="Apply the configured missed occurrence policy.",
        missed_policy=policy,
    )

    occurrence, claimed = await service.claim_due(schedule, fixed_clock() + timedelta(seconds=35))

    assert occurrence is not None
    assert claimed is expected_claimed
    assert occurrence.status is expected_status
    assert occurrence.missed_count == expected_missed
    assert occurrence.scheduled_for == fixed_clock() + timedelta(seconds=30)


async def test_large_skip_backlog_advances_in_a_bounded_durable_chunk() -> None:
    factory = MemoryUnitOfWorkFactory()
    service = ScheduleService(factory, clock=fixed_clock)
    schedule = await service.create_interval(
        name="bounded-skip-backlog",
        every_seconds=1,
        timezone="UTC",
        content="Skip a large delayed interval without poisoning later worker scans.",
        missed_policy=ScheduleMissedPolicy.SKIP,
    )

    occurrence, claimed = await service.claim_due(
        schedule, fixed_clock() + timedelta(seconds=10_001)
    )

    assert occurrence is not None
    assert claimed is False
    assert occurrence.status is ScheduleOccurrenceStatus.SKIPPED
    assert occurrence.missed_count == 10_000
    assert occurrence.scheduled_for == fixed_clock() + timedelta(seconds=10_000)
