"""Focused SQLAlchemy implementation of the v0.1 execution Repository Port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from anban.core.context import ContextEntry, ContextScope, ContextSummary
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.graph import GraphRevision
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    CheckpointId,
    ContextEntryId,
    EventId,
    ExecutionRunId,
    GraphRevisionId,
    NodeRunId,
    SessionId,
    TaskId,
)
from anban.core.lifecycle import (
    ensure_capability_invocation_transition,
    ensure_checkpoint_transition,
    ensure_execution_run_transition,
    ensure_node_run_transition,
    ensure_task_transition,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    Checkpoint,
    CheckpointStatus,
    Event,
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    NodeRunStatus,
    Task,
    TaskStatus,
)
from anban.core.persistence import ExecutionRunAggregate
from anban.persistence.mappers import (
    artifact_domain,
    artifact_record,
    checkpoint_domain,
    checkpoint_record,
    context_coverage_records,
    context_entry_domain,
    context_entry_record,
    context_summary_domain,
    context_summary_record,
    event_domain,
    event_record,
    graph_revision_domain,
    graph_revision_record,
    invocation_domain,
    invocation_record,
    node_domain,
    node_record,
    run_domain,
    run_record,
    task_domain,
    task_record,
)
from anban.persistence.models import (
    ArtifactRecord,
    CapabilityInvocationRecord,
    CheckpointRecord,
    ContextEntryRecord,
    ContextSummaryCoverageRecord,
    ContextSummaryRecord,
    EventRecord,
    ExecutionRunRecord,
    GraphRevisionRecord,
    NodeRunRecord,
    TaskRecord,
)


def missing_record(entity: str, record_id: UUID) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="persistence update target does not exist",
            details=SafeMetadata({"entity": entity, "record_id": str(record_id)}),
        )
    )


def inconsistent_run() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_UNAVAILABLE,
            message="persisted Run relationships are incomplete",
        )
    )


def graph_revision_conflict(reason: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="Graph revision history rejected an append",
            details=SafeMetadata({"reason": reason}),
        )
    )


class SQLAlchemyExecutionRepository:
    """One aggregate-focused Repository bound to an active AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_task(self, task: Task) -> None:
        self._session.add(task_record(task))
        await self._session.flush()

    async def get_task(self, task_id: TaskId) -> Task | None:
        record = await self._session.get(TaskRecord, task_id)
        return None if record is None else task_domain(record)

    async def update_task(self, task: Task) -> None:
        result = await self._session.execute(
            select(TaskRecord).where(TaskRecord.id == task.id).with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("task", task.id)
        ensure_task_transition(TaskStatus(record.status), task.status)
        record.request = task.request
        record.status = task.status.value
        record.error_code = None if task.error_code is None else task.error_code.value
        record.safe_metadata = dict(task.metadata.root)

    async def add_run(self, run: ExecutionRun) -> None:
        self._session.add(run_record(run))
        await self._session.flush()

    async def get_run(self, run_id: ExecutionRunId) -> ExecutionRun | None:
        record = await self._session.get(ExecutionRunRecord, run_id)
        return None if record is None else run_domain(record)

    async def update_run(self, run: ExecutionRun) -> None:
        result = await self._session.execute(
            select(ExecutionRunRecord).where(ExecutionRunRecord.id == run.id).with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("execution_run", run.id)
        ensure_execution_run_transition(ExecutionRunStatus(record.status), run.status)
        record.status = run.status.value
        record.graph_revision_id = run.graph_revision_id
        record.started_at = run.started_at
        record.finished_at = run.finished_at
        record.final_text = run.final_text
        record.error_code = None if run.error_code is None else run.error_code.value
        record.safe_metadata = dict(run.metadata.root)

    async def add_graph_revision(self, revision: GraphRevision) -> None:
        current = await self.get_current_graph_revision(revision.task_id, lock=True)
        if revision.previous_revision_id is None:
            if current is not None:
                raise graph_revision_conflict("initial_revision_exists")
        elif current is None or current.id != revision.previous_revision_id:
            raise graph_revision_conflict("previous_revision_not_current")
        self._session.add(graph_revision_record(revision))
        await self._session.flush()

    async def get_graph_revision(self, revision_id: GraphRevisionId) -> GraphRevision | None:
        record = await self._session.get(GraphRevisionRecord, revision_id)
        return None if record is None else graph_revision_domain(record)

    async def list_graph_revisions(self, task_id: TaskId) -> tuple[GraphRevision, ...]:
        records = await self._session.scalars(
            select(GraphRevisionRecord)
            .where(GraphRevisionRecord.task_id == task_id)
            .order_by(GraphRevisionRecord.created_at, GraphRevisionRecord.id)
        )
        return tuple(graph_revision_domain(record) for record in records.all())

    async def get_current_graph_revision(
        self,
        task_id: TaskId,
        *,
        lock: bool = False,
    ) -> GraphRevision | None:
        current = aliased(GraphRevisionRecord)
        successor = aliased(GraphRevisionRecord)
        statement = (
            select(current)
            .outerjoin(
                successor,
                and_(
                    successor.task_id == current.task_id,
                    successor.previous_revision_id == current.id,
                ),
            )
            .where(current.task_id == task_id, successor.id.is_(None))
        )
        if lock:
            statement = statement.with_for_update(of=current)
        record = (await self._session.scalars(statement)).one_or_none()
        return None if record is None else graph_revision_domain(record)

    async def add_node_run(self, node_run: NodeRun) -> None:
        self._session.add(node_record(node_run))
        await self._session.flush()

    async def get_node_run(self, node_run_id: NodeRunId) -> NodeRun | None:
        record = await self._session.get(NodeRunRecord, node_run_id)
        return None if record is None else node_domain(record)

    async def update_node_run(self, node_run: NodeRun) -> None:
        result = await self._session.execute(
            select(NodeRunRecord).where(NodeRunRecord.id == node_run.id).with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("node_run", node_run.id)
        ensure_node_run_transition(NodeRunStatus(record.status), node_run.status)
        record.status = node_run.status.value
        record.started_at = node_run.started_at
        record.finished_at = node_run.finished_at
        record.output = node_run.output
        record.error_code = None if node_run.error_code is None else node_run.error_code.value
        record.safe_metadata = dict(node_run.metadata.root)

    async def add_invocation(self, invocation: CapabilityInvocation) -> None:
        self._session.add(invocation_record(invocation))
        await self._session.flush()

    async def get_invocation(
        self, invocation_id: CapabilityInvocationId
    ) -> CapabilityInvocation | None:
        record = await self._session.get(CapabilityInvocationRecord, invocation_id)
        return None if record is None else invocation_domain(record)

    async def update_invocation(self, invocation: CapabilityInvocation) -> None:
        result = await self._session.execute(
            select(CapabilityInvocationRecord)
            .where(CapabilityInvocationRecord.id == invocation.id)
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("capability_invocation", invocation.id)
        ensure_capability_invocation_transition(
            CapabilityInvocationStatus(record.status), invocation.status
        )
        record.status = invocation.status.value
        record.started_at = invocation.started_at
        record.finished_at = invocation.finished_at
        record.error_code = None if invocation.error_code is None else invocation.error_code.value
        record.safe_metadata = dict(invocation.metadata.root)

    async def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._session.add(checkpoint_record(checkpoint))
        await self._session.flush()

    async def get_checkpoint(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        record = await self._session.get(CheckpointRecord, checkpoint_id)
        return None if record is None else checkpoint_domain(record)

    async def update_checkpoint(self, checkpoint: Checkpoint) -> None:
        result = await self._session.execute(
            select(CheckpointRecord).where(CheckpointRecord.id == checkpoint.id).with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("checkpoint", checkpoint.id)
        ensure_checkpoint_transition(CheckpointStatus(record.status), checkpoint.status)
        record.status = checkpoint.status.value
        record.resumed_at = checkpoint.resumed_at
        record.finished_at = checkpoint.finished_at
        record.error_code = None if checkpoint.error_code is None else checkpoint.error_code.value
        record.safe_metadata = dict(checkpoint.metadata.root)

    async def list_checkpoints(self, run_id: ExecutionRunId) -> tuple[Checkpoint, ...]:
        records = await self._session.scalars(
            select(CheckpointRecord)
            .where(CheckpointRecord.run_id == run_id)
            .order_by(CheckpointRecord.created_at, CheckpointRecord.id)
        )
        return tuple(checkpoint_domain(record) for record in records.all())

    async def add_artifact(self, artifact: Artifact) -> None:
        self._session.add(artifact_record(artifact))
        await self._session.flush()

    async def get_artifact(self, artifact_id: ArtifactId) -> Artifact | None:
        record = await self._session.get(ArtifactRecord, artifact_id)
        return None if record is None else artifact_domain(record)

    async def add_context_entry(self, entry: ContextEntry) -> None:
        self._session.add(context_entry_record(entry))
        await self._session.flush()

    async def get_context_entry(self, entry_id: ContextEntryId) -> ContextEntry | None:
        record = await self._session.get(ContextEntryRecord, entry_id)
        return None if record is None else context_entry_domain(record)

    async def update_context_entry(self, entry: ContextEntry) -> None:
        result = await self._session.execute(
            select(ContextEntryRecord).where(ContextEntryRecord.id == entry.id).with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise missing_record("context_entry", entry.id)
        replacement = context_entry_record(entry)
        record.scope = replacement.scope
        record.task_id = replacement.task_id
        record.session_id = replacement.session_id
        record.kind = replacement.kind
        record.content = replacement.content
        record.source_kind = replacement.source_kind
        record.source_reference = replacement.source_reference
        record.source_observed_at = replacement.source_observed_at
        record.sensitivity = replacement.sensitivity
        record.state = replacement.state
        record.artifact_id = replacement.artifact_id
        record.supersedes = replacement.supersedes
        record.conflicts_with = replacement.conflicts_with
        record.created_at = replacement.created_at
        record.expires_at = replacement.expires_at
        record.safe_metadata = replacement.safe_metadata

    async def add_context_summary(self, summary: ContextSummary) -> None:
        self._session.add(context_summary_record(summary))
        await self._session.flush()
        self._session.add_all(context_coverage_records(summary))
        await self._session.flush()

    async def list_context_entries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextEntry, ...]:
        identity_column = (
            ContextEntryRecord.task_id
            if scope is ContextScope.TASK
            else ContextEntryRecord.session_id
        )
        records = await self._session.scalars(
            select(ContextEntryRecord)
            .where(ContextEntryRecord.scope == scope.value, identity_column == identity)
            .order_by(ContextEntryRecord.created_at, ContextEntryRecord.id)
        )
        return tuple(context_entry_domain(record) for record in records.all())

    async def list_context_summaries(
        self, scope: ContextScope, identity: TaskId | SessionId
    ) -> tuple[ContextSummary, ...]:
        identity_column = (
            ContextSummaryRecord.task_id
            if scope is ContextScope.TASK
            else ContextSummaryRecord.session_id
        )
        records = tuple(
            (
                await self._session.scalars(
                    select(ContextSummaryRecord)
                    .where(ContextSummaryRecord.scope == scope.value, identity_column == identity)
                    .order_by(ContextSummaryRecord.created_at, ContextSummaryRecord.id)
                )
            ).all()
        )
        if not records:
            return ()
        coverage = await self._session.execute(
            select(
                ContextSummaryCoverageRecord.summary_id,
                ContextSummaryCoverageRecord.entry_id,
            )
            .where(
                ContextSummaryCoverageRecord.summary_id.in_(tuple(record.id for record in records))
            )
            .order_by(
                ContextSummaryCoverageRecord.summary_id,
                ContextSummaryCoverageRecord.ordinal,
            )
        )
        grouped: dict[UUID, list[ContextEntryId]] = {}
        for summary_id, entry_id in coverage.all():
            grouped.setdefault(summary_id, []).append(ContextEntryId(entry_id))
        return tuple(
            context_summary_domain(record, tuple(grouped.get(record.id, ()))) for record in records
        )

    async def add_event(self, event: Event) -> None:
        self._session.add(event_record(event))
        await self._session.flush()

    async def get_event(self, event_id: EventId) -> Event | None:
        record = await self._session.get(EventRecord, event_id)
        return None if record is None else event_domain(record)

    async def list_events(self, run_id: ExecutionRunId) -> tuple[Event, ...]:
        result = await self._session.scalars(
            select(EventRecord)
            .where(EventRecord.run_id == run_id)
            .order_by(EventRecord.sequence, EventRecord.id)
        )
        return tuple(event_domain(record) for record in result.all())

    async def list_runs(self, limit: int) -> tuple[ExecutionRun, ...]:
        result = await self._session.scalars(
            select(ExecutionRunRecord)
            .order_by(ExecutionRunRecord.created_at.desc(), ExecutionRunRecord.id.desc())
            .limit(limit)
        )
        return tuple(run_domain(record) for record in result.all())

    async def load_run(self, run_id: ExecutionRunId) -> ExecutionRunAggregate | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        task = await self.get_task(run.task_id)
        if task is None:
            raise inconsistent_run()
        graph_revision = (
            None
            if run.graph_revision_id is None
            else await self.get_graph_revision(run.graph_revision_id)
        )
        if run.graph_revision_id is not None and graph_revision is None:
            raise inconsistent_run()
        node_records = await self._session.scalars(
            select(NodeRunRecord)
            .where(NodeRunRecord.run_id == run_id)
            .order_by(NodeRunRecord.created_at, NodeRunRecord.id)
        )
        invocation_records = await self._session.scalars(
            select(CapabilityInvocationRecord)
            .where(CapabilityInvocationRecord.run_id == run_id)
            .order_by(CapabilityInvocationRecord.requested_at, CapabilityInvocationRecord.id)
        )
        artifact_records = await self._session.scalars(
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(ArtifactRecord.created_at, ArtifactRecord.id)
        )
        return ExecutionRunAggregate(
            task=task,
            run=run,
            graph_revision=graph_revision,
            nodes=tuple(node_domain(record) for record in node_records.all()),
            invocations=tuple(invocation_domain(record) for record in invocation_records.all()),
            checkpoints=await self.list_checkpoints(run_id),
            artifacts=tuple(artifact_domain(record) for record in artifact_records.all()),
            events=await self.list_events(run_id),
        )
