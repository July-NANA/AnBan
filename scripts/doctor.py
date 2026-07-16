"""Offline, fail-closed diagnostics for the local Anban development environment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace

REPOSITORY = Path(__file__).resolve().parents[1]
CLAW_CLI = "clawhub@0.23.1"
CLAW_CLI_VERSION = "0.23.1"
SKILL_SLUG = "@steipete/weather"
SKILL_VERSION = "1.0.0"
SKILL_HASH = "1ca0c8d768ad603ea8d5d47f56a9b435fe575f7f34e719eda85c82003d740e93"
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


def environment_contract_valid(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    name_valid = re.search(r"(?m)^name:\s*anban\s*(?:#.*)?$", content) is not None
    python_valid = re.search(r"(?m)^\s*-\s*python\s*=\s*3\.12\s*(?:#.*)?$", content) is not None
    return name_valid and python_valid


def python_environment_result(
    environ: Mapping[str, str],
    version: tuple[int, int],
    executable: Path,
    uv_version: str | None,
    environment_file: Path,
) -> CheckResult:
    if environ.get("CONDA_DEFAULT_ENV") != "anban":
        return fail_result(
            "Python",
            "conda_environment_invalid",
            "The active Conda environment is not anban.",
            "Activate the Miniforge anban environment.",
        )
    if version != (3, 12):
        return fail_result(
            "Python",
            "python_version_invalid",
            "Python is not version 3.12.",
            "Recreate the anban environment from environment.yml.",
        )
    prefix_value = environ.get("CONDA_PREFIX", "").strip()
    if not prefix_value:
        return fail_result(
            "Python",
            "conda_prefix_missing",
            "CONDA_PREFIX is not available.",
            "Activate the Miniforge anban environment.",
        )
    prefix = Path(prefix_value).expanduser().resolve()
    resolved_executable = executable.expanduser().resolve()
    if prefix not in resolved_executable.parents:
        return fail_result(
            "Python",
            "conda_interpreter_invalid",
            "Python does not come from the active CONDA_PREFIX.",
            "Run doctor with Python from the active anban environment.",
        )
    if not environment_contract_valid(environment_file):
        return fail_result(
            "Python",
            "environment_file_invalid",
            "environment.yml does not declare anban with Python 3.12.",
            "Restore the approved environment.yml contract.",
        )
    if uv_version is None:
        return fail_result(
            "Python",
            "uv_unavailable",
            "uv is not executable in the active environment.",
            "Install uv through the approved Miniforge environment.",
        )
    return pass_result(
        "Python",
        f"anban, Python {version[0]}.{version[1]}, interpreter inside CONDA_PREFIX, {uv_version}",
    )


def check_python() -> CheckResult:
    try:
        uv_version = command("uv", "--version")
    except (OSError, subprocess.SubprocessError):
        uv_version = None
    return python_environment_result(
        os.environ,
        (sys.version_info.major, sys.version_info.minor),
        Path(sys.executable),
        uv_version,
        REPOSITORY / "environment.yml",
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


def load_workspace_config(workspace: Path) -> dict[str, object]:
    with (workspace / "anban.toml").open("rb") as handle:
        return tomllib.load(handle)


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
    try:
        config = load_workspace_config(workspace)
    except (OSError, tomllib.TOMLDecodeError):
        return fail_result(
            "Workspace",
            "workspace_configuration_invalid",
            "anban.toml cannot be parsed.",
            "Restore a valid Workspace configuration.",
        )
    schema_version = config.get("schema_version")
    workspace_id = config.get("workspace_id")
    if not isinstance(schema_version, int) or schema_version < 1:
        return fail_result(
            "Workspace",
            "workspace_schema_version_invalid",
            "anban.toml schema_version is invalid.",
            "Set schema_version to a supported positive integer.",
        )
    if not isinstance(workspace_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{2,63}", workspace_id
    ):
        return fail_result(
            "Workspace",
            "workspace_id_invalid",
            "anban.toml workspace_id is invalid.",
            "Set a stable lowercase workspace identifier.",
        )
    source = resolution.source if resolution is not None else "explicit validation path"
    return pass_result(
        "Workspace", f"resolved from {source}; external, permissions and configuration valid"
    )


def load_configuration_presence(workspace: Path, environ: Mapping[str, str]) -> dict[str, bool]:
    values = dotenv_values(workspace / "secrets.env", interpolate=False)
    return {
        name: bool(environ.get(name) or (isinstance(values.get(name), str) and values.get(name)))
        for name in CONFIGURATION_KEYS
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


def check_configuration(workspace: Path) -> list[CheckResult]:
    return configuration_results(load_configuration_presence(workspace, os.environ))


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


def check_postgresql(workspace: Path) -> CheckResult:
    values = dotenv_values(workspace / "secrets.env", interpolate=False)
    development = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
    test = os.environ.get("ANBAN_TEST_DATABASE_URL") or values.get("ANBAN_TEST_DATABASE_URL")
    if not isinstance(development, str) or not development or not isinstance(test, str) or not test:
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


def main() -> int:
    os.chdir(REPOSITORY)
    try:
        workspace = resolve_workspace(repository=REPOSITORY).path
    except WorkspaceResolutionError:
        workspace = REPOSITORY
    results: list[CheckResult] = []
    results += run_guarded("repository", check_repository, one)
    results += run_guarded("Python", check_python, one)
    results += run_guarded("Node", check_node, one)
    results += run_guarded("Workspace", check_workspace, one)
    results += run_guarded("configuration", lambda: check_configuration(workspace), many)
    results += run_guarded("PostgreSQL", lambda: check_postgresql(workspace), one)
    results += run_guarded("Skill baseline", lambda: check_skill_baseline(workspace), one)
    results += run_guarded("Chromium", check_chromium, one)

    for result in results:
        for line in result_lines(result):
            print(line)

    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
