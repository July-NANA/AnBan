"""Bounded, safe Runtime query projections for CLI inspection."""

from __future__ import annotations

import hashlib

from pydantic import Field

from anban.core.context import (
    ContextConflictState,
    ContextEntry,
    ContextEntryKind,
    ContextScope,
    ContextSensitivity,
    ContextSourceKind,
    ContextSummary,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.graph import GraphRevision, GraphRevisionStatus, TaskGraphSpec
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    ContextEntryId,
    ContextSummaryId,
    ExecutionRunId,
    GraphRevisionId,
    NodeRunId,
    SessionId,
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
    now_utc,
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
MAX_CONTEXT_ENTRIES = 512
MAX_CONTEXT_SUMMARIES = 64


class RunSummary(RuntimeValue):
    id: ExecutionRunId
    task_id: TaskId
    status: ExecutionRunStatus
    graph_revision_id: GraphRevisionId | None = None
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


class GraphRevisionDetail(RuntimeValue):
    id: GraphRevisionId
    task_id: TaskId
    previous_revision_id: GraphRevisionId | None = None
    reason: str
    spec: TaskGraphSpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: GraphRevisionStatus
    created_at: UtcDateTime


class RunDetail(RuntimeValue):
    task: TaskDetail
    run: RunSummary
    graph_revision: GraphRevisionDetail | None = None
    nodes: tuple[NodeDetail, ...] = Field(max_length=MAX_NODES)
    invocations: tuple[InvocationDetail, ...] = Field(max_length=MAX_INVOCATIONS)
    artifacts: tuple[ArtifactDetail, ...] = Field(max_length=MAX_ARTIFACTS)
    final_text: str | None = None
    observability: RunObservability


class ContextEntryDetail(RuntimeValue):
    id: ContextEntryId
    kind: ContextEntryKind
    sensitivity: ContextSensitivity
    state: ContextConflictState
    source_kind: ContextSourceKind
    source_reference_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_chars: int = Field(ge=1, le=8192)
    supersedes: ContextEntryId | None = None
    conflicts_with: ContextEntryId | None = None
    created_at: UtcDateTime
    expires_at: UtcDateTime | None = None


class ContextSummaryDetail(RuntimeValue):
    id: ContextSummaryId
    covered_entry_ids: tuple[ContextEntryId, ...] = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_chars: int = Field(ge=1, le=8192)
    created_at: UtcDateTime


class ContextDetail(RuntimeValue):
    scope: ContextScope
    identity: str = Field(min_length=36, max_length=36)
    entries: tuple[ContextEntryDetail, ...] = Field(max_length=MAX_CONTEXT_ENTRIES)
    summaries: tuple[ContextSummaryDetail, ...] = Field(max_length=MAX_CONTEXT_SUMMARIES)
    active_entry_count: int = Field(ge=0, le=MAX_CONTEXT_ENTRIES)
    active_chars: int = Field(ge=0, le=131_072)


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
            graph_revision=(
                None
                if aggregate.graph_revision is None
                else graph_revision_detail(aggregate.graph_revision)
            ),
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

    async def task_context(self, task_id: TaskId) -> ContextDetail:
        return await self._context(ContextScope.TASK, task_id)

    async def session_context(self, session_id: SessionId) -> ContextDetail:
        return await self._context(ContextScope.SESSION, session_id)

    async def _context(self, scope: ContextScope, identity: TaskId | SessionId) -> ContextDetail:
        try:
            async with self._unit_of_work() as unit:
                if (
                    scope is ContextScope.TASK
                    and await unit.executions.get_task(TaskId(identity)) is None
                ):
                    raise context_missing()
                entries = await unit.executions.list_context_entries(scope, identity)
                summaries = await unit.executions.list_context_summaries(scope, identity)
        except AnbanError:
            raise
        except Exception:
            raise query_failure() from None
        if scope is ContextScope.SESSION and not entries and not summaries:
            raise context_missing()
        if len(entries) > MAX_CONTEXT_ENTRIES or len(summaries) > MAX_CONTEXT_SUMMARIES:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="Context inspection exceeds its bounded limit",
                )
            )
        active = tuple(
            entry
            for entry in entries
            if entry.state in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
            and (entry.expires_at is None or entry.expires_at > now_utc())
        )
        return ContextDetail(
            scope=scope,
            identity=str(identity),
            entries=tuple(context_entry_detail(entry) for entry in entries),
            summaries=tuple(context_summary_detail(summary) for summary in summaries),
            active_entry_count=len(active),
            active_chars=sum(len(entry.content) for entry in active),
        )

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
        graph_revision_id=run.graph_revision_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
    )


def graph_revision_detail(revision: GraphRevision) -> GraphRevisionDetail:
    return GraphRevisionDetail(
        id=revision.id,
        task_id=revision.task_id,
        previous_revision_id=revision.previous_revision_id,
        reason=revision.reason,
        spec=revision.spec,
        spec_hash=revision.spec_hash,
        status=revision.status,
        created_at=revision.created_at,
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


def context_entry_detail(entry: ContextEntry) -> ContextEntryDetail:
    return ContextEntryDetail(
        id=entry.id,
        kind=entry.kind,
        sensitivity=entry.sensitivity,
        state=entry.state,
        source_kind=entry.source.kind,
        source_reference_hash=hashlib.sha256(entry.source.reference.encode()).hexdigest(),
        content_hash=hashlib.sha256(entry.content.encode()).hexdigest(),
        content_chars=len(entry.content),
        supersedes=entry.supersedes,
        conflicts_with=entry.conflicts_with,
        created_at=entry.created_at,
        expires_at=entry.expires_at,
    )


def context_summary_detail(summary: ContextSummary) -> ContextSummaryDetail:
    return ContextSummaryDetail(
        id=summary.id,
        covered_entry_ids=summary.covered_entry_ids,
        content_hash=hashlib.sha256(summary.content.encode()).hexdigest(),
        content_chars=len(summary.content),
        created_at=summary.created_at,
    )


def context_missing() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.VALIDATION_FAILED,
            message="Context does not exist",
        )
    )


def query_failure() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_UNAVAILABLE,
            message="Run inspection data is unavailable",
        )
    )
