"""Interaction-to-Runtime mapping without Adapter or provider bypasses."""

from __future__ import annotations

from anban.core.ids import ExecutionRunId, SessionId, TaskId
from anban.core.metadata import SafeMetadata
from anban.interaction.contracts import InteractionEnvelope
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
)


def interaction_metadata(envelope: InteractionEnvelope) -> SafeMetadata:
    return SafeMetadata(
        {
            "interaction_id": str(envelope.id),
            "source": envelope.source,
        }
    )


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
        return await self._runtime_service().execute(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

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
