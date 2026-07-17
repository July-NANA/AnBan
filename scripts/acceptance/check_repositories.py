"""Real PostgreSQL Repository and transaction acceptance against anban_test."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, func, select, text

from anban.core import (
    AnbanError,
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    ErrorCode,
    Event,
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    NodeRunStatus,
    SafeMetadata,
    Task,
    TaskStatus,
    ensure_capability_invocation_transition,
    ensure_execution_run_transition,
    ensure_node_run_transition,
    ensure_task_transition,
    new_artifact_id,
    new_capability_invocation_id,
    new_event_id,
    new_execution_run_id,
    new_node_run_id,
    new_task_id,
)
from anban.persistence import (
    DatabaseProfile,
    SQLAlchemyUnitOfWorkFactory,
    create_database_engine,
)
from anban.persistence.models import TaskRecord


class RepositoryAcceptanceError(RuntimeError):
    """Safe local acceptance failure without database exception text."""


def records() -> tuple[
    Task,
    ExecutionRun,
    NodeRun,
    CapabilityInvocation,
    Artifact,
    tuple[Event, Event],
]:
    task = Task(
        id=new_task_id(),
        request="repository integration acceptance",
        metadata=SafeMetadata({"source": "acceptance"}),
    )
    run = ExecutionRun(id=new_execution_run_id(), task_id=task.id)
    node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
    invocation = CapabilityInvocation(
        id=new_capability_invocation_id(),
        run_id=run.id,
        node_run_id=node.id,
        capability_name="file.write",
    )
    artifact = Artifact(
        id=new_artifact_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        uri=f"anban://artifact/{run.id}/acceptance",
        sha256="b" * 64,
        size_bytes=10,
        media_type="text/plain",
    )
    event_one = Event(
        id=new_event_id(),
        run_id=run.id,
        sequence=1,
        event_type="run.created",
    )
    event_two = Event(
        id=new_event_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        artifact_id=artifact.id,
        sequence=2,
        event_type="artifact.created",
    )
    return task, run, node, invocation, artifact, (event_one, event_two)


async def accept_repositories() -> None:
    engine = create_database_engine(DatabaseProfile.TEST)
    factory = SQLAlchemyUnitOfWorkFactory(engine)
    task, run, node, invocation, artifact, events = records()
    failed_task = Task(id=new_task_id(), request="must roll back")
    failed_run = ExecutionRun(id=new_execution_run_id(), task_id=failed_task.id)
    cleanup_ids = (task.id, failed_task.id)
    try:
        async with engine.connect() as connection:
            identity = (await connection.execute(text("SELECT current_database()"))).scalar_one()
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            if identity != "anban_test" or revision != "0003_capability_error":
                raise RepositoryAcceptanceError("test database or migration identity mismatch")

        async with factory() as unit:
            await unit.executions.add_task(task)
            await unit.executions.add_run(run)
            await unit.executions.add_node_run(node)
            await unit.executions.add_invocation(invocation)
            await unit.executions.add_artifact(artifact)
            await unit.executions.add_event(events[1])
            await unit.executions.add_event(events[0])
            await unit.commit()

        async with factory() as unit:
            aggregate = await unit.executions.load_run(run.id)
            ordered = await unit.executions.list_events(run.id)
            if aggregate is None:
                raise RepositoryAcceptanceError("Run aggregate was not reconstructed")
            if aggregate.task != task or aggregate.run != run:
                raise RepositoryAcceptanceError("Task or Run reconstruction mismatch")
            if aggregate.nodes != (node,) or aggregate.invocations != (invocation,):
                raise RepositoryAcceptanceError("Node or Invocation reconstruction mismatch")
            if aggregate.artifacts != (artifact,) or aggregate.events != events:
                raise RepositoryAcceptanceError("Artifact or Event reconstruction mismatch")
            if ordered != events:
                raise RepositoryAcceptanceError("Event order is not deterministic")

        ensure_task_transition(task.status, TaskStatus.RUNNING)
        ensure_execution_run_transition(run.status, ExecutionRunStatus.RUNNING)
        ensure_node_run_transition(node.status, NodeRunStatus.RUNNING)
        ensure_capability_invocation_transition(
            invocation.status, CapabilityInvocationStatus.RUNNING
        )
        running_task = task.model_copy(update={"status": TaskStatus.RUNNING})
        running_run = run.model_copy(update={"status": ExecutionRunStatus.RUNNING})
        running_node = node.model_copy(update={"status": NodeRunStatus.RUNNING})
        running_invocation = invocation.model_copy(
            update={"status": CapabilityInvocationStatus.RUNNING}
        )
        async with factory() as unit:
            await unit.executions.update_task(running_task)
            await unit.executions.update_run(running_run)
            await unit.executions.update_node_run(running_node)
            await unit.executions.update_invocation(running_invocation)
            await unit.commit()

        async with factory() as unit:
            if await unit.executions.get_task(task.id) != running_task:
                raise RepositoryAcceptanceError("Task update was not durable")
            if await unit.executions.get_run(run.id) != running_run:
                raise RepositoryAcceptanceError("Run update was not durable")
            if await unit.executions.get_node_run(node.id) != running_node:
                raise RepositoryAcceptanceError("Node update was not durable")
            if await unit.executions.get_invocation(invocation.id) != running_invocation:
                raise RepositoryAcceptanceError("Invocation update was not durable")
            if await unit.executions.get_artifact(artifact.id) != artifact:
                raise RepositoryAcceptanceError("Artifact read mismatch")
            if await unit.executions.get_event(events[0].id) != events[0]:
                raise RepositoryAcceptanceError("Event read mismatch")

        try:
            async with factory() as unit:
                await unit.executions.update_task(running_task)
                await unit.commit()
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.INVALID_TRANSITION:
                raise RepositoryAcceptanceError("stale update error code mismatch") from exc
        else:
            raise RepositoryAcceptanceError("stale status update unexpectedly committed")

        try:
            async with factory() as unit:
                await unit.executions.add_task(failed_task)
                await unit.executions.add_run(failed_run)
                duplicate = Event(
                    id=new_event_id(),
                    run_id=failed_run.id,
                    sequence=1,
                    event_type="run.duplicate",
                )
                await unit.executions.add_event(duplicate)
                await unit.executions.add_event(duplicate.model_copy(update={"id": new_event_id()}))
                await unit.commit()
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.PERSISTENCE_WRITE_FAILED:
                raise RepositoryAcceptanceError("failed transaction error code mismatch") from exc
        else:
            raise RepositoryAcceptanceError("invalid transaction unexpectedly committed")

        async with factory() as unit:
            if await unit.executions.get_task(failed_task.id) is not None:
                raise RepositoryAcceptanceError("failed transaction left partial Task state")
            if await unit.executions.get_run(failed_run.id) is not None:
                raise RepositoryAcceptanceError("failed transaction left partial Run state")
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRecord).where(TaskRecord.id.in_(cleanup_ids)))
            async with engine.connect() as connection:
                remaining = (
                    await connection.execute(
                        select(func.count())
                        .select_from(TaskRecord)
                        .where(TaskRecord.id.in_(cleanup_ids))
                    )
                ).scalar_one()
                if remaining != 0:
                    raise RepositoryAcceptanceError("acceptance cleanup was incomplete")
        finally:
            await engine.dispose()


def main() -> int:
    try:
        asyncio.run(accept_repositories())
    except Exception as exc:
        print(f"repository acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("repository acceptance: PASS - create, read, locked update, aggregate, order, rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
