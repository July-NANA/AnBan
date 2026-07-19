"""Real D32 Cron/Interval definition, PostgreSQL, timezone, and CLI acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import cast
from zoneinfo import ZoneInfo

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core import ScheduleDefinition
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class ScheduleAcceptanceError(RuntimeError):
    """Safe failure without task content, database URLs, or physical paths."""


@dataclass(frozen=True)
class Variant:
    label: str
    command: tuple[str, ...]
    name: str
    timezone: str
    content: str
    local_hour: int | None = None
    local_minute: int | None = None


async def cli_json(*arguments: str) -> tuple[int, object, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "anban.cli",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ScheduleAcceptanceError("schedule CLI timed out") from None
    if len(stdout) > 65_536:
        raise ScheduleAcceptanceError("schedule CLI output exceeded its bound")
    text = stdout.decode("utf-8")
    try:
        payload: object = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = None
    return process.returncode or 0, payload, text


async def database_snapshot() -> tuple[
    tuple[ScheduleDefinition, ...], tuple[str, ...], tuple[str, ...]
]:
    application = await build_query_application()
    try:
        schedules = await application.schedules.list(100)
        runs = tuple(str(run.id) for run in await application.interactions.runs(100))
        inbox = tuple(
            str(entry.interaction_id) for entry in await application.interactions.inbox(100)
        )
        return schedules, runs, inbox
    finally:
        await application.close()


def one_projection(payload: object) -> dict[str, object]:
    if not isinstance(payload, list):
        raise ScheduleAcceptanceError("schedule CLI omitted its bounded projection")
    items = cast(list[object], payload)
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ScheduleAcceptanceError("schedule CLI omitted its bounded projection")
    return cast(dict[str, object], items[0])


async def create_variant(variant: Variant) -> dict[str, object]:
    code, payload, raw_output = await cli_json(*variant.command, "--json")
    projection = one_projection(payload)
    if (
        code != 0
        or projection.get("name") != variant.name
        or projection.get("timezone") != variant.timezone
        or variant.content in raw_output
        or projection.get("content_hash") != hashlib.sha256(variant.content.encode()).hexdigest()
    ):
        raise ScheduleAcceptanceError("schedule creation did not preserve its safe contract")
    return projection


async def reverse_cases(variants: tuple[Variant, ...], expected_count: int) -> dict[str, str]:
    cases = (
        (
            "invalid_cron",
            (
                "schedule",
                "create-cron",
                variants[0].name + "-invalid",
                "0 0 31 2 *",
                "UTC",
                "Rejected calendar object.",
            ),
        ),
        (
            "invalid_timezone",
            (
                "schedule",
                "create-cron",
                variants[0].name + "-zone",
                "0 9 * * *",
                "Invalid/Nowhere",
                "Rejected timezone object.",
            ),
        ),
        (
            "invalid_interval",
            (
                "schedule",
                "create-interval",
                variants[0].name + "-interval",
                "0",
                "UTC",
                "Rejected interval object.",
            ),
        ),
        (
            "duplicate_name",
            (
                "schedule",
                "create-interval",
                variants[0].name,
                "97",
                "UTC",
                "Conflicting duplicate object.",
            ),
        ),
    )
    results: dict[str, str] = {}
    for label, arguments in cases:
        code, _, _ = await cli_json(*arguments, "--json")
        if code == 0:
            raise ScheduleAcceptanceError("invalid schedule input did not fail explicitly")
        results[label] = "rejected"
    schedules, _, _ = await database_snapshot()
    owned = tuple(
        item for item in schedules if item.name.startswith(variants[0].name.rsplit("-", 1)[0])
    )
    if len(owned) != expected_count:
        raise ScheduleAcceptanceError("rejected schedule input changed durable definitions")
    return results


async def accept_schedules() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d32-schedules-{marker}")
    prefix = f"d32-{marker}"
    variants = (
        Variant(
            "weekday",
            (
                "schedule",
                "create-cron",
                f"{prefix}-weekday",
                "17 9 * * 1-5",
                "Asia/Shanghai",
                f"Prepare weekday object {marker}.",
            ),
            f"{prefix}-weekday",
            "Asia/Shanghai",
            f"Prepare weekday object {marker}.",
            9,
            17,
        ),
        Variant(
            "dst-zone",
            (
                "schedule",
                "create-cron",
                f"{prefix}-dst",
                "43 11 * * *",
                "America/New_York",
                f"Inspect timezone object {marker}.",
            ),
            f"{prefix}-dst",
            "America/New_York",
            f"Inspect timezone object {marker}.",
            11,
            43,
        ),
        Variant(
            "interval",
            (
                "schedule",
                "create-interval",
                f"{prefix}-interval",
                "71",
                "UTC",
                f"Summarize interval object {marker}.",
            ),
            f"{prefix}-interval",
            "UTC",
            f"Summarize interval object {marker}.",
        ),
    )
    with isolated_environment(workspace, source):
        _, before_runs, before_inbox = await database_snapshot()
        projections = tuple([await create_variant(variant) for variant in variants])
        schedules, after_runs, after_inbox = await database_snapshot()
        owned = tuple(item for item in schedules if item.name.startswith(prefix))
        if len(owned) != len(variants) or before_runs != after_runs or before_inbox != after_inbox:
            raise ScheduleAcceptanceError("schedule definition created execution side effects")
        by_name = {item.name: item for item in owned}
        for variant in variants:
            schedule = by_name.get(variant.name)
            if schedule is None or schedule.content != variant.content:
                raise ScheduleAcceptanceError("fresh Application did not reconstruct schedule")
            if variant.local_hour is not None:
                local = schedule.next_occurrence_at.astimezone(ZoneInfo(variant.timezone))
                if (local.hour, local.minute) != (variant.local_hour, variant.local_minute):
                    raise ScheduleAcceptanceError("Cron occurrence lost timezone semantics")
        code, listed, listed_output = await cli_json("schedules", "--limit", "100", "--json")
        if (
            code != 0
            or not isinstance(listed, list)
            or any(variant.content in listed_output for variant in variants)
        ):
            raise ScheduleAcceptanceError("fresh CLI schedule query was incomplete or unsafe")
        reverse = await reverse_cases(variants, len(variants))
    return {
        "variants": [
            {
                "label": variant.label,
                "schedule_id": projection["id"],
                "timezone": variant.timezone,
            }
            for variant, projection in zip(variants, projections, strict=True)
        ],
        "reverse": reverse,
        "fresh_process_queries": True,
        "run_created": False,
        "interaction_delivered": False,
        "scenarios": ["S08", "S11"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_schedules())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ScheduleAcceptanceError) else "unexpected"
        print(f"Schedule acceptance: FAIL ({type(exc).__name__}: {detail})", file=sys.stderr)
        return 1
    print("Schedule acceptance: PASS " + json.dumps(evidence, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
