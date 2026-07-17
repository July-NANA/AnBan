"""Focused SQLAlchemy implementation of the v0.1 execution Repository Port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    EventId,
    ExecutionRunId,
    NodeRunId,
    TaskId,
)
from anban.core.lifecycle import (
    ensure_capability_invocation_transition,
    ensure_execution_run_transition,
    ensure_node_run_transition,
    ensure_task_transition,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
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
    event_domain,
    event_record,
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
    EventRecord,
    ExecutionRunRecord,
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
        record.started_at = run.started_at
        record.finished_at = run.finished_at
        record.final_text = run.final_text
        record.error_code = None if run.error_code is None else run.error_code.value
        record.safe_metadata = dict(run.metadata.root)

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

    async def add_artifact(self, artifact: Artifact) -> None:
        self._session.add(artifact_record(artifact))
        await self._session.flush()

    async def get_artifact(self, artifact_id: ArtifactId) -> Artifact | None:
        record = await self._session.get(ArtifactRecord, artifact_id)
        return None if record is None else artifact_domain(record)

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
            nodes=tuple(node_domain(record) for record in node_records.all()),
            invocations=tuple(invocation_domain(record) for record in invocation_records.all()),
            artifacts=tuple(artifact_domain(record) for record in artifact_records.all()),
            events=await self.list_events(run_id),
        )
