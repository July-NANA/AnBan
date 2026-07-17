"""Bounded, safe Runtime query projections for CLI inspection."""

from __future__ import annotations

from pydantic import Field

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ExecutionRunId,
    NodeRunId,
    TaskId,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    NodeRunStatus,
    TaskStatus,
    UtcDateTime,
)
from anban.core.persistence import ExecutionRunAggregate, UnitOfWorkFactory
from anban.runtime.contracts import RuntimeValue
from anban.runtime.observability import RunObservability, project_observability

DEFAULT_RUN_LIMIT = 20
MAX_RUN_LIMIT = 100
MAX_NODES = 8
MAX_INVOCATIONS = 64
MAX_ARTIFACTS = 256
MAX_EVENTS = 512


class RunSummary(RuntimeValue):
    id: ExecutionRunId
    task_id: TaskId
    status: ExecutionRunStatus
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None


class TaskDetail(RuntimeValue):
    id: TaskId
    status: TaskStatus
    created_at: UtcDateTime
    error_code: ErrorCode | None = None


class NodeDetail(RuntimeValue):
    id: NodeRunId
    node_name: str
    status: NodeRunStatus
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None


class InvocationDetail(RuntimeValue):
    id: CapabilityInvocationId
    node_run_id: NodeRunId
    capability_name: str
    status: CapabilityInvocationStatus
    requested_at: UtcDateTime
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: ErrorCode | None = None


class ArtifactDetail(RuntimeValue):
    id: ArtifactId
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    created_at: UtcDateTime


class RunDetail(RuntimeValue):
    task: TaskDetail
    run: RunSummary
    nodes: tuple[NodeDetail, ...] = Field(max_length=MAX_NODES)
    invocations: tuple[InvocationDetail, ...] = Field(max_length=MAX_INVOCATIONS)
    artifacts: tuple[ArtifactDetail, ...] = Field(max_length=MAX_ARTIFACTS)
    final_text: str | None = None
    observability: RunObservability


class ExecutionQueryService:
    """Load authoritative PostgreSQL state through the Core persistence Port."""

    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def list_runs(self, limit: int = DEFAULT_RUN_LIMIT) -> tuple[RunSummary, ...]:
        if not 1 <= limit <= MAX_RUN_LIMIT:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="Run list limit must be between 1 and 100",
                )
            )
        try:
            async with self._unit_of_work() as unit:
                runs = await unit.executions.list_runs(limit)
        except AnbanError:
            raise
        except Exception:
            raise query_failure() from None
        return tuple(run_summary(run) for run in runs)

    async def show(self, run_id: ExecutionRunId) -> RunDetail:
        aggregate = await self._load(run_id)
        enforce_aggregate_bounds(aggregate)
        return RunDetail(
            task=TaskDetail(
                id=aggregate.task.id,
                status=aggregate.task.status,
                created_at=aggregate.task.created_at,
                error_code=aggregate.task.error_code,
            ),
            run=run_summary(aggregate.run),
            nodes=tuple(node_detail(node) for node in aggregate.nodes),
            invocations=tuple(
                invocation_detail(invocation) for invocation in aggregate.invocations
            ),
            artifacts=tuple(artifact_detail(artifact) for artifact in aggregate.artifacts),
            final_text=aggregate.run.final_text,
            observability=project_observability(aggregate),
        )

    async def trace(self, run_id: ExecutionRunId) -> RunObservability:
        aggregate = await self._load(run_id)
        enforce_aggregate_bounds(aggregate)
        return project_observability(aggregate)

    async def artifacts(self, run_id: ExecutionRunId) -> tuple[ArtifactDetail, ...]:
        aggregate = await self._load(run_id)
        enforce_aggregate_bounds(aggregate)
        return tuple(artifact_detail(artifact) for artifact in aggregate.artifacts)

    async def _load(self, run_id: ExecutionRunId) -> ExecutionRunAggregate:
        try:
            async with self._unit_of_work() as unit:
                aggregate = await unit.executions.load_run(run_id)
        except AnbanError:
            raise
        except Exception:
            raise query_failure() from None
        if aggregate is None:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="Run does not exist",
                    details=SafeMetadata({"run_id": str(run_id)}),
                )
            )
        return aggregate


def enforce_aggregate_bounds(aggregate: ExecutionRunAggregate) -> None:
    if (
        len(aggregate.nodes) > MAX_NODES
        or len(aggregate.invocations) > MAX_INVOCATIONS
        or len(aggregate.artifacts) > MAX_ARTIFACTS
        or len(aggregate.events) > MAX_EVENTS
    ):
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Run inspection exceeds its bounded limit",
            )
        )


def run_summary(run: ExecutionRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        task_id=run.task_id,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
    )


def node_detail(node: NodeRun) -> NodeDetail:
    return NodeDetail(
        id=node.id,
        node_name=node.node_name,
        status=node.status,
        created_at=node.created_at,
        started_at=node.started_at,
        finished_at=node.finished_at,
        error_code=node.error_code,
    )


def invocation_detail(invocation: CapabilityInvocation) -> InvocationDetail:
    return InvocationDetail(
        id=invocation.id,
        node_run_id=invocation.node_run_id,
        capability_name=invocation.capability_name,
        status=invocation.status,
        requested_at=invocation.requested_at,
        started_at=invocation.started_at,
        finished_at=invocation.finished_at,
        error_code=invocation.error_code,
    )


def artifact_detail(artifact: Artifact) -> ArtifactDetail:
    return ArtifactDetail(
        id=artifact.id,
        node_run_id=artifact.node_run_id,
        invocation_id=artifact.invocation_id,
        uri=artifact.uri,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        created_at=artifact.created_at,
    )


def query_failure() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_UNAVAILABLE,
            message="Run inspection data is unavailable",
        )
    )
