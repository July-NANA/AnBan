"""Repeatable, fail-closed Phase 0 development-readiness checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.check_real_model import ReadinessError, run_check

REPOSITORY = Path(__file__).resolve().parents[1]
LOCAL_WORKSPACE = Path("/Users/fanyuhang/AnbanWorkspace")
LOCAL_INTERPRETER = Path("/Users/fanyuhang/miniforge3/envs/anban/bin/python")
CLAW_CLI = "clawhub@0.23.1"
SKILL_SLUG = "@steipete/weather"
SKILL_VERSION = "1.0.0"
SKILL_HASH = "1ca0c8d768ad603ea8d5d47f56a9b435fe575f7f34e719eda85c82003d740e93"
Status = Literal["PASS", "FAIL", "BLOCKED"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    code: str | None = None
    remediation: str | None = None


def command(*arguments: str, cwd: Path = REPOSITORY, timeout: int = 120) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
    )
    return completed.stdout.strip()


def workspace_path() -> Path:
    configured = os.environ.get("ANBAN_WORKSPACE_DIR")
    return Path(configured).expanduser().resolve() if configured else LOCAL_WORKSPACE


def pass_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "PASS", detail)


def fail_result(name: str, code: str, detail: str, remediation: str) -> CheckResult:
    return CheckResult(name, "FAIL", detail, code, remediation)


def check_repository() -> CheckResult:
    remote = command("git", "remote", "get-url", "origin")
    if remote not in {
        "git@github.com:July-NANA/AnBan.git",
        "https://github.com/July-NANA/AnBan.git",
    }:
        return fail_result(
            "repository",
            "repository_remote_invalid",
            "Unexpected origin remote.",
            "Restore the approved AnBan origin remote.",
        )

    head = command("git", "rev-parse", "HEAD")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if os.environ.get("GITHUB_REF_NAME") != "anban" or os.environ.get("GITHUB_SHA") != head:
            return fail_result(
                "repository",
                "repository_ci_ref_invalid",
                "CI is not checking the exact anban event SHA.",
                "Dispatch the workflow from the anban branch.",
            )
    elif command("git", "branch", "--show-current") != "anban":
        return fail_result(
            "repository",
            "repository_branch_invalid",
            "Current branch is not anban.",
            "Switch to the synchronized anban branch.",
        )

    if command("git", "status", "--porcelain=v1", "--untracked-files=all"):
        return fail_result(
            "repository",
            "repository_worktree_dirty",
            "Working tree is not clean.",
            "Commit or remove the intended files, then rerun doctor.",
        )
    required = (
        "AGENTS.md",
        "environment.yml",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "pnpm-lock.yaml",
        "docs/adr/0001-core-architecture.md",
        "docs/adr/0002-workspace-and-configuration.md",
    )
    missing = [path for path in required if not (REPOSITORY / path).is_file()]
    if missing:
        return fail_result(
            "repository",
            "repository_baseline_missing",
            "One or more baseline files are missing.",
            "Restore the committed Phase 0 baseline files.",
        )
    agents = (REPOSITORY / "AGENTS.md").read_text(encoding="utf-8")
    forbidden = ("Rust", "Axum", "Cargo", "Tauri", "SkillManifest", "ToolManifest")
    if any(token in agents for token in forbidden):
        return fail_result(
            "repository",
            "repository_legacy_instruction_found",
            "AGENTS.md contains a legacy Rust-era instruction.",
            "Remove the legacy instruction and preserve the Python architecture baseline.",
        )
    tracked = command("git", "ls-files")
    if ".env" in tracked.splitlines():
        return fail_result(
            "repository",
            "repository_env_tracked",
            "A real .env file is tracked.",
            "Remove .env from Git without printing its values.",
        )
    return pass_result("repository", f"anban exact HEAD {head[:12]}, clean tree, approved origin")


def check_miniforge() -> CheckResult:
    base = Path(command("conda", "info", "--base")).resolve()
    executable = Path(sys.executable).resolve()
    if "miniforge" not in base.name.lower():
        return fail_result(
            "Miniforge",
            "miniforge_base_invalid",
            "Conda base is not a Miniforge installation.",
            "Install and activate Miniforge before running doctor.",
        )
    if sys.version_info[:2] != (3, 12) or executable.parent.parent.name != "anban":
        return fail_result(
            "Miniforge",
            "miniforge_environment_invalid",
            "Python is not 3.12 from the anban environment.",
            "Activate the Miniforge anban environment.",
        )
    agents = (REPOSITORY / "AGENTS.md").read_text(encoding="utf-8")
    local_mismatch = executable != LOCAL_INTERPRETER or str(executable) not in agents
    if os.environ.get("GITHUB_ACTIONS") != "true" and local_mismatch:
        return fail_result(
            "Miniforge",
            "miniforge_interpreter_mismatch",
            "Local interpreter differs from the AGENTS.md record.",
            "Recreate the documented Miniforge environment or update the verified record.",
        )
    uv_version = command("uv", "--version")
    return pass_result(
        "Miniforge",
        f"{base}, Python {sys.version_info.major}.{sys.version_info.minor}, {uv_version}",
    )


def load_workspace_config(workspace: Path) -> dict[str, object]:
    with (workspace / "anban.toml").open("rb") as handle:
        return tomllib.load(handle)


def check_workspace() -> CheckResult:
    workspace = workspace_path()
    if os.environ.get("GITHUB_ACTIONS") != "true" and workspace != LOCAL_WORKSPACE:
        return fail_result(
            "Workspace",
            "workspace_path_invalid",
            "Local Workspace is not the canonical path.",
            "Set ANBAN_WORKSPACE_DIR to /Users/fanyuhang/AnbanWorkspace.",
        )
    required = ("skills", "runs", "artifacts", "cache", "logs", "tmp")
    if not workspace.is_dir() or any(not (workspace / name).is_dir() for name in required):
        return fail_result(
            "Workspace",
            "workspace_layout_invalid",
            "Managed Workspace layout is incomplete.",
            "Recreate the documented Workspace directories.",
        )
    if stat.S_IMODE(workspace.stat().st_mode) != 0o700:
        return fail_result(
            "Workspace",
            "workspace_permissions_invalid",
            "Workspace root mode is not 0700.",
            "Set the Workspace root mode to 0700.",
        )
    secrets = workspace / "secrets.env"
    if not secrets.is_file() or stat.S_IMODE(secrets.stat().st_mode) != 0o600:
        return fail_result(
            "Workspace",
            "workspace_secret_permissions_invalid",
            "secrets.env is missing or not mode 0600.",
            "Create secrets.env and set mode 0600.",
        )
    try:
        config = load_workspace_config(workspace)
    except (OSError, tomllib.TOMLDecodeError):
        return fail_result(
            "Workspace",
            "workspace_configuration_invalid",
            "anban.toml cannot be parsed.",
            "Restore the documented schema_version 1 configuration.",
        )
    if config.get("schema_version") != 1 or config.get("workspace_id") != "local-main":
        return fail_result(
            "Workspace",
            "workspace_configuration_invalid",
            "anban.toml identity fields are invalid.",
            "Restore schema_version 1 and workspace_id local-main.",
        )
    return pass_result("Workspace", f"{workspace}, mode 0700, secrets mode 0600, TOML valid")


async def database_probe(url: str, expected_database: str) -> None:
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(text("SELECT current_database(), current_user, version()"))
            ).one()
            if row[0] != expected_database or row[1] != "anban":
                raise RuntimeError("database identity mismatch")
            table_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
                    )
                )
            ).scalar_one()
            if table_count != 0:
                raise RuntimeError("database contains unexpected tables")
            await connection.rollback()
            transaction = await connection.begin()
            try:
                await connection.execute(text("CREATE TEMP TABLE readiness_probe(value text)"))
                await connection.execute(text("INSERT INTO readiness_probe VALUES ('ok')"))
                value = (
                    await connection.execute(text("SELECT value FROM readiness_probe"))
                ).scalar_one()
                if value != "ok":
                    raise RuntimeError("database write probe mismatch")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def check_postgresql() -> CheckResult:
    values = dotenv_values(workspace_path() / "secrets.env", interpolate=False)
    development = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
    test = os.environ.get("ANBAN_TEST_DATABASE_URL") or values.get("ANBAN_TEST_DATABASE_URL")
    if not isinstance(development, str) or not isinstance(test, str):
        return fail_result(
            "PostgreSQL",
            "postgresql_configuration_missing",
            "Database URL references are missing.",
            "Configure both database URLs in Workspace secrets.env.",
        )
    try:
        asyncio.run(database_probe(development, "anban"))
        asyncio.run(database_probe(test, "anban_test"))
    except Exception as exc:
        return fail_result(
            "PostgreSQL",
            "postgresql_probe_failed",
            f"A real database probe failed ({type(exc).__name__}).",
            "Start both PostgreSQL instances; ensure target databases are empty and writable.",
        )
    return pass_result(
        "PostgreSQL",
        "development and test databases connected, empty, writable, and rollback-clean",
    )


def model_results() -> list[CheckResult]:
    try:
        result = run_check(workspace_path())
    except ReadinessError as exc:
        status: Status = "BLOCKED" if exc.blocked else "FAIL"
        return [CheckResult("real model", status, exc.message, exc.code, exc.remediation)]
    return [
        pass_result(
            "real model",
            f"provider={result.provider_type}, model={result.model}, normal response received",
        ),
        pass_result("native Tool Calling", "one schema-conformant native Tool Call received"),
        pass_result(
            "real capability", "isolated validation.txt was created, read, verified, and cleaned"
        ),
        pass_result(
            "Tool Result round trip",
            "real Tool result returned to model and final response received",
        ),
    ]


def check_clawhub() -> list[CheckResult]:
    workspace = workspace_path()
    skill_file = workspace / "skills" / "@steipete" / "weather" / "SKILL.md"
    lock_file = workspace / ".clawhub" / "lock.json"
    try:
        node = command("node", "--version")
        pnpm = command("pnpm", "--version")
        cli = command("npx", "--yes", CLAW_CLI, "--cli-version", timeout=180)
        listing = command(
            "npx",
            "--yes",
            CLAW_CLI,
            "--workdir",
            str(workspace),
            "--dir",
            "skills",
            "list",
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            fail_result(
                "ClawHub",
                "clawhub_cli_failed",
                f"ClawHub CLI invocation failed ({type(exc).__name__}).",
                "Restore Node, pnpm, network access, and the pinned ClawHub CLI.",
            )
        ]
    if not skill_file.is_file() or not lock_file.is_file():
        return [
            fail_result(
                "real Skill",
                "clawhub_skill_missing",
                "The pinned real Weather Skill is incomplete.",
                "Install @steipete/weather@1.0.0 into the Workspace.",
            )
        ]
    digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    lock = cast(dict[str, object], json.loads(lock_file.read_text(encoding="utf-8")))
    skills = cast(dict[str, dict[str, object]], lock.get("skills", {}))
    record = skills.get(SKILL_SLUG, {})
    if (
        digest != SKILL_HASH
        or record.get("version") != SKILL_VERSION
        or record.get("pinned") is not True
        or "pinned" not in listing
    ):
        return [
            fail_result(
                "real Skill",
                "clawhub_skill_identity_invalid",
                "Installed Skill version, pin, or content hash differs from the approved baseline.",
                "Reinstall and pin the approved version after explicit inspection.",
            )
        ]
    try:
        weather = command(
            "curl", "-fsS", "--max-time", "30", "https://wttr.in/Sydney?format=3", timeout=40
        )
        if "Sydney" not in weather:
            raise ValueError("wttr response did not identify Sydney")
        service = "wttr.in"
    except (ValueError, OSError, subprocess.SubprocessError):
        try:
            fallback = command(
                "curl",
                "-fsS",
                "--max-time",
                "30",
                "https://api.open-meteo.com/v1/forecast?latitude=-33.8688&longitude=151.2093&current_weather=true",
                timeout=40,
            )
            payload = json.loads(fallback)
            if "current_weather" not in payload:
                raise ValueError("Open-Meteo response omitted current weather")
            service = "Open-Meteo"
        except (ValueError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
            return [
                fail_result(
                    "real Skill",
                    "real_skill_network_failed",
                    f"Both Skill-documented weather services failed ({type(exc).__name__}).",
                    "Restore outbound HTTPS access and rerun the real Sydney weather query.",
                )
            ]
    return [
        pass_result("ClawHub", f"Node {node}, pnpm {pnpm}, {cli}"),
        pass_result(
            "real Skill",
            f"{SKILL_SLUG}@{SKILL_VERSION} pinned, hash verified, Sydney query via {service}",
        ),
    ]


def check_frontend() -> list[CheckResult]:
    try:
        command("pnpm", "install", "--frozen-lockfile", timeout=300)
        command("pnpm", "--dir", "apps/web", "check", timeout=300)
        command("pnpm", "build", timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            fail_result(
                "frontend",
                "frontend_check_failed",
                f"Frontend install, typecheck, test, or build failed ({type(exc).__name__}).",
                "Repair the locked frontend toolchain and rerun pnpm check/build.",
            )
        ]
    try:
        chromium = command("pnpm", "--dir", "apps/web", "check:chromium", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            pass_result("frontend", "frozen install, TypeScript, Vitest, and Vite build passed"),
            fail_result(
                "Chromium",
                "chromium_launch_failed",
                f"Playwright Chromium failed to launch ({type(exc).__name__}).",
                "Install the locked Playwright Chromium build and its system dependencies.",
            ),
        ]
    return [
        pass_result("frontend", "frozen install, TypeScript, Vitest, and Vite build passed"),
        pass_result("Chromium", chromium),
    ]


def check_ci_files() -> CheckResult:
    paths = (
        REPOSITORY / ".github/workflows/ci.yml",
        REPOSITORY / ".github/workflows/real-readiness.yml",
    )
    if not all(path.is_file() for path in paths):
        return fail_result(
            "CI files",
            "ci_files_missing",
            "Baseline or trusted readiness workflow is missing.",
            "Add both version-independent GitHub Actions workflows.",
        )
    return pass_result("CI files", "baseline and trusted readiness workflows exist")


def run_guarded[T](
    name: str, check: Callable[[], T], adapter: Callable[[T], list[CheckResult]]
) -> list[CheckResult]:
    try:
        return adapter(check())
    except Exception as exc:
        return [
            fail_result(
                name,
                f"{name.lower().replace(' ', '_')}_unexpected",
                f"Unexpected readiness failure ({type(exc).__name__}).",
                "Inspect the named subsystem without exposing Secret values.",
            )
        ]


def one(value: CheckResult) -> list[CheckResult]:
    return [value]


def many(value: list[CheckResult]) -> list[CheckResult]:
    return value


def main() -> int:
    os.chdir(REPOSITORY)
    results: list[CheckResult] = []
    results += run_guarded("repository", check_repository, one)
    results += run_guarded("Miniforge", check_miniforge, one)
    results += run_guarded("Workspace", check_workspace, one)
    results += run_guarded("PostgreSQL", check_postgresql, one)
    results += run_guarded("real model", model_results, many)
    results += run_guarded("ClawHub", check_clawhub, many)
    results += run_guarded("frontend", check_frontend, many)
    results += run_guarded("CI files", check_ci_files, one)

    for result in results:
        suffix = f" [{result.code}]" if result.code else ""
        print(f"{result.name}: {result.status}{suffix} - {result.detail}")
        if result.remediation:
            print(f"  remediation: {result.remediation}")

    if any(result.status == "FAIL" for result in results):
        return 1
    if any(result.status == "BLOCKED" for result in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
