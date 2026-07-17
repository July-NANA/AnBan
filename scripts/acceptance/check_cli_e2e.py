"""Real installed-CLI acceptance with restart-safe PostgreSQL inspection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import delete

from anban.capability.skill import WEATHER_SKILL
from anban.core.errors import ErrorInfo
from anban.core.ids import ExecutionRunId, TaskId
from anban.model.config import load_model_configuration
from anban.persistence import (
    DatabaseProfile,
    SQLAlchemyUnitOfWorkFactory,
    create_database_engine,
    database_url,
)
from anban.persistence.models import TaskRecord
from anban.runtime import ArtifactDetail, RunDetail, RunSummary, TraceEntry
from scripts.workspace_bootstrap import REPOSITORY, WorkspaceResolutionError, resolve_workspace

MAX_OUTPUT_BYTES = 65_536
PROCESS_TIMEOUT_SECONDS = 210
FORBIDDEN_OUTPUT_MARKERS = (
    "authorization:",
    "bearer ",
    "file://",
    "postgresql://",
    "postgresql+asyncpg://",
    "provider_response",
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class CliE2EError(RuntimeError):
    """Safe acceptance failure without requests, credentials, or physical paths."""


class CliResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str
    run_id: ExecutionRunId
    persisted: bool
    error: ErrorInfo | None = None


class CliFailureError(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    code: str


class CliFailure(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str
    error: CliFailureError
    run_id: ExecutionRunId | None = None


class CliTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: ExecutionRunId
    complete: bool
    inconsistencies: tuple[str, ...]
    trace: tuple[TraceEntry, ...]


ARTIFACT_LIST: TypeAdapter[tuple[ArtifactDetail, ...]] = TypeAdapter(tuple[ArtifactDetail, ...])
RUN_LIST: TypeAdapter[tuple[RunSummary, ...]] = TypeAdapter(tuple[RunSummary, ...])


async def run_cli(
    executable: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=PROCESS_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise CliE2EError("CLI acceptance process exceeded its bound") from None
    if len(stdout_bytes) > MAX_OUTPUT_BYTES or len(stderr_bytes) > MAX_OUTPUT_BYTES:
        raise CliE2EError("CLI acceptance output exceeded its bound")
    try:
        return (
            process.returncode or 0,
            stdout_bytes.decode("utf-8"),
            stderr_bytes.decode("utf-8"),
        )
    except UnicodeDecodeError:
        raise CliE2EError("CLI acceptance output is not UTF-8") from None


def parse_model[ModelT: BaseModel](model: type[ModelT], value: str) -> ModelT:
    try:
        return model.model_validate_json(value)
    except ValidationError:
        raise CliE2EError("CLI acceptance output is not valid JSON") from None


def parse_adapter[ValueT](adapter: TypeAdapter[ValueT], value: str) -> ValueT:
    try:
        return adapter.validate_json(value)
    except ValidationError:
        raise CliE2EError("CLI acceptance output is not valid JSON") from None


def assert_safe_output(output: str, workspace: Path, protected_values: Sequence[str]) -> None:
    lowered = output.lower()
    if str(workspace) in output or any(marker in lowered for marker in FORBIDDEN_OUTPUT_MARKERS):
        raise CliE2EError("CLI acceptance output contains a protected value")
    if any(value and value in output for value in protected_values):
        raise CliE2EError("CLI acceptance output contains a protected value")


async def expect_success(
    executable: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> str:
    return_code, stdout, stderr = await run_cli(executable, arguments, environment)
    if return_code != 0 or stderr or not stdout:
        raise CliE2EError("CLI command did not succeed cleanly")
    return stdout


async def expect_failure(
    executable: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    expected_code: str,
) -> CliFailure:
    return_code, stdout, stderr = await run_cli(executable, arguments, environment)
    if return_code == 0:
        raise CliE2EError("CLI failure probe unexpectedly succeeded")
    payload = parse_model(CliFailure, stdout or stderr)
    if payload.error.code != expected_code:
        raise CliE2EError("CLI failure classification mismatch")
    return payload


async def accept_vertical_slice(
    executable: str,
    environment: Mapping[str, str],
    workspace: Path,
    run_ids: list[ExecutionRunId],
) -> None:
    result_output = await expect_success(
        executable,
        (
            "run",
            "First call skill.activate for @steipete/weather. After its Tool Result, call "
            "file.write exactly once with path acceptance/cli-e2e.txt and content "
            "cli-e2e-accepted. After both Tool Results, return one short final sentence.",
            "--json",
        ),
        environment,
    )
    result = parse_model(CliResult, result_output)
    if result.status != "succeeded" or not result.persisted:
        raise CliE2EError("real CLI execution did not persist a successful result")
    run_id = result.run_id
    run_ids.append(run_id)

    run_argument = str(run_id)
    show_output, trace_output, artifacts_output, runs_output = await asyncio.gather(
        expect_success(executable, ("run", "show", run_argument, "--json"), environment),
        expect_success(executable, ("trace", run_argument, "--json"), environment),
        expect_success(executable, ("artifacts", run_argument, "--json"), environment),
        expect_success(executable, ("runs", "--limit", "100", "--json"), environment),
    )
    combined = result_output + show_output + trace_output + artifacts_output + runs_output
    assert_safe_output(
        combined,
        workspace,
        (environment.get("OPENAI_COMPATIBLE_API_KEY", ""),),
    )

    show = parse_model(RunDetail, show_output)
    trace = parse_model(CliTrace, trace_output)
    artifacts = parse_adapter(ARTIFACT_LIST, artifacts_output)
    runs = parse_adapter(RUN_LIST, runs_output)
    if (
        show.run.id != run_id
        or show.run.status.value != "succeeded"
        or len(show.nodes) != 1
        or show.nodes[0].status.value != "succeeded"
        or [item.capability_name for item in show.invocations] != ["skill.activate", "file.write"]
        or any(item.status.value != "succeeded" for item in show.invocations)
        or not show.observability.complete
        or not trace.complete
        or trace.inconsistencies
        or len(artifacts) != 1
        or not any(item.id == run_id for item in runs)
    ):
        raise CliE2EError("restart-safe CLI projections are incomplete")

    sequences = [entry.sequence for entry in trace.trace]
    event_types = {entry.event_type for entry in trace.trace}
    skill_events = [entry for entry in trace.trace if entry.event_type == "skill.activated"]
    skill_metadata = skill_events[0].metadata.root if len(skill_events) == 1 else None
    artifact = artifacts[0]
    if (
        sequences != list(range(1, len(sequences) + 1))
        or not {
            "model.requested",
            "model.completed",
            "skill.activated",
            "capability.completed",
            "artifact.created",
            "run.final",
        }
        <= event_types
        or skill_metadata is None
        or skill_metadata.get("skill_version") != WEATHER_SKILL.version
        or skill_metadata.get("content_hash") != WEATHER_SKILL.sha256
        or skill_metadata.get("skill_source") != "anban://skill/@steipete/weather@1.0.0"
        or not artifact.uri.startswith("anban://artifact/")
        or artifact.size_bytes != len(b"cli-e2e-accepted")
    ):
        raise CliE2EError("CLI Trace, Skill, or Artifact evidence is incomplete")

    artifact_file = workspace / "artifacts" / run_argument / str(artifact.id)
    try:
        artifact_bytes = artifact_file.read_bytes()
    except OSError:
        raise CliE2EError("CLI Artifact snapshot is unavailable") from None
    if (
        artifact_bytes != b"cli-e2e-accepted"
        or hashlib.sha256(artifact_bytes).hexdigest() != artifact.sha256
    ):
        raise CliE2EError("CLI Artifact snapshot does not match durable metadata")


async def accept_failure_paths(
    executable: str,
    environment: dict[str, str],
    workspace: Path,
    run_ids: list[ExecutionRunId],
) -> None:
    unavailable_model = dict(environment)
    unavailable_model.update(
        {
            "OPENAI_COMPATIBLE_BASE_URL": "http://127.0.0.1:1/v1",
            "OPENAI_COMPATIBLE_API_KEY": "cli-e2e-probe-not-a-secret",
            "OPENAI_COMPATIBLE_MODEL": "unavailable",
        }
    )
    failed_model = await expect_failure(
        executable,
        ("run", "Return one short sentence.", "--json"),
        unavailable_model,
        "model_transport_failed",
    )
    try:
        if failed_model.run_id is None:
            raise ValueError
        failed_run_id = failed_model.run_id
    except ValueError:
        raise CliE2EError("failed model Run was not durably identified") from None
    run_ids.append(failed_run_id)

    with tempfile.TemporaryDirectory(prefix="anban-cli-e2e-") as temporary:
        isolated_workspace = Path(temporary)
        shutil.copyfile(workspace / "anban.toml", isolated_workspace / "anban.toml")
        missing_skill = dict(environment)
        missing_skill["ANBAN_WORKSPACE_DIR"] = str(isolated_workspace)
        missing_skill_failure = await expect_failure(
            executable,
            ("run", "Activate the approved Workspace Skill.", "--json"),
            missing_skill,
            "capability_execution_failed",
        )
        assert_safe_output(
            missing_skill_failure.model_dump_json(),
            isolated_workspace,
            (environment["OPENAI_COMPATIBLE_API_KEY"],),
        )

    unavailable_database = dict(environment)
    unavailable_database["DATABASE_URL"] = (
        "postgresql+asyncpg://acceptance:acceptance@127.0.0.1:1/unavailable"
    )
    database_failure = await expect_failure(
        executable,
        ("runs", "--json"),
        unavailable_database,
        "persistence_unavailable",
    )
    outputs = json.dumps(
        (
            failed_model.model_dump(mode="json"),
            missing_skill_failure.model_dump(mode="json"),
            database_failure.model_dump(mode="json"),
        ),
        separators=(",", ":"),
    )
    assert_safe_output(
        outputs,
        workspace,
        (
            environment["OPENAI_COMPATIBLE_API_KEY"],
            unavailable_model["OPENAI_COMPATIBLE_API_KEY"],
        ),
    )


async def cleanup(workspace: Path, run_ids: Sequence[ExecutionRunId]) -> None:
    engine = create_database_engine(DatabaseProfile.TEST)
    try:
        factory = SQLAlchemyUnitOfWorkFactory(engine)
        task_ids: list[TaskId] = []
        for run_id in run_ids:
            async with factory() as unit:
                aggregate = await unit.executions.load_run(run_id)
            if aggregate is not None:
                task_ids.append(aggregate.task.id)
        if task_ids:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id.in_(task_ids)))
    finally:
        await engine.dispose()
    for run_id in run_ids:
        for parent in (workspace / "runs", workspace / "artifacts"):
            target = parent / str(run_id)
            if target.is_relative_to(workspace) and target.name == str(run_id):
                shutil.rmtree(target, ignore_errors=True)


async def accept_cli_e2e(workspace: Path) -> None:
    executable = shutil.which("anban")
    if executable is None:
        raise CliE2EError("installed anban console script is unavailable")
    model = load_model_configuration(workspace=workspace)
    test_database_url = database_url(DatabaseProfile.TEST, workspace=workspace)
    environment = dict(os.environ)
    environment.update(
        {
            "ANBAN_WORKSPACE_DIR": str(workspace),
            "DATABASE_URL": test_database_url,
            "OPENAI_COMPATIBLE_BASE_URL": model.base_url,
            "OPENAI_COMPATIBLE_API_KEY": model.api_key,
            "OPENAI_COMPATIBLE_MODEL": model.model,
        }
    )
    run_ids: list[ExecutionRunId] = []
    try:
        await accept_vertical_slice(executable, environment, workspace, run_ids)
        await accept_failure_paths(executable, environment, workspace, run_ids)
    finally:
        await cleanup(workspace, run_ids)


def evidence_facts() -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    sha = result.stdout.strip()
    environment = os.environ.get("CONDA_DEFAULT_ENV", "")
    if result.returncode != 0 or not SHA_PATTERN.fullmatch(sha) or environment != "anban":
        raise CliE2EError("CLI E2E environment facts are invalid")
    return (
        f"sha={sha} python={platform.python_version()} platform={sys.platform} "
        f"conda={environment} database=test skill={WEATHER_SKILL.slug}@{WEATHER_SKILL.version}"
    )


def main() -> int:
    try:
        workspace = resolve_workspace().path
        asyncio.run(accept_cli_e2e(workspace))
        facts = evidence_facts()
    except WorkspaceResolutionError as exc:
        print(f"CLI E2E acceptance: FAIL [{exc.code}]", file=sys.stderr)
        return 1
    except CliE2EError:
        print("CLI E2E acceptance: FAIL [acceptance_invalid]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"CLI E2E acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "CLI E2E acceptance: PASS - installed CLI, real Model/Skill/Capability, "
        "PostgreSQL, Artifact, Event/Audit/Trace, restart, explicit failures"
    )
    print(f"CLI E2E evidence: {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
