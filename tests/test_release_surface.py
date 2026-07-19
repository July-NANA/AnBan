"""Freeze the prepared v0.5 package, Gate commands, and release evidence surface."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import cast

REPOSITORY = Path(__file__).parents[1]
CANDIDATE_VERSION = "0.5.0"


def test_python_package_and_lock_use_candidate_version() -> None:
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPOSITORY / "uv.lock").read_text(encoding="utf-8"))
    packages = cast(list[dict[str, object]], lock["package"])
    local = next(item for item in packages if item.get("name") == "anban")

    assert cast(dict[str, object], project["project"])["version"] == CANDIDATE_VERSION
    assert local["version"] == CANDIDATE_VERSION


def test_release_gate_commands_cover_every_phase_and_candidate_closure() -> None:
    package = cast(
        dict[str, object],
        json.loads((REPOSITORY / "package.json").read_text(encoding="utf-8")),
    )
    scripts = cast(dict[str, str], package["scripts"])

    assert {"acceptance:p1", "acceptance:p2", "acceptance:p3", "acceptance:v0.5"} <= scripts.keys()
    assert scripts["acceptance:v0.5"].endswith("pnpm run acceptance:release")
    for integration in ("mcp", "subagent", "webhook", "schedule", "automation"):
        assert f"acceptance:{integration}" in scripts["acceptance:p3"]


def test_release_candidate_notes_name_all_scenarios_and_safety_evidence() -> None:
    notes = (REPOSITORY / "docs/releases/v0.5.0.md").read_text(encoding="utf-8")

    for index in range(1, 13):
        assert f"S{index:02d}" in notes
    for evidence in (
        "PostgreSQL",
        "Audit",
        "Trace",
        "restart",
        "no fallback success",
        "schedule.occurrence_dispatched",
    ):
        assert evidence in notes
