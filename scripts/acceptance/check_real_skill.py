"""Explicitly validate the approved real Weather Skill and its live service."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from anban.capability import (
    CapabilityResultStatus,
    InvocationContext,
    local_capability_registry,
    register_workspace_skill,
)
from anban.config import load_configuration
from anban.core.errors import AnbanError
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from scripts.doctor import CLAW_CLI, REPOSITORY, command, skill_baseline_result
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace


async def accept_live_skill() -> None:
    workspace = resolve_workspace(repository=REPOSITORY).path
    configuration = load_configuration(workspace=workspace)
    registry = local_capability_registry(
        workspace_root=workspace,
        protected_values=configuration.protected_values(),
    )
    packages = register_workspace_skill(registry, workspace_root=workspace)
    context = InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    activation = await registry.invoke("skill.activate", {"name": "@steipete/weather"}, context)
    if (
        len(packages) != 1
        or activation.status is not CapabilityResultStatus.COMPLETED
        or "wttr.in" not in (activation.observation or "")
        or str(workspace) in str(activation.model_dump(mode="json"))
        or "/tmp/" in (activation.observation or "")
    ):
        raise RuntimeError("skill_activation_invalid")
    weather = await registry.invoke(
        "http.get",
        {"url": "https://wttr.in/Sydney?format=3", "timeout": 30},
        context.model_copy(update={"invocation_id": new_capability_invocation_id()}),
    )
    if (
        weather.status is not CapabilityResultStatus.COMPLETED
        or "Sydney" not in (weather.observation or "")
        or weather.metadata.root.get("status_code") != 200
    ):
        raise RuntimeError("real_skill_response_invalid")


def main() -> int:
    try:
        workspace = resolve_workspace(repository=REPOSITORY).path
    except WorkspaceResolutionError as exc:
        print(f"real Skill: FAIL [{exc.code}] Workspace resolution failed.")
        return 1

    try:
        cli_version = command("npx", "--offline", "--yes", CLAW_CLI, "--cli-version", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"real Skill: FAIL [clawhub_cli_unavailable] {type(exc).__name__}")
        return 1
    baseline = skill_baseline_result(workspace, cli_version)
    if baseline.status != "PASS":
        print(f"real Skill: FAIL [{baseline.code}] {baseline.detail}")
        return 1

    try:
        asyncio.run(accept_live_skill())
    except AnbanError as exc:
        print(f"real Skill: FAIL [{exc.info.code.value}] governed execution failed")
        return 1
    except Exception as exc:
        print(f"real Skill: FAIL [real_skill_failed] {type(exc).__name__}")
        return 1

    print("real Skill: PASS discovery, pin, activation, and production http.get weather request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
