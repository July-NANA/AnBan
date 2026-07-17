"""Offline, fail-closed diagnostics for the local Anban development environment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from anban.capability.skill import WEATHER_SKILL
from anban.config.loader import AnbanConfiguration, load_configuration
from anban.core import AnbanError
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace

REPOSITORY = Path(__file__).resolve().parents[1]
CLAW_CLI = "clawhub@0.23.1"
CLAW_CLI_VERSION = "0.23.1"
SKILL_SLUG = WEATHER_SKILL.slug
SKILL_VERSION = WEATHER_SKILL.version
SKILL_HASH = WEATHER_SKILL.sha256
CONFIGURATION_KEYS = (
    "DATABASE_URL",
    "ANBAN_TEST_DATABASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_MODEL",
)
Status = Literal["PASS", "FAIL"]


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


def pass_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "PASS", detail)


def fail_result(name: str, code: str, detail: str, remediation: str) -> CheckResult:
    return CheckResult(name, "FAIL", detail, code, remediation)


def check_repository() -> CheckResult:
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
            "Required repository baseline files are missing: " + ", ".join(missing),
            "Restore the required development baseline files.",
        )
    return pass_result("repository", "required development baseline files exist")


def python_environment_result(
    version: tuple[int, int],
    tool_versions: Mapping[str, str | None],
    *,
    dependencies_available: bool,
    package_source_valid: bool,
) -> CheckResult:
    if version != (3, 12):
        return fail_result(
            "Python",
            "python_version_invalid",
            "Python is not version 3.12.",
            "Run Anban from a Python 3.12 environment.",
        )
    if not dependencies_available:
        return fail_result(
            "Python",
            "python_dependency_unavailable",
            "One or more project dependencies cannot be imported.",
            "Install the locked project dependencies into the current Python environment.",
        )
    unavailable = [name for name, value in tool_versions.items() if value is None]
    if unavailable:
        return fail_result(
            "Python",
            "python_tool_unavailable",
            "Current Python cannot execute: " + ", ".join(sorted(unavailable)) + ".",
            "Install the locked development dependencies into the current Python environment.",
        )
    if not package_source_valid:
        return fail_result(
            "Python",
            "anban_package_mismatch",
            "The installed anban package does not correspond to this checkout.",
            "Install this checkout into the current Python environment in editable mode.",
        )
    tools = ", ".join(f"{name}={value}" for name, value in sorted(tool_versions.items()))
    return pass_result(
        "Python",
        f"Python {version[0]}.{version[1]}, current interpreter, dependencies importable, {tools}",
    )


def check_python() -> CheckResult:
    tools: dict[str, str | None] = {}
    for name in ("ruff", "pytest", "pyright"):
        try:
            tools[name] = command(sys.executable, "-m", name, "--version").splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            tools[name] = None
    dependencies_available = True
    for name in (
        "alembic",
        "asyncpg",
        "dotenv",
        "fastapi",
        "httpx",
        "langgraph",
        "openai",
        "pydantic",
        "sqlalchemy",
    ):
        try:
            importlib.import_module(name)
        except Exception:
            dependencies_available = False
            break
    try:
        import anban

        package_source_valid = package_version("anban") == "0.1.0" and Path(
            anban.__file__
        ).resolve().is_relative_to(REPOSITORY.resolve())
    except (ImportError, PackageNotFoundError, OSError):
        package_source_valid = False
    return python_environment_result(
        (sys.version_info.major, sys.version_info.minor),
        tools,
        dependencies_available=dependencies_available,
        package_source_valid=package_source_valid,
    )


def node_environment_result(
    node_version: str | None, pnpm_version: str | None, package_manager: str | None
) -> CheckResult:
    if not node_version or not pnpm_version:
        return fail_result(
            "Node",
            "node_toolchain_unavailable",
            "Node or pnpm is not executable.",
            "Install the repository-compatible Node and pnpm toolchain.",
        )
    if not package_manager or not package_manager.startswith("pnpm@"):
        return fail_result(
            "Node",
            "package_manager_invalid",
            "package.json does not declare a pnpm packageManager.",
            "Restore the repository packageManager declaration.",
        )
    required_version = package_manager.partition("@")[2]
    if pnpm_version != required_version:
        return fail_result(
            "Node",
            "pnpm_version_incompatible",
            "The active pnpm version does not match packageManager.",
            f"Activate pnpm {required_version}.",
        )
    return pass_result("Node", f"Node {node_version}, pnpm {pnpm_version}")


def check_node() -> CheckResult:
    try:
        node_version = command("node", "--version")
        pnpm_version = command("pnpm", "--version")
    except (OSError, subprocess.SubprocessError):
        node_version = None
        pnpm_version = None
    try:
        package = cast(
            dict[str, object], json.loads((REPOSITORY / "package.json").read_text(encoding="utf-8"))
        )
        package_manager_value = package.get("packageManager")
        package_manager = package_manager_value if isinstance(package_manager_value, str) else None
    except (OSError, json.JSONDecodeError):
        package_manager = None
    return node_environment_result(node_version, pnpm_version, package_manager)


def check_workspace(workspace: Path | None = None) -> CheckResult:
    resolution = None
    try:
        if workspace is None:
            resolution = resolve_workspace(repository=REPOSITORY)
            workspace = resolution.path
        else:
            workspace = workspace.resolve()
    except WorkspaceResolutionError as exc:
        return fail_result(
            "Workspace", exc.code, str(exc), "Set a valid absolute external Workspace path."
        )
    repository = REPOSITORY.resolve()
    home = Path.home().resolve()
    if (
        workspace == Path(workspace.anchor)
        or workspace in {home, repository}
        or repository in workspace.parents
    ):
        return fail_result(
            "Workspace",
            "workspace_path_unsafe",
            "Workspace must not be a filesystem root, HOME, or inside the repository.",
            "Choose a dedicated external Workspace directory.",
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
    source = resolution.source if resolution is not None else "explicit validation path"
    return pass_result(
        "Workspace", f"resolved from {source}; external layout and permissions valid"
    )


def configuration_presence(configuration: AnbanConfiguration) -> dict[str, bool]:
    model_configured = configuration.model is not None
    return {
        "DATABASE_URL": configuration.database.development_url is not None,
        "ANBAN_TEST_DATABASE_URL": configuration.database.test_url is not None,
        "OPENAI_COMPATIBLE_BASE_URL": model_configured,
        "OPENAI_COMPATIBLE_API_KEY": model_configured,
        "OPENAI_COMPATIBLE_MODEL": model_configured,
    }


def configuration_results(presence: Mapping[str, bool]) -> list[CheckResult]:
    return [
        (
            pass_result(f"configuration {name}", "configured")
            if presence.get(name) is True
            else fail_result(
                f"configuration {name}",
                "configuration_missing",
                "missing",
                f"Configure {name} in Workspace secrets.env or the process environment.",
            )
        )
        for name in CONFIGURATION_KEYS
    ]


def check_configuration(configuration: AnbanConfiguration) -> list[CheckResult]:
    results = configuration_results(configuration_presence(configuration))
    model = configuration.model
    if model is None:
        return results
    results.append(
        pass_result(
            "effective configuration",
            "model timeout="
            f"{model.request_timeout_seconds}s, transport retries={model.transport_retries}, "
            f"response repairs={model.response_repair_retries}, model turns="
            f"{configuration.agent.max_model_turns}, capability calls="
            f"{configuration.agent.max_capability_calls}, total timeout="
            f"{configuration.agent.total_timeout_seconds}s, process timeout="
            f"{configuration.process.default_timeout_seconds}s",
        )
    )
    return results


async def database_probe(url: str, expected_database: str) -> None:
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(text("SELECT current_database(), current_user"))
            ).one()
            if identity[0] != expected_database or identity[1] != "anban":
                raise RuntimeError("database identity mismatch")
            await connection.execute(
                text(
                    "SELECT current_schema(), count(*) "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "GROUP BY current_schema()"
                )
            )
            await connection.rollback()
            transaction = await connection.begin()
            try:
                await connection.execute(text("CREATE TEMP TABLE anban_doctor_probe(value text)"))
                await connection.execute(text("INSERT INTO anban_doctor_probe VALUES ('ok')"))
                value = (
                    await connection.execute(text("SELECT value FROM anban_doctor_probe"))
                ).scalar_one()
                if value != "ok":
                    raise RuntimeError("database write probe mismatch")
            finally:
                await transaction.rollback()
            probe_after_rollback = (
                await connection.execute(text("SELECT to_regclass('pg_temp.anban_doctor_probe')"))
            ).scalar_one()
            if probe_after_rollback is not None:
                raise RuntimeError("database transaction rollback failed")
    finally:
        await engine.dispose()


def check_postgresql(configuration: AnbanConfiguration) -> CheckResult:
    try:
        development = configuration.database.require("development")
        test = configuration.database.require("test")
    except AnbanError:
        return fail_result(
            "PostgreSQL",
            "postgresql_configuration_missing",
            "Database URL configuration is missing.",
            "Configure both database URLs without exposing their values.",
        )
    try:
        asyncio.run(database_probe(development, "anban"))
        asyncio.run(database_probe(test, "anban_test"))
    except Exception as exc:
        return fail_result(
            "PostgreSQL",
            "postgresql_probe_failed",
            f"A database probe failed ({type(exc).__name__}).",
            "Start both databases and verify identity, schema readability, and transaction rights.",
        )
    return pass_result(
        "PostgreSQL", "development and test databases connected; schema readable; rollback verified"
    )


def skill_baseline_result(workspace: Path, cli_version: str | None) -> CheckResult:
    skill_file = workspace / "skills" / "@steipete" / "weather" / "SKILL.md"
    lock_file = workspace / ".clawhub" / "lock.json"
    if cli_version != CLAW_CLI_VERSION:
        return fail_result(
            "Skill baseline",
            "clawhub_cli_unavailable",
            "The pinned ClawHub CLI is not locally callable through npx in offline mode.",
            f"Make {CLAW_CLI} available in the local npm cache.",
        )
    if not skill_file.is_file() or not lock_file.is_file():
        return fail_result(
            "Skill baseline",
            "skill_baseline_missing",
            "The approved Skill files or ClawHub lock are missing.",
            "Restore the approved local Skill and its source record.",
        )
    try:
        digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        lock = cast(dict[str, object], json.loads(lock_file.read_text(encoding="utf-8")))
        skills = cast(dict[str, dict[str, object]], lock.get("skills", {}))
        record = skills.get(SKILL_SLUG, {})
    except (OSError, json.JSONDecodeError, TypeError):
        return fail_result(
            "Skill baseline",
            "skill_source_record_invalid",
            "The approved Skill source record cannot be read.",
            "Restore a valid ClawHub lock for the approved Skill.",
        )
    if (
        digest != SKILL_HASH
        or record.get("version") != SKILL_VERSION
        or record.get("pinned") is not True
    ):
        return fail_result(
            "Skill baseline",
            "skill_baseline_mismatch",
            "The approved Skill version, pin, or content hash does not match.",
            "Review and restore the approved local Skill baseline.",
        )
    return pass_result(
        "Skill baseline", f"{SKILL_SLUG}@{SKILL_VERSION} pinned with approved content hash"
    )


def check_skill_baseline(workspace: Path) -> CheckResult:
    try:
        cli_version = command("npx", "--offline", "--yes", CLAW_CLI, "--cli-version", timeout=30)
    except (OSError, subprocess.SubprocessError):
        cli_version = None
    return skill_baseline_result(workspace, cli_version)


def check_chromium() -> CheckResult:
    try:
        version = command("pnpm", "--dir", "apps/web", "run", "check:chromium", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return fail_result(
            "Chromium",
            "chromium_launch_failed",
            f"Playwright Chromium failed to launch ({type(exc).__name__}).",
            "Install the locked Playwright Chromium build and its system dependencies.",
        )
    return pass_result("Chromium", version.splitlines()[-1])


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
                f"Unexpected doctor failure ({type(exc).__name__}).",
                "Inspect the named subsystem without exposing Secret values.",
            )
        ]


def one(value: CheckResult) -> list[CheckResult]:
    return [value]


def many(value: list[CheckResult]) -> list[CheckResult]:
    return value


def result_lines(result: CheckResult) -> list[str]:
    suffix = f" [{result.code}]" if result.code else ""
    lines = [f"{result.name}: {result.status}{suffix} - {result.detail}"]
    if result.remediation:
        lines.append(f"  remediation: {result.remediation}")
    return lines


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m scripts.doctor")
    result.add_argument(
        "--toolchain-only",
        action="store_true",
        help="check only repository, Python, and Node toolchains",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    os.chdir(REPOSITORY)
    try:
        workspace = resolve_workspace(repository=REPOSITORY).path
    except WorkspaceResolutionError:
        workspace = REPOSITORY
    results: list[CheckResult] = []
    results += run_guarded("repository", check_repository, one)
    results += run_guarded("Python", check_python, one)
    results += run_guarded("Node", check_node, one)
    if arguments.toolchain_only:
        for result in results:
            for line in result_lines(result):
                print(line)
        return 1 if any(result.status == "FAIL" for result in results) else 0
    results += run_guarded("Workspace", check_workspace, one)
    try:
        configuration = load_configuration(workspace=workspace)
    except AnbanError:
        results.append(
            fail_result(
                "configuration",
                "workspace_configuration_invalid",
                "Workspace configuration is invalid.",
                "Correct anban.toml and its fixed environment references.",
            )
        )
        configuration = None
    if configuration is not None:
        results += run_guarded("configuration", lambda: check_configuration(configuration), many)
        results += run_guarded("PostgreSQL", lambda: check_postgresql(configuration), one)
    results += run_guarded("Skill baseline", lambda: check_skill_baseline(workspace), one)
    results += run_guarded("Chromium", check_chromium, one)

    for result in results:
        for line in result_lines(result):
            print(line)

    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
