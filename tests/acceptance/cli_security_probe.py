"""Explicit local security probe; the hanging Provider exists only under tests/."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select

from anban.core.errors import ErrorCode
from anban.core.ids import ExecutionRunId
from anban.persistence import DatabaseProfile, create_database_engine, database_url
from anban.persistence.models import ExecutionRunRecord, TaskRecord
from anban.runtime import RunDetail
from scripts.acceptance.check_cli_e2e import (
    CliE2EError,
    CliFailure,
    CliResult,
    assert_safe_output,
    evidence_facts,
    parse_model,
    run_cli,
)
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace

CANARY_SECRET = "anban-security-probe-canary-value"
MAX_OUTPUT_BYTES = 65_536


class SecurityProbeError(RuntimeError):
    """Safe failure without Provider, database, process, or host details."""


async def snapshot_runs() -> dict[UUID, UUID]:
    engine = create_database_engine(DatabaseProfile.TEST)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                select(ExecutionRunRecord.id, ExecutionRunRecord.task_id)
            )
            return {run_id: task_id for run_id, task_id in rows.tuples()}
    finally:
        await engine.dispose()


async def cleanup_new_runs(workspace: Path, before: Mapping[UUID, UUID]) -> None:
    after = await snapshot_runs()
    new_runs = set(after).difference(before)
    task_ids = {after[run_id] for run_id in new_runs}
    if task_ids:
        engine = create_database_engine(DatabaseProfile.TEST)
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id.in_(task_ids)))
        finally:
            await engine.dispose()
    for run_id in new_runs:
        for parent in (workspace / "runs", workspace / "artifacts"):
            target = parent / str(run_id)
            if target.is_relative_to(workspace) and target.name == str(run_id):
                shutil.rmtree(target, ignore_errors=True)


async def accept_missing_configuration(executable: str, base_environment: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="anban-security-") as temporary:
        workspace = Path(temporary)
        real_workspace = resolve_workspace().path
        shutil.copyfile(real_workspace / "anban.toml", workspace / "anban.toml")
        environment = dict(base_environment)
        environment["ANBAN_WORKSPACE_DIR"] = str(workspace)
        for key in (
            "OPENAI_COMPATIBLE_BASE_URL",
            "OPENAI_COMPATIBLE_API_KEY",
            "OPENAI_COMPATIBLE_MODEL",
        ):
            environment.pop(key, None)
        return_code, stdout, stderr = await run_cli(
            executable,
            ("run", "Fail before execution when model configuration is missing.", "--json"),
            environment,
        )
        failure = parse_model(CliFailure, stdout or stderr)
        if return_code == 0 or failure.error.code != ErrorCode.CONFIGURATION_MISSING.value:
            raise SecurityProbeError("missing model configuration did not fail explicitly")
        assert_safe_output(stdout + stderr, workspace, (CANARY_SECRET,))


async def accept_interruption(
    executable: str,
    base_environment: dict[str, str],
    workspace: Path,
) -> None:
    before = await snapshot_runs()
    connected = asyncio.Event()
    release = asyncio.Event()

    async def hang_provider(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connected.set()
        try:
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(hang_provider, "127.0.0.1", 0)
    sockets = server.sockets or ()
    if len(sockets) != 1:
        server.close()
        await server.wait_closed()
        raise SecurityProbeError("interruption endpoint did not bind")
    port = sockets[0].getsockname()[1]
    if not isinstance(port, int):
        raise SecurityProbeError("interruption endpoint is invalid")
    environment = dict(base_environment)
    environment.update(
        {
            "ANBAN_WORKSPACE_DIR": str(workspace),
            "DATABASE_URL": database_url(DatabaseProfile.TEST, workspace=workspace),
            "OPENAI_COMPATIBLE_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "OPENAI_COMPATIBLE_API_KEY": CANARY_SECRET,
            "OPENAI_COMPATIBLE_MODEL": "interruption-probe",
        }
    )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "run",
            "Wait for the bounded model response.",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        await asyncio.wait_for(connected.wait(), timeout=15)
        process.send_signal(signal.SIGINT)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=20)
        if len(stdout_bytes) > MAX_OUTPUT_BYTES or len(stderr_bytes) > MAX_OUTPUT_BYTES:
            raise SecurityProbeError("interruption output exceeded its bound")
        stdout = stdout_bytes.decode("utf-8")
        stderr = stderr_bytes.decode("utf-8")
        if process.returncode != 130:
            raise SecurityProbeError("interrupted CLI execution did not persist cancellation")
        try:
            result = parse_model(CliResult, stdout or stderr)
        except CliE2EError:
            failure = parse_model(CliFailure, stdout or stderr)
            if failure.error.code != ErrorCode.EXECUTION_INTERRUPTED.value:
                raise SecurityProbeError(
                    "interrupted CLI failure classification is invalid"
                ) from None
            after = await snapshot_runs()
            new_runs = set(after).difference(before)
            if len(new_runs) != 1:
                raise SecurityProbeError("interrupted CLI Run identity is ambiguous") from None
            run_id = ExecutionRunId(next(iter(new_runs)))
        else:
            if (
                result.status != "cancelled"
                or not result.persisted
                or result.error is None
                or result.error.code is not ErrorCode.EXECUTION_INTERRUPTED
            ):
                raise SecurityProbeError("interrupted CLI result is invalid")
            run_id = ExecutionRunId(result.run_id)
        show_code, show_stdout, show_stderr = await run_cli(
            executable,
            ("run", "show", str(run_id), "--json"),
            environment,
        )
        detail = parse_model(RunDetail, show_stdout or show_stderr)
        event_types = {event.event_type for event in detail.observability.trace}
        if (
            show_code != 0
            or detail.run.status.value != "cancelled"
            or detail.run.error_code is not ErrorCode.EXECUTION_INTERRUPTED
            or not detail.observability.complete
            or not {"model.requested", "run.cancelled", "run.error"} <= event_types
        ):
            raise SecurityProbeError("interrupted Run is not restart-safe")
        assert_safe_output(
            stdout + stderr + show_stdout + show_stderr,
            workspace,
            (CANARY_SECRET, environment["DATABASE_URL"]),
        )
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await cleanup_new_runs(workspace, before)


async def accept_security() -> None:
    executable = shutil.which("anban")
    if executable is None:
        raise SecurityProbeError("installed anban console script is unavailable")
    workspace = resolve_workspace().path
    environment = dict(os.environ)
    await accept_missing_configuration(executable, environment)
    await accept_interruption(executable, environment, workspace)


def main() -> int:
    try:
        asyncio.run(accept_security())
        facts = evidence_facts()
    except WorkspaceResolutionError as exc:
        print(f"security acceptance: FAIL [{exc.code}]", file=sys.stderr)
        return 1
    except SecurityProbeError:
        print("security acceptance: FAIL [acceptance_invalid]", file=sys.stderr)
        return 1
    except CliE2EError:
        print("security acceptance: FAIL [output_invalid]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"security acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "security acceptance: PASS - missing configuration, bounded interruption, "
        "durable cancellation, Canary-safe CLI/Audit/Trace"
    )
    print(f"security acceptance evidence: {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
