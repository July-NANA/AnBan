"""Real local production Registry acceptance without a model or network dependency."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete

from anban.capability import (
    CapabilityResultStatus,
    InvocationContext,
    MemoryContextCapability,
    local_capability_components,
)
from anban.config import load_configuration
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
    new_task_id,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRun, NodeRun, Task, now_utc
from anban.persistence import SQLAlchemyUnitOfWorkFactory, create_database_engine
from anban.persistence.models import TaskRecord


class CapabilityAcceptanceError(RuntimeError):
    """Safe acceptance failure without process output or physical paths."""


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=now_utc() + timedelta(seconds=30),
    )


async def accept_capabilities() -> None:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("test"))
    factory = SQLAlchemyUnitOfWorkFactory(engine)
    memory = MemoryContextCapability(
        factory,
        protected_values=configuration.protected_values(),
    )
    registry, inventory = local_capability_components(
        workspace_root=configuration.workspace,
        protected_values=configuration.protected_values(),
        model_available=configuration.model is not None,
        additional_handlers=(memory,),
    )
    work = configuration.workspace / "tmp" / f"acceptance-{new_execution_run_id()}"
    skill_name = f"skill-{uuid4().hex[:12]}"
    skill_root = configuration.workspace / "skills" / skill_name
    invocation = context()
    memory_task = Task(id=new_task_id(), request="local Memory Capability acceptance")
    memory_run = ExecutionRun(id=new_execution_run_id(), task_id=memory_task.id)
    memory_node = NodeRun(id=new_node_run_id(), run_id=memory_run.id, node_name="general_agent")
    try:
        if tuple(item.name for item in registry.search()) != (
            "memory.context",
            "process.execute",
            "skill.activate",
        ):
            raise CapabilityAcceptanceError("production Capability surface mismatch")
        work.mkdir(mode=0o700)
        async with factory() as unit:
            await unit.executions.add_task(memory_task)
            await unit.executions.add_run(memory_run)
            await unit.executions.add_node_run(memory_node)
            await unit.commit()

        memory_context = InvocationContext(
            run_id=memory_run.id,
            node_run_id=memory_node.id,
            invocation_id=new_capability_invocation_id(),
            deadline_at=now_utc() + timedelta(seconds=30),
            metadata=SafeMetadata(),
        )
        retained = await registry.invoke(
            "memory.context",
            {
                "operation": "remember",
                "scope": "task",
                "kind": "user_fact",
                "content": "A unique bounded acceptance fact.",
            },
            memory_context,
        )
        recalled = await registry.invoke(
            "memory.context",
            {"operation": "read", "scope": "task"},
            memory_context.model_copy(update={"invocation_id": new_capability_invocation_id()}),
        )
        if (
            retained.status is not CapabilityResultStatus.COMPLETED
            or recalled.status is not CapabilityResultStatus.COMPLETED
            or "A unique bounded acceptance fact." not in (recalled.observation or "")
        ):
            raise CapabilityAcceptanceError("real Memory retention or recall mismatch")

        completed = await registry.invoke(
            "process.execute",
            {
                "command": sys.executable,
                "cwd": str(work),
                "env": [{"name": "ANBAN_ACCEPTANCE_VALUE", "value": "overridden"}],
                "stdin": "stdin-value",
                "args": [
                    "-c",
                    "import os,sys;from pathlib import Path;"
                    "Path('result.txt').write_text(sys.stdin.read()+'-'+os.environ['ANBAN_ACCEPTANCE_VALUE']);"
                    "Path('summary.json').write_text('{\"ok\":true}');"
                    "print('completed')",
                ],
                "artifacts": [
                    {"path": "result.txt", "media_type": "text/plain"},
                    {"path": "summary.json", "media_type": "application/json"},
                ],
            },
            invocation,
        )
        if (
            completed.status is not CapabilityResultStatus.COMPLETED
            or len(completed.artifacts) != 2
        ):
            raise CapabilityAcceptanceError("real Process or Artifact did not complete")
        payload = json.loads(completed.observation or "{}")
        if payload.get("stdout") != "completed\n":
            raise CapabilityAcceptanceError("real Process output mismatch")
        artifact_root = configuration.workspace / "artifacts" / str(invocation.run_id)
        snapshots = [
            (artifact_root / str(artifact.id)).read_text(encoding="utf-8")
            for artifact in completed.artifacts
        ]
        if snapshots != ["stdin-value-overridden", '{"ok":true}']:
            raise CapabilityAcceptanceError("Artifact snapshot mismatch")

        nonzero = await registry.invoke(
            "process.execute",
            {"command": sys.executable, "args": ["-c", "raise SystemExit(9)"]},
            context(),
        )
        missing_artifact = await registry.invoke(
            "process.execute",
            {
                "command": sys.executable,
                "cwd": str(work),
                "args": ["-c", "pass"],
                "artifacts": [{"path": "result.txt"}, {"path": "missing.txt"}],
            },
            context(),
        )
        if (
            nonzero.status is not CapabilityResultStatus.FAILED
            or missing_artifact.status is not CapabilityResultStatus.FAILED
        ):
            raise CapabilityAcceptanceError("Process failure paths returned success")

        skill_root.mkdir(mode=0o700)
        skill_root.joinpath("SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            "description: Validate real runtime catalog refresh.\n"
            "---\n\n"
            "Use only ordinary governed Capabilities.\n",
            encoding="utf-8",
        )
        skill_slug = f"@local/{skill_name}"
        discovered = inventory.describe(skill_slug)
        activated = await registry.invoke(
            "skill.activate",
            {"name": skill_slug},
            context(),
        )
        if (
            discovered.version_digest is None
            or activated.status is not CapabilityResultStatus.COMPLETED
            or activated.metadata.root.get("skill_slug") != skill_slug
            or not isinstance(activated.metadata.root.get("catalog_digest"), str)
        ):
            raise CapabilityAcceptanceError("runtime Skill refresh or activation mismatch")
        shutil.rmtree(skill_root)
        try:
            inventory.describe(skill_slug)
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.CAPABILITY_UNKNOWN:
                raise
        else:
            raise CapabilityAcceptanceError("removed Skill remained in the runtime catalog")
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(skill_root, ignore_errors=True)
            shutil.rmtree(
                configuration.workspace / "artifacts" / str(invocation.run_id),
                ignore_errors=True,
            )
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id == memory_task.id))
        finally:
            await engine.dispose()


def main() -> int:
    try:
        asyncio.run(accept_capabilities())
    except AnbanError as exc:
        print(f"local Capability acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"local Capability acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "local Capability acceptance: PASS - surface, process, stdin, env, multi-Artifact, "
        "runtime Skill refresh, durable Memory retention/restart, failures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
