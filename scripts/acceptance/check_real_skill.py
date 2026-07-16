"""Explicitly validate the approved real Weather Skill and its live service."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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

    skill_file = workspace / "skills" / "@steipete" / "weather" / "SKILL.md"
    try:
        instructions = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"real Skill: FAIL [skill_instruction_unreadable] {type(exc).__name__}")
        return 1
    if "wttr.in" not in instructions:
        print("real Skill: FAIL [skill_instruction_invalid] Approved service instruction missing.")
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

    print("real Skill: PASS approved version, pin, hash, instructions, and live weather request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
