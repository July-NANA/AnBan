"""Real D33 Schedule worker, Provider, PostgreSQL, concurrency, and restart acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core import ScheduleId
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class ScheduleDispatchAcceptanceError(RuntimeError):
    """Safe failure without Task content, Provider output, URLs, or physical paths."""


async def cli_json(*arguments: str, timeout: float = 360) -> tuple[int, object]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "anban.cli",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ScheduleDispatchAcceptanceError("Schedule worker CLI timed out") from None
    if len(stdout) > 131_072:
        raise ScheduleDispatchAcceptanceError("Schedule worker CLI output exceeded its bound")
    try:
        payload: object = json.loads(stdout.decode()) if stdout.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    return process.returncode or 0, payload


def one_schedule(payload: object) -> dict[str, object]:
    if not isinstance(payload, list):
        raise ScheduleDispatchAcceptanceError("Schedule creation projection was invalid")
    items = cast(list[object], payload)
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ScheduleDispatchAcceptanceError("Schedule creation projection was invalid")
    return cast(dict[str, object], items[0])


async def create_daily_variant(
    *, name: str, content: str, timezone: str, target: datetime
) -> dict[str, object]:
    local = target.astimezone(ZoneInfo(timezone))
    expression = f"{local.minute} {local.hour} * * *"
    code, payload = await cli_json(
        "schedule",
        "create-cron",
        name,
        expression,
        timezone,
        content,
        "--json",
    )
    projection = one_schedule(payload)
    if (
        code != 0
        or projection.get("name") != name
        or projection.get("timezone") != timezone
        or projection.get("missed_policy") != "skip"
        or projection.get("overlap_policy") != "skip"
        or projection.get("content_hash") != hashlib.sha256(content.encode()).hexdigest()
    ):
        raise ScheduleDispatchAcceptanceError("Schedule dispatch variant was not durable")
    return projection


async def run_concurrent_workers(count: int) -> tuple[dict[str, object], ...]:
    results = await asyncio.gather(
        *(cli_json("scheduler", "run-once", "--json") for _ in range(count))
    )
    payloads: list[dict[str, object]] = []
    for code, payload in results:
        if code != 0 or not isinstance(payload, dict):
            raise ScheduleDispatchAcceptanceError("Concurrent Schedule worker failed")
        payloads.append(cast(dict[str, object], payload))
    return tuple(payloads)


async def verify_owned(
    projections: tuple[dict[str, object], ...], contents: tuple[str, ...]
) -> tuple[dict[str, str], ...]:
    application = await build_query_application()
    try:
        inbox = await application.interactions.inbox(100)
        inbox_by_id = {str(item.interaction_id): item for item in inbox}
        evidence: list[dict[str, str]] = []
        for projection, content in zip(projections, contents, strict=True):
            schedule_id = ScheduleId(UUID(cast(str, projection["id"])))
            occurrences = await application.schedules.list_occurrences(schedule_id, 10)
            if len(occurrences) != 1:
                raise ScheduleDispatchAcceptanceError(
                    "Concurrent workers did not own exactly one occurrence"
                )
            occurrence = occurrences[0]
            if occurrence.status.value != "processed" or occurrence.run_id is None:
                raise ScheduleDispatchAcceptanceError("Schedule occurrence did not reach a Run")
            delivery = inbox_by_id.get(str(occurrence.interaction_id))
            if (
                delivery is None
                or delivery.delivery_count != 1
                or delivery.input_kind != "schedule_occurrence"
                or delivery.run_id != occurrence.run_id
            ):
                raise ScheduleDispatchAcceptanceError("Schedule inbox correlation was incomplete")
            detail = await application.interactions.show_run(occurrence.run_id)
            types = tuple(item.event_type for item in detail.observability.trace)
            if (
                detail.run.status.value != "succeeded"
                or not detail.observability.complete
                or types.count("schedule.occurrence_dispatched") != 1
                or types.index("schedule.occurrence_dispatched")
                >= types.index("interaction.routed")
                or content in str(detail.observability)
            ):
                raise ScheduleDispatchAcceptanceError("Schedule Run evidence did not reconcile")
            event = next(
                item
                for item in detail.observability.audit
                if item.event_type == "schedule.occurrence_dispatched"
            )
            if (
                event.metadata.root.get("schedule_occurrence_id") != str(occurrence.id)
                or event.metadata.root.get("schedule_missed_policy") != "skip"
                or event.metadata.root.get("schedule_overlap_policy") != "skip"
            ):
                raise ScheduleDispatchAcceptanceError("Schedule Audit metadata was incomplete")
            evidence.append(
                {
                    "schedule_id": str(schedule_id),
                    "occurrence_id": str(occurrence.id),
                    "run_id": str(occurrence.run_id),
                }
            )
        return tuple(evidence)
    finally:
        await application.close()


async def verify_missed_skip(marker: str) -> dict[str, str]:
    name = f"d33-{marker}-missed"
    content = f"Skip a deliberately delayed occurrence object {marker}."
    code, payload = await cli_json(
        "schedule", "create-interval", name, "1", "UTC", content, "--json"
    )
    projection = one_schedule(payload)
    if code != 0:
        raise ScheduleDispatchAcceptanceError("Missed-policy Schedule was not created")
    await asyncio.sleep(2.2)
    code, worker = await cli_json("scheduler", "run-once", "--json")
    if code != 0 or not isinstance(worker, dict):
        raise ScheduleDispatchAcceptanceError("Missed-policy worker failed")
    application = await build_query_application()
    try:
        occurrences = await application.schedules.list_occurrences(
            ScheduleId(UUID(cast(str, projection["id"]))), 10
        )
    finally:
        await application.close()
    if (
        len(occurrences) != 1
        or occurrences[0].status.value != "skipped"
        or occurrences[0].run_id is not None
        or occurrences[0].missed_count < 2
    ):
        raise ScheduleDispatchAcceptanceError("Missed-policy skip fabricated execution")
    return {
        "schedule_id": cast(str, projection["id"]),
        "occurrence_id": str(occurrences[0].id),
        "status": "skipped",
    }


async def accept_schedule_dispatch() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d33-dispatch-{marker}")
    target = (datetime.now(UTC) + timedelta(minutes=2)).replace(second=0, microsecond=0)
    timezones = ("UTC", "Asia/Shanghai", "America/New_York")
    contents = tuple(
        f"Explain one bounded automation property for variant {index} nonce {marker}."
        for index in range(3)
    )
    with isolated_environment(workspace, source):
        projections = tuple(
            [
                await create_daily_variant(
                    name=f"d33-{marker}-{index}",
                    content=content,
                    timezone=timezone,
                    target=target,
                )
                for index, (content, timezone) in enumerate(
                    zip(contents, timezones, strict=True), start=1
                )
            ]
        )
        await asyncio.sleep(
            max(0.0, (target + timedelta(seconds=1) - datetime.now(UTC)).total_seconds())
        )
        worker_results = await run_concurrent_workers(len(projections))
        owned = await verify_owned(projections, contents)
        missed = await verify_missed_skip(marker)
    return {
        "variants": owned,
        "worker_processes": len(worker_results),
        "concurrent_claims": "one_occurrence_per_schedule",
        "fresh_application_reconstruction": True,
        "missed_policy": missed,
        "audit_event": "schedule.occurrence_dispatched",
        "scenarios": ["S01", "S02", "S03", "S04", "S08", "S09", "S10", "S11"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_schedule_dispatch())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ScheduleDispatchAcceptanceError) else "unexpected"
        print(
            f"Schedule dispatch acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "Schedule dispatch acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
