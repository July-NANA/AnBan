"""CLI projection for durable schedule definitions."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from anban.application import build_application, build_query_application
from anban.core import (
    ScheduleDefinition,
    ScheduleId,
    ScheduleMissedPolicy,
    ScheduleOccurrence,
)
from anban.interaction import ScheduleWorkerResult

AddJsonOption = Callable[[argparse.ArgumentParser], None]


def configure_schedule_commands(
    commands: Any,
    add_json_option: AddJsonOption,
) -> None:
    schedules = commands.add_parser("schedules", help="List durable schedule definitions.")
    schedules.add_argument("--limit", type=int, default=20)
    add_json_option(schedules)

    schedule = commands.add_parser("schedule", help="Create or inspect one schedule.")
    actions = schedule.add_subparsers(dest="schedule_command", required=True)
    cron = actions.add_parser("create-cron", help="Create a five-field Cron schedule.")
    cron.add_argument("name")
    cron.add_argument("expression")
    cron.add_argument("timezone")
    cron.add_argument("content", nargs="+")
    cron.add_argument(
        "--missed-policy",
        choices=tuple(item.value for item in ScheduleMissedPolicy),
        default=ScheduleMissedPolicy.SKIP.value,
    )
    add_json_option(cron)
    interval = actions.add_parser("create-interval", help="Create an elapsed-time schedule.")
    interval.add_argument("name")
    interval.add_argument("every_seconds", type=int)
    interval.add_argument("timezone")
    interval.add_argument("content", nargs="+")
    interval.add_argument(
        "--missed-policy",
        choices=tuple(item.value for item in ScheduleMissedPolicy),
        default=ScheduleMissedPolicy.SKIP.value,
    )
    add_json_option(interval)
    show = actions.add_parser("show", help="Show one schedule definition.")
    show.add_argument("schedule_id")
    add_json_option(show)
    occurrences = actions.add_parser("occurrences", help="List durable trigger occurrences.")
    occurrences.add_argument("schedule_id")
    occurrences.add_argument("--limit", type=int, default=20)
    add_json_option(occurrences)
    scheduler = commands.add_parser("scheduler", help="Dispatch due durable schedules.")
    scheduler_actions = scheduler.add_subparsers(dest="scheduler_command", required=True)
    run_once = scheduler_actions.add_parser("run-once", help="Scan and dispatch due work once.")
    add_json_option(run_once)


async def run_schedule_command(arguments: argparse.Namespace, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        if arguments.command == "schedules":
            schedules = await application.schedules.list(arguments.limit)
            emit_schedules(schedules, json_output=json_output)
            return 0
        if arguments.schedule_command == "occurrences":
            occurrences = await application.schedules.list_occurrences(
                ScheduleId(UUID(arguments.schedule_id)), arguments.limit
            )
            emit_occurrences(occurrences, json_output=json_output)
            return 0
        if arguments.schedule_command == "create-cron":
            created = await application.schedules.create_cron(
                name=arguments.name,
                expression=arguments.expression,
                timezone=arguments.timezone,
                content=" ".join(arguments.content),
                missed_policy=ScheduleMissedPolicy(arguments.missed_policy),
            )
        elif arguments.schedule_command == "create-interval":
            created = await application.schedules.create_interval(
                name=arguments.name,
                every_seconds=arguments.every_seconds,
                timezone=arguments.timezone,
                content=" ".join(arguments.content),
                missed_policy=ScheduleMissedPolicy(arguments.missed_policy),
            )
        else:
            created = await application.schedules.get(ScheduleId(UUID(arguments.schedule_id)))
        emit_schedules((created,), json_output=json_output)
        return 0
    finally:
        await application.close()


async def run_scheduler_command(*, json_output: bool) -> int:
    application = await build_application()
    try:
        result = await application.scheduler.run_once()
    finally:
        await application.close()
    emit_worker_result(result, json_output=json_output)
    return 1 if result.failed else 0


def schedule_projection(schedule: ScheduleDefinition) -> dict[str, object]:
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "kind": schedule.kind.value,
        "timezone": schedule.timezone,
        "cron_expression": schedule.cron_expression,
        "every_seconds": schedule.every_seconds,
        "missed_policy": schedule.missed_policy.value,
        "overlap_policy": schedule.overlap_policy.value,
        "content_hash": hashlib.sha256(schedule.content.encode()).hexdigest(),
        "content_size": len(schedule.content.encode()),
        "anchor_at": schedule.anchor_at.isoformat(),
        "next_occurrence_at": schedule.next_occurrence_at.isoformat(),
        "created_at": schedule.created_at.isoformat(),
    }


def emit_schedules(
    schedules: tuple[ScheduleDefinition, ...],
    *,
    json_output: bool,
) -> None:
    projections = tuple(schedule_projection(schedule) for schedule in schedules)
    if json_output:
        print(json.dumps(projections, separators=(",", ":")))
        return
    if not projections:
        print("No schedules.")
        return
    for item in projections:
        detail = item["cron_expression"] or f"every {item['every_seconds']}s"
        print(
            f"{item['id']}  {item['name']}  {item['kind']}  {item['timezone']}  "
            f"{detail}  next={item['next_occurrence_at']}"
        )


def occurrence_projection(occurrence: ScheduleOccurrence) -> dict[str, object]:
    return {
        "id": str(occurrence.id),
        "schedule_id": str(occurrence.schedule_id),
        "interaction_id": str(occurrence.interaction_id),
        "scheduled_for": occurrence.scheduled_for.isoformat(),
        "status": occurrence.status.value,
        "missed_count": occurrence.missed_count,
        "attempt_count": occurrence.attempt_count,
        "claimed_at": occurrence.claimed_at.isoformat(),
        "lease_until": occurrence.lease_until.isoformat(),
        "finished_at": (
            None if occurrence.finished_at is None else occurrence.finished_at.isoformat()
        ),
        "run_id": None if occurrence.run_id is None else str(occurrence.run_id),
        "error_code": None if occurrence.error_code is None else occurrence.error_code.value,
    }


def emit_occurrences(occurrences: tuple[ScheduleOccurrence, ...], *, json_output: bool) -> None:
    projections = tuple(occurrence_projection(item) for item in occurrences)
    if json_output:
        print(json.dumps(projections, separators=(",", ":")))
        return
    if not projections:
        print("No schedule occurrences.")
        return
    for item in projections:
        print(
            f"{item['id']}  {item['status']}  scheduled={item['scheduled_for']}  "
            f"run={item['run_id']}  attempts={item['attempt_count']}"
        )


def emit_worker_result(result: ScheduleWorkerResult, *, json_output: bool) -> None:
    if json_output:
        print(result.model_dump_json())
        return
    print(f"Schedules scanned: {result.schedule_count}")
    if not result.dispatches:
        print("No due schedule occurrences.")
        return
    for dispatch in result.dispatches:
        print(
            f"{dispatch.occurrence_id}  {dispatch.status.value}  "
            f"run={dispatch.run_id}  attempts={dispatch.attempt_count}"
        )
