"""P2 Gate: one real Skill + Capability Runtime slice and production failure probes."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import JsonValue
from sqlalchemy import delete

from anban.capability import (
    CapabilityRegistry,
    CapabilityResultStatus,
    InvocationContext,
    local_capability_registry,
    register_workspace_skill,
)
from anban.config import load_configuration
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    ExecutionRunId,
    TaskId,
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from anban.model import ModelMessage, ModelRequest, OpenAICompatibleAdapter
from anban.model.config import load_model_configuration
from anban.persistence import (
    DatabaseProfile,
    SQLAlchemyUnitOfWorkFactory,
    create_database_engine,
    database_url,
)
from anban.persistence.models import TaskRecord
from anban.runtime import AgentOutcomeStatus, EventProjectionService, PersistentRuntime
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace


class P2GateError(RuntimeError):
    """Safe Gate failure without requests, provider data, database values, or host paths."""


def invocation_context(*, seconds: int = 10) -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def next_invocation(context: InvocationContext) -> InvocationContext:
    return context.model_copy(update={"invocation_id": new_capability_invocation_id()})


async def expect_error(
    registry: CapabilityRegistry,
    name: str,
    arguments: dict[str, JsonValue],
    context: InvocationContext,
    code: ErrorCode,
) -> None:
    try:
        await registry.invoke(name, arguments, context)
    except AnbanError as exc:
        if exc.info.code is not code:
            raise P2GateError("Capability failure classification mismatch") from None
    else:
        raise P2GateError("invalid Capability invocation unexpectedly succeeded")


async def accept_model_failures() -> None:
    request = ModelRequest(messages=(ModelMessage(role="user", content="Bounded failure probe."),))
    transport = OpenAICompatibleAdapter(
        AsyncOpenAI(
            api_key="gate-probe-not-a-secret",
            base_url="http://127.0.0.1:1/v1",
            timeout=0.2,
            max_retries=0,
        ),
        "unavailable",
    )
    try:
        try:
            await transport.complete(request)
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.MODEL_TRANSPORT_FAILED:
                raise P2GateError("model transport failure classification mismatch") from None
        else:
            raise P2GateError("unavailable model endpoint unexpectedly succeeded")
    finally:
        await transport.aclose()

    configuration = load_model_configuration()
    timeout = OpenAICompatibleAdapter(
        AsyncOpenAI(
            api_key=configuration.api_key.get_secret_value(),
            base_url=configuration.base_url.get_secret_value(),
            timeout=0.001,
            max_retries=0,
        ),
        configuration.model,
    )
    try:
        try:
            await timeout.complete(request)
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.MODEL_TIMEOUT:
                raise P2GateError("model timeout classification mismatch") from None
        else:
            raise P2GateError("bounded model timeout probe unexpectedly succeeded")
    finally:
        await timeout.aclose()


async def accept_capability_failures(workspace: Path) -> tuple[ExecutionRunId, ...]:
    registry = local_capability_registry(
        workspace_root=workspace,
        allowed_executables={"python": Path(sys.executable)},
        environment={"PYTHONUTF8": "1"},
    )
    context = invocation_context()
    run_ids = [context.run_id]
    await expect_error(registry, "unknown.capability", {}, context, ErrorCode.CAPABILITY_UNKNOWN)
    await expect_error(
        registry,
        "file.write",
        {"path": "missing-content.txt"},
        next_invocation(context),
        ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
    )
    await expect_error(
        registry,
        "file.read",
        {"path": "../outside.txt"},
        next_invocation(context),
        ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
    )

    missing = await registry.invoke(
        "process.execute",
        {"command": "missing"},
        next_invocation(context),
    )
    timed_out = await registry.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import time;time.sleep(5)"],
            "timeout": 1,
        },
        next_invocation(context),
    )
    oversized = await registry.invoke(
        "process.execute",
        {"command": "python", "args": ["-c", "print('x'*20000)"]},
        next_invocation(context),
    )
    if (
        missing.status is not CapabilityResultStatus.FAILED
        or missing.error is None
        or missing.error.code is not ErrorCode.CAPABILITY_UNAVAILABLE
        or timed_out.status is not CapabilityResultStatus.TIMED_OUT
        or timed_out.error is None
        or timed_out.error.code is not ErrorCode.EXECUTION_TIMED_OUT
        or oversized.status is not CapabilityResultStatus.FAILED
        or oversized.observation is not None
    ):
        raise P2GateError("bounded process failure semantics mismatch")

    canary_key = "ANBAN_P2_CANARY"
    canary_value = "p2-process-canary-value"
    previous = os.environ.get(canary_key)
    os.environ[canary_key] = canary_value
    try:
        environment = await registry.invoke(
            "process.execute",
            {
                "command": "python",
                "args": ["-c", f"import os;print(os.getenv('{canary_key}','not-inherited'))"],
            },
            next_invocation(context),
        )
    finally:
        if previous is None:
            os.environ.pop(canary_key, None)
        else:
            os.environ[canary_key] = previous
    if environment.status is not CapabilityResultStatus.COMPLETED or canary_value in str(
        environment.model_dump(mode="json")
    ):
        raise P2GateError("process environment filtering failed")

    symlink_context = invocation_context()
    run_ids.append(symlink_context.run_id)
    await registry.invoke("file.list", {}, symlink_context)
    run_root = workspace / "runs" / str(symlink_context.run_id) / "workspace"
    external = workspace / "p2-external-boundary-probe"
    link = run_root / "external-link"
    external.write_text("outside run boundary", encoding="utf-8")
    link.symlink_to(external)
    try:
        await expect_error(
            registry,
            "file.read",
            {"path": "external-link"},
            next_invocation(symlink_context),
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
        )
    finally:
        link.unlink(missing_ok=True)
        external.unlink(missing_ok=True)

    safe_results = (missing, timed_out, oversized, environment)
    serialized = "".join(result.model_dump_json() for result in safe_results)
    if str(workspace) in serialized or canary_value in serialized:
        raise P2GateError("Capability output exposed a protected value")
    return tuple(run_ids)


async def inspect_gate_run(run_id: ExecutionRunId, workspace: Path) -> None:
    engine = create_database_engine(database_url(DatabaseProfile.TEST))
    try:
        factory = SQLAlchemyUnitOfWorkFactory(engine)
        async with factory() as unit:
            aggregate = await unit.executions.load_run(run_id)
        if aggregate is None:
            raise P2GateError("P2 Run is unavailable after restart")
        observability = await EventProjectionService(factory).inspect(run_id)
        invocation_names = tuple(item.capability_name for item in aggregate.invocations)
        event_types = {event.event_type for event in aggregate.events}
        if (
            aggregate.run.status.value != "succeeded"
            or len(aggregate.nodes) != 1
            or aggregate.nodes[0].status.value != "succeeded"
            or invocation_names != ("skill.activate", "file.write")
            or any(item.status.value != "succeeded" for item in aggregate.invocations)
            or len(aggregate.artifacts) != 1
            or not observability.complete
            or not {
                "model.requested",
                "model.completed",
                "skill.activated",
                "capability.completed",
                "artifact.created",
                "run.final",
            }
            <= event_types
        ):
            raise P2GateError("P2 persisted execution is incomplete")
        projected = observability.model_dump_json()
        if str(workspace) in projected or any(
            value in projected.lower()
            for value in (
                "authorization:",
                "bearer ",
                "file://",
                "postgresql://",
                "postgresql+asyncpg://",
                "provider_response",
            )
        ):
            raise P2GateError("P2 Audit or Trace contains unsafe data")
    finally:
        await engine.dispose()


async def inspect_in_new_process(run_id: ExecutionRunId) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.acceptance.check_p2_gate",
        "--inspect",
        str(run_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    if process.returncode != 0 or stdout != b"P2 restart inspection: PASS\n" or stderr:
        raise P2GateError("P2 new-process inspection failed")


async def accept_real_vertical_slice(workspace: Path) -> tuple[TaskId, ExecutionRunId]:
    configuration = load_configuration(workspace=workspace)
    engine = create_database_engine(configuration.database.require("test"))
    model = OpenAICompatibleAdapter.configured(
        configuration.require_model(), protected_values=configuration.protected_values()
    )
    task_id: TaskId | None = None
    run_id: ExecutionRunId | None = None
    try:
        registry = local_capability_registry(
            workspace_root=workspace,
            allowed_executables={"python": Path(sys.executable)},
            environment={"PYTHONUTF8": "1"},
        )
        register_workspace_skill(registry, workspace_root=workspace)
        runtime = PersistentRuntime(model, registry, SQLAlchemyUnitOfWorkFactory(engine))
        result = await runtime.execute(
            "First call skill.activate for @steipete/weather. After its Tool Result, call "
            "file.write exactly once with path p2/result.txt and content p2-runtime-accepted. "
            "After both Tool Results, return one short final sentence."
        )
        if (
            not result.persisted
            or result.outcome.status is not AgentOutcomeStatus.SUCCEEDED
            or result.outcome.model_turn_count < 2
            or result.outcome.capability_call_count != 2
            or len(result.outcome.artifacts) != 1
        ):
            raise P2GateError("P2 real vertical slice did not complete")
        task_id, run_id = result.task_id, result.run_id
        await inspect_in_new_process(result.run_id)
        return result.task_id, result.run_id
    except BaseException:
        if task_id is not None:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
        if run_id is not None:
            cleanup_workspace(workspace, (run_id,))
        raise
    finally:
        await model.aclose()
        await engine.dispose()


def cleanup_workspace(workspace: Path, run_ids: tuple[ExecutionRunId, ...]) -> None:
    for run_id in run_ids:
        for parent in (workspace / "runs", workspace / "artifacts"):
            target = parent / str(run_id)
            if target.is_relative_to(workspace) and target.name == str(run_id):
                shutil.rmtree(target, ignore_errors=True)


async def accept_p2_gate(workspace: Path) -> None:
    task_id: TaskId | None = None
    run_id: ExecutionRunId | None = None
    boundary_runs: tuple[ExecutionRunId, ...] = ()
    try:
        await accept_model_failures()
        boundary_runs = await accept_capability_failures(workspace)
        task_id, run_id = await accept_real_vertical_slice(workspace)
    finally:
        cleanup_workspace(
            workspace,
            boundary_runs + (() if run_id is None else (run_id,)),
        )
        if task_id is not None:
            engine = create_database_engine(database_url(DatabaseProfile.TEST))
            try:
                async with engine.begin() as connection:
                    await connection.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
            finally:
                await engine.dispose()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(add_help=False)
    result.add_argument("--inspect")
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        workspace = resolve_workspace().path
        if arguments.inspect is not None:
            asyncio.run(inspect_gate_run(ExecutionRunId(UUID(arguments.inspect)), workspace))
            print("P2 restart inspection: PASS")
            return 0
        asyncio.run(accept_p2_gate(workspace))
    except WorkspaceResolutionError as exc:
        print(f"P2 Gate: FAIL [{exc.code}]", file=sys.stderr)
        return 1
    except AnbanError as exc:
        print(f"P2 Gate: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except (P2GateError, ValueError):
        print("P2 Gate: FAIL [acceptance_invalid]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"P2 Gate: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "P2 Gate: PASS - real Model, Skill, Capability, PostgreSQL, Artifact, Audit/Trace, "
        "restart, fail-closed boundaries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
