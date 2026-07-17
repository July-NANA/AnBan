"""Real Model/Capability/PostgreSQL acceptance for PersistentRuntime."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete

from anban.capability import local_capability_registry
from anban.core.errors import AnbanError
from anban.core.ids import ExecutionRunId, TaskId
from anban.model import OpenAICompatibleAdapter
from anban.persistence import (
    DatabaseProfile,
    SQLAlchemyUnitOfWorkFactory,
    create_database_engine,
)
from anban.persistence.models import TaskRecord
from anban.runtime import AgentOutcomeStatus, EventProjectionService, PersistentRuntime
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace


class RuntimeAcceptanceError(RuntimeError):
    """Safe failure without request, provider data, database values, or host paths."""


async def inspect_persisted_run(run_id: ExecutionRunId) -> None:
    engine = create_database_engine(DatabaseProfile.TEST)
    try:
        factory = SQLAlchemyUnitOfWorkFactory(engine)
        async with factory() as unit:
            aggregate = await unit.executions.load_run(run_id)
        observability = await EventProjectionService(factory).inspect(run_id)
        if aggregate is None:
            raise RuntimeAcceptanceError("persisted Run is unavailable")
        event_types = {event.event_type for event in aggregate.events}
        if (
            aggregate.task.status.value != "succeeded"
            or aggregate.run.status.value != "succeeded"
            or len(aggregate.nodes) != 1
            or aggregate.nodes[0].status.value != "succeeded"
            or len(aggregate.invocations) != 1
            or aggregate.invocations[0].status.value != "succeeded"
            or len(aggregate.artifacts) != 1
            or tuple(event.sequence for event in aggregate.events)
            != tuple(range(1, len(aggregate.events) + 1))
            or not {
                "model.requested",
                "model.completed",
                "capability.requested",
                "capability.completed",
                "artifact.created",
                "run.final",
            }
            <= event_types
            or not observability.complete
            or observability.inconsistencies
            or len(observability.trace) != len(aggregate.events)
            or not observability.audit
        ):
            raise RuntimeAcceptanceError("persisted Run aggregate is incomplete")
        safe_projection = observability.model_dump_json().lower()
        if any(
            forbidden in safe_projection
            for forbidden in (
                "authorization:",
                "bearer ",
                "file://",
                "postgresql://",
                "postgresql+asyncpg://",
                "provider_response",
            )
        ):
            raise RuntimeAcceptanceError("observability projection contains unsafe data")
    finally:
        await engine.dispose()


async def inspect_in_new_process(run_id: ExecutionRunId) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.acceptance.check_persistent_runtime",
        "--inspect",
        str(run_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    if process.returncode != 0 or stdout != b"persistent runtime inspection: PASS\n" or stderr:
        raise RuntimeAcceptanceError("new-process persistence inspection failed")


async def accept_persistent_runtime(workspace: Path) -> None:
    engine = create_database_engine(DatabaseProfile.TEST)
    model = OpenAICompatibleAdapter.configured()
    task_id: TaskId | None = None
    run_id: ExecutionRunId | None = None
    try:
        runtime = PersistentRuntime(
            model,
            local_capability_registry(workspace_root=workspace),
            SQLAlchemyUnitOfWorkFactory(engine),
        )
        result = await runtime.execute(
            "Call file.write exactly once with path acceptance/runtime.txt and content "
            "persistent-runtime-acceptance. After the Tool Result, answer with one short sentence."
        )
        task_id = result.task_id
        run_id = result.run_id
        if (
            not result.persisted
            or result.outcome.status is not AgentOutcomeStatus.SUCCEEDED
            or result.outcome.model_turn_count < 2
            or result.outcome.capability_call_count != 1
            or len(result.outcome.artifacts) != 1
        ):
            raise RuntimeAcceptanceError("real persistent Runtime outcome is invalid")
        await inspect_in_new_process(run_id)
    finally:
        await model.aclose()
        if task_id is not None:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
        await engine.dispose()
        if run_id is not None:
            for parent in (workspace / "runs", workspace / "artifacts"):
                target = parent / str(run_id)
                if target.is_relative_to(workspace) and target.name == str(run_id):
                    shutil.rmtree(target, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(add_help=False)
    result.add_argument("--inspect")
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.inspect is not None:
            run_id = ExecutionRunId(UUID(arguments.inspect))
            asyncio.run(inspect_persisted_run(run_id))
            print("persistent runtime inspection: PASS")
            return 0
        workspace = resolve_workspace().path
        asyncio.run(accept_persistent_runtime(workspace))
    except WorkspaceResolutionError as exc:
        print(f"persistent Runtime acceptance: FAIL [{exc.code}]", file=sys.stderr)
        return 1
    except (ValueError, RuntimeAcceptanceError):
        print("persistent Runtime acceptance: FAIL [acceptance_invalid]", file=sys.stderr)
        return 1
    except AnbanError as exc:
        print(f"persistent Runtime acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"persistent Runtime acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "persistent Runtime acceptance: PASS - real model, Capability, PostgreSQL, "
        "Audit/Trace, restart"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
