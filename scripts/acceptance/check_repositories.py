"""Real PostgreSQL Repository and transaction acceptance against anban_test."""

from __future__ import annotations

import asyncio
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from anban.config import load_configuration
from anban.core import (
    AnbanError,
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    ContextConflictState,
    ContextEntry,
    ContextEntryKind,
    ContextScope,
    ContextSource,
    ContextSourceKind,
    ContextSummary,
    ErrorCode,
    Event,
    ExecutionRun,
    ExecutionRunStatus,
    GraphRevision,
    NodeRun,
    NodeRunStatus,
    SafeMetadata,
    Task,
    TaskGraphEdge,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
    TaskGraphValueBinding,
    TaskGraphValueSource,
    TaskStatus,
    ensure_capability_invocation_transition,
    ensure_execution_run_transition,
    ensure_node_run_transition,
    ensure_task_transition,
    new_artifact_id,
    new_capability_invocation_id,
    new_context_entry_id,
    new_context_summary_id,
    new_event_id,
    new_execution_run_id,
    new_node_run_id,
    new_session_id,
    new_task_id,
)
from anban.persistence import SQLAlchemyUnitOfWorkFactory, create_database_engine
from anban.persistence.models import (
    ContextEntryRecord,
    ContextSummaryRecord,
    GraphRevisionRecord,
    TaskRecord,
)


class RepositoryAcceptanceError(RuntimeError):
    """Safe local acceptance failure without database exception text."""


def records() -> tuple[
    Task,
    tuple[GraphRevision, GraphRevision],
    ExecutionRun,
    NodeRun,
    CapabilityInvocation,
    tuple[Artifact, Artifact],
    tuple[Event, Event, Event],
]:
    task = Task(
        id=new_task_id(),
        request="repository integration acceptance",
        metadata=SafeMetadata({"source": "acceptance"}),
    )
    first_revision = GraphRevision.create(
        task_id=task.id,
        reason="Initial repository graph revision.",
        spec=graph_spec("first_result"),
    )
    second_revision = GraphRevision.create(
        task_id=task.id,
        previous_revision_id=first_revision.id,
        reason="Append changed graph content without overwriting the first revision.",
        spec=graph_spec("second_result"),
    )
    run = ExecutionRun(
        id=new_execution_run_id(),
        task_id=task.id,
        graph_revision_id=second_revision.id,
    )
    node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
    invocation = CapabilityInvocation(
        id=new_capability_invocation_id(),
        run_id=run.id,
        node_run_id=node.id,
        capability_name="process.execute",
    )
    artifact_one = Artifact(
        id=new_artifact_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        uri=f"anban://artifact/{run.id}/acceptance-one",
        sha256="b" * 64,
        size_bytes=10,
        media_type="text/plain",
    )
    artifact_two = Artifact(
        id=new_artifact_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        uri=f"anban://artifact/{run.id}/acceptance-two",
        sha256="c" * 64,
        size_bytes=11,
        media_type="application/json",
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
        artifact_id=artifact_one.id,
        sequence=2,
        event_type="artifact.created",
    )
    event_three = Event(
        id=new_event_id(),
        run_id=run.id,
        node_run_id=node.id,
        invocation_id=invocation.id,
        artifact_id=artifact_two.id,
        sequence=3,
        event_type="artifact.created",
    )
    return (
        task,
        (first_revision, second_revision),
        run,
        node,
        invocation,
        (artifact_one, artifact_two),
        (event_one, event_two, event_three),
    )


def graph_spec(output_key: str) -> TaskGraphSpec:
    node = TaskGraphNode(
        id="persist_graph",
        kind=TaskGraphNodeKind.ACTION,
        objective=f"Produce the bounded {output_key} value.",
        outputs=(output_key,),
    )
    return TaskGraphSpec(
        nodes=(node,),
        edges=tuple[TaskGraphEdge](),
        entry_node=node.id,
        terminal_nodes=(node.id,),
        outputs={
            output_key: TaskGraphValueBinding(
                source=TaskGraphValueSource.NODE_OUTPUT,
                node_id=node.id,
                key=output_key,
            )
        },
    )


async def accept_repositories() -> None:
    engine = create_database_engine(load_configuration().database.require("test"))
    factory = SQLAlchemyUnitOfWorkFactory(engine)
    task, graph_revisions, run, node, invocation, artifacts, events = records()
    session_id = new_session_id()
    task_entry = ContextEntry(
        id=new_context_entry_id(),
        scope=ContextScope.TASK,
        task_id=task.id,
        kind=ContextEntryKind.USER_FACT,
        content="authoritative repository context",
        source=ContextSource(
            kind=ContextSourceKind.INTERACTION,
            reference="interaction:repository-acceptance",
        ),
    )
    session_entry = ContextEntry(
        id=new_context_entry_id(),
        scope=ContextScope.SESSION,
        session_id=session_id,
        kind=ContextEntryKind.SUPPLEMENT,
        content="durable session context",
        source=ContextSource(
            kind=ContextSourceKind.RUNTIME,
            reference="runtime:repository-acceptance",
        ),
    )
    summary = ContextSummary(
        id=new_context_summary_id(),
        scope=ContextScope.TASK,
        task_id=task.id,
        covered_entry_ids=(task_entry.id,),
        content="A bounded summary retaining the original entry identity.",
    )
    superseded_task_entry = task_entry.model_copy(update={"state": ContextConflictState.SUPERSEDED})
    failed_task = Task(id=new_task_id(), request="must roll back")
    failed_run = ExecutionRun(id=new_execution_run_id(), task_id=failed_task.id)
    cleanup_ids = (task.id, failed_task.id)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            expected_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            if revision != expected_head:
                raise RepositoryAcceptanceError("migration identity mismatch")

        async with factory() as unit:
            await unit.executions.add_task(task)
            for revision in graph_revisions:
                await unit.executions.add_graph_revision(revision)
            await unit.executions.add_run(run)
            await unit.executions.add_node_run(node)
            await unit.executions.add_invocation(invocation)
            for artifact in artifacts:
                await unit.executions.add_artifact(artifact)
            for event in reversed(events):
                await unit.executions.add_event(event)
            await unit.executions.add_context_entry(task_entry)
            await unit.executions.add_context_entry(session_entry)
            await unit.executions.add_context_summary(summary)
            await unit.executions.update_context_entry(superseded_task_entry)
            await unit.commit()

        async with factory() as unit:
            aggregate = await unit.executions.load_run(run.id)
            ordered = await unit.executions.list_events(run.id)
            if aggregate is None:
                raise RepositoryAcceptanceError("Run aggregate was not reconstructed")
            if aggregate.task != task or aggregate.run != run:
                raise RepositoryAcceptanceError("Task or Run reconstruction mismatch")
            if aggregate.graph_revision != graph_revisions[-1]:
                raise RepositoryAcceptanceError("Run Graph revision reconstruction mismatch")
            if aggregate.nodes != (node,) or aggregate.invocations != (invocation,):
                raise RepositoryAcceptanceError("Node or Invocation reconstruction mismatch")
            if aggregate.artifacts != artifacts or aggregate.events != events:
                raise RepositoryAcceptanceError("Artifact or Event reconstruction mismatch")
            if ordered != events:
                raise RepositoryAcceptanceError("Event order is not deterministic")
            if await unit.executions.list_context_entries(ContextScope.TASK, task.id) != (
                superseded_task_entry,
            ):
                raise RepositoryAcceptanceError("Task Context reconstruction mismatch")
            if await unit.executions.list_context_entries(ContextScope.SESSION, session_id) != (
                session_entry,
            ):
                raise RepositoryAcceptanceError("Session Context reconstruction mismatch")
            if await unit.executions.list_context_summaries(ContextScope.TASK, task.id) != (
                summary,
            ):
                raise RepositoryAcceptanceError("Context summary reconstruction mismatch")
            if await unit.executions.list_graph_revisions(task.id) != graph_revisions:
                raise RepositoryAcceptanceError("Graph revision history reconstruction mismatch")
            if await unit.executions.get_current_graph_revision(task.id) != graph_revisions[-1]:
                raise RepositoryAcceptanceError("Current Graph revision mismatch")
            if (
                await unit.executions.get_graph_revision(graph_revisions[0].id)
                != graph_revisions[0]
            ):
                raise RepositoryAcceptanceError("Graph revision identity query mismatch")

        stale_revision = GraphRevision.create(
            task_id=task.id,
            previous_revision_id=graph_revisions[0].id,
            reason="This stale branch must be rejected.",
            spec=graph_spec("stale_result"),
        )
        try:
            async with factory() as unit:
                await unit.executions.add_graph_revision(stale_revision)
                await unit.commit()
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.PERSISTENCE_WRITE_FAILED:
                raise RepositoryAcceptanceError("Graph append error code mismatch") from exc
        else:
            raise RepositoryAcceptanceError("stale Graph revision unexpectedly committed")

        try:
            async with engine.begin() as connection:
                await connection.execute(
                    update(GraphRevisionRecord)
                    .where(GraphRevisionRecord.id == graph_revisions[0].id)
                    .values(reason="Attempted in-place mutation")
                )
        except SQLAlchemyError:
            pass
        else:
            raise RepositoryAcceptanceError("Graph revision UPDATE bypassed immutability")

        async with factory() as unit:
            if await unit.executions.list_graph_revisions(task.id) != graph_revisions:
                raise RepositoryAcceptanceError("Graph revision history changed after rejection")

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
            for artifact in artifacts:
                if await unit.executions.get_artifact(artifact.id) != artifact:
                    raise RepositoryAcceptanceError("Artifact read mismatch")
            if await unit.executions.get_event(events[0].id) != events[0]:
                raise RepositoryAcceptanceError("Event read mismatch")
            if await unit.executions.get_context_entry(task_entry.id) != superseded_task_entry:
                raise RepositoryAcceptanceError("Context entry read mismatch")

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

        invalid_summary = summary.model_copy(
            update={
                "id": new_context_summary_id(),
                "covered_entry_ids": (new_context_entry_id(),),
            }
        )
        try:
            async with factory() as unit:
                await unit.executions.add_context_summary(invalid_summary)
                await unit.commit()
        except AnbanError as exc:
            if exc.info.code is not ErrorCode.PERSISTENCE_WRITE_FAILED:
                raise RepositoryAcceptanceError("Context rollback error mismatch") from exc
        else:
            raise RepositoryAcceptanceError("invalid Context summary unexpectedly committed")

        async with factory() as unit:
            if await unit.executions.get_task(failed_task.id) is not None:
                raise RepositoryAcceptanceError("failed transaction left partial Task state")
            if await unit.executions.get_run(failed_run.id) is not None:
                raise RepositoryAcceptanceError("failed transaction left partial Run state")
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(ContextSummaryRecord).where(
                        ContextSummaryRecord.session_id == session_id
                    )
                )
                await connection.execute(
                    delete(ContextEntryRecord).where(ContextEntryRecord.session_id == session_id)
                )
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
    print(
        "repository acceptance: PASS - create, read, locked update, aggregate, order, "
        "Task/Session Context restart, immutable Graph revisions, summary coverage, rollback"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
