"""CLI projection for durable schedule definitions."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from anban.application import build_query_application
from anban.core import ScheduleDefinition, ScheduleId

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
    add_json_option(cron)
    interval = actions.add_parser("create-interval", help="Create an elapsed-time schedule.")
    interval.add_argument("name")
    interval.add_argument("every_seconds", type=int)
    interval.add_argument("timezone")
    interval.add_argument("content", nargs="+")
    add_json_option(interval)
    show = actions.add_parser("show", help="Show one schedule definition.")
    show.add_argument("schedule_id")
    add_json_option(show)


async def run_schedule_command(arguments: argparse.Namespace, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        if arguments.command == "schedules":
            schedules = await application.schedules.list(arguments.limit)
            emit_schedules(schedules, json_output=json_output)
            return 0
        if arguments.schedule_command == "create-cron":
            created = await application.schedules.create_cron(
                name=arguments.name,
                expression=arguments.expression,
                timezone=arguments.timezone,
                content=" ".join(arguments.content),
            )
        elif arguments.schedule_command == "create-interval":
            created = await application.schedules.create_interval(
                name=arguments.name,
                every_seconds=arguments.every_seconds,
                timezone=arguments.timezone,
                content=" ".join(arguments.content),
            )
        else:
            created = await application.schedules.get(ScheduleId(UUID(arguments.schedule_id)))
        emit_schedules((created,), json_output=json_output)
        return 0
    finally:
        await application.close()


def schedule_projection(schedule: ScheduleDefinition) -> dict[str, object]:
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "kind": schedule.kind.value,
        "timezone": schedule.timezone,
        "cron_expression": schedule.cron_expression,
        "every_seconds": schedule.every_seconds,
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
