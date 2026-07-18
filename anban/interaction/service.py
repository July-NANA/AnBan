"""Interaction-to-Runtime mapping without Adapter or provider bypasses."""

from __future__ import annotations

from anban.core.ids import CheckpointId, ExecutionRunId, SessionId, TaskId
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.interaction.contracts import (
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.runtime import (
    ArtifactDetail,
    ContextDetail,
    ExecutionQueryService,
    ExecutionResult,
    PersistentChatSession,
    PersistentRuntime,
    RunDetail,
    RunObservability,
    RunSummary,
    WaitingExecution,
)


def interaction_metadata(envelope: InteractionEnvelope) -> SafeMetadata:
    values: dict[str, SafeScalar] = {
        "interaction_id": str(envelope.id),
        "source": envelope.source,
        "input_kind": envelope.input_kind.value,
        "interaction_route": envelope.correlation.route.value,
    }
    resume = envelope.correlation.resume_key
    if resume is not None:
        values.update(
            {
                "resume_namespace": resume.namespace,
                "resume_correlation_hash": resume.fingerprint,
            }
        )
    deduplication = envelope.correlation.deduplication_key
    if deduplication is not None:
        values.update(
            {
                "deduplication_namespace": deduplication.namespace,
                "deduplication_correlation_hash": deduplication.fingerprint,
            }
        )
    return SafeMetadata(values)


def require_existing_cli_path(envelope: InteractionEnvelope) -> None:
    """Fail closed until durable v0.5 routing and deduplication are implemented."""

    if (
        envelope.source != "cli"
        or envelope.input_kind is not InteractionInputKind.USER_MESSAGE
        or envelope.correlation.route is not InteractionRoute.NEW_TASK
        or envelope.correlation.keys
    ):
        raise RuntimeError("v0.5 Interaction routing is not configured")


class InteractionChatSession:
    """Map bounded CLI envelopes into one Runtime chat session."""

    def __init__(self, session: PersistentChatSession) -> None:
        self._session = session

    @property
    def can_continue(self) -> bool:
        return self._session.can_continue

    @property
    def session_id(self) -> SessionId:
        return self._session.session_id

    @property
    def remaining_seconds(self) -> float:
        return self._session.remaining_seconds

    async def submit(self, envelope: InteractionEnvelope) -> ExecutionResult:
        require_existing_cli_path(envelope)
        return await self._session.submit(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

    async def close(self) -> ExecutionResult | None:
        return await self._session.close()

    async def expire(self) -> ExecutionResult | None:
        return await self._session.expire()

    async def interrupt(self) -> ExecutionResult | None:
        return await self._session.interrupt()


class InteractionService:
    """The only CLI-facing entry into the v0.1 Runtime."""

    def __init__(
        self,
        runtime: PersistentRuntime | None,
        queries: ExecutionQueryService | None = None,
    ) -> None:
        self._runtime = runtime
        self._queries = queries

    async def submit(self, envelope: InteractionEnvelope) -> ExecutionResult:
        require_existing_cli_path(envelope)
        return await self._runtime_service().execute(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

    async def start_async(
        self, envelope: InteractionEnvelope
    ) -> WaitingExecution | ExecutionResult:
        require_existing_cli_path(envelope)
        return await self._runtime_service().start_async(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

    async def resume_async(self, checkpoint_id: CheckpointId) -> WaitingExecution | ExecutionResult:
        return await self._runtime_service().resume_async(checkpoint_id)

    async def cancel_async(self, checkpoint_id: CheckpointId) -> ExecutionResult:
        return await self._runtime_service().cancel_async(checkpoint_id)

    def chat(self) -> InteractionChatSession:
        return InteractionChatSession(self._runtime_service().chat())

    async def runs(self, limit: int = 20) -> tuple[RunSummary, ...]:
        return await self._query_service().list_runs(limit)

    async def show_run(self, run_id: ExecutionRunId) -> RunDetail:
        return await self._query_service().show(run_id)

    async def trace(self, run_id: ExecutionRunId) -> RunObservability:
        return await self._query_service().trace(run_id)

    async def artifacts(self, run_id: ExecutionRunId) -> tuple[ArtifactDetail, ...]:
        return await self._query_service().artifacts(run_id)

    async def task_context(self, task_id: TaskId) -> ContextDetail:
        return await self._query_service().task_context(task_id)

    async def session_context(self, session_id: SessionId) -> ContextDetail:
        return await self._query_service().session_context(session_id)

    def _query_service(self) -> ExecutionQueryService:
        if self._queries is None:
            raise RuntimeError("Runtime query service is not configured")
        return self._queries

    def _runtime_service(self) -> PersistentRuntime:
        if self._runtime is None:
            raise RuntimeError("Runtime execution service is not configured")
        return self._runtime
