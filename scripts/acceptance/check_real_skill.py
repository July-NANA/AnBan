"""Explicitly validate the approved real Weather Skill and its live service."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anban.capability import (
    CapabilityRegistry,
    CapabilityResultStatus,
    InvocationContext,
    register_workspace_skill,
)
from anban.core.errors import AnbanError
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from scripts.doctor import CLAW_CLI, REPOSITORY, command, skill_baseline_result
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace


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
        registry = CapabilityRegistry()
        packages = register_workspace_skill(registry, workspace_root=workspace)
        context = InvocationContext(
            run_id=new_execution_run_id(),
            node_run_id=new_node_run_id(),
            invocation_id=new_capability_invocation_id(),
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        activation = asyncio.run(
            registry.invoke("skill.activate", {"name": "@steipete/weather"}, context)
        )
    except AnbanError as exc:
        print(f"real Skill: FAIL [{exc.info.code.value}] activation failed")
        return 1
    if (
        len(packages) != 1
        or activation.status is not CapabilityResultStatus.COMPLETED
        or "wttr.in" not in (activation.observation or "")
        or str(workspace) in str(activation.model_dump(mode="json"))
        or "/tmp/" in (activation.observation or "")
    ):
        print("real Skill: FAIL [skill_activation_invalid] safe activation mismatch")
        return 1

    try:
        response = command(
            "curl",
            "-fsS",
            "--max-time",
            "30",
            "https://wttr.in/Sydney?format=3",
            cwd=Path.cwd(),
            timeout=40,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"real Skill: FAIL [real_skill_network_failed] {type(exc).__name__}")
        return 1
    if "Sydney" not in response:
        print("real Skill: FAIL [real_skill_response_invalid] City identity missing.")
        return 1

    print(
        "real Skill: PASS discovery, version, pin, hash, safe activation, and live weather request"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
