from __future__ import annotations

from datetime import UTC, datetime

import pytest

from anban.core import AnbanError, ScheduleKind
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
