"""Interaction-to-Runtime mapping without Adapter or provider bypasses."""

from __future__ import annotations

from uuid import uuid4

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CheckpointId, ExecutionRunId, InteractionId, SessionId, TaskId
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.core.persistence import UnitOfWorkFactory
from anban.interaction.contracts import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.interaction.inbox import InteractionInboxCoordinator, InteractionInboxDetail
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

_CONTINUATION_NAMESPACE = "anban.continuation"
_HUMAN_RESUME_INPUTS = frozenset(
    {
        InteractionInputKind.USER_MESSAGE,
        InteractionInputKind.SUPPLEMENTAL_INPUT,
        InteractionInputKind.HUMAN_INPUT,
    }
)
_RESULT_RESUME_INPUTS = frozenset(
    {
        InteractionInputKind.ASYNC_CAPABILITY_RESULT,
        InteractionInputKind.MCP_RESULT,
        InteractionInputKind.SUBAGENT_RESULT,
    }
)


class CorrelatedWaitingExecution(WaitingExecution):
    """Waiting projection carrying one opaque external resume correlation."""

    resume_key: CorrelationKey


def interaction_metadata(
    envelope: InteractionEnvelope, *, inbox_managed: bool = False
) -> SafeMetadata:
    values: dict[str, SafeScalar] = {
        "interaction_id": str(envelope.id),
        "source": envelope.source,
        "input_kind": envelope.input_kind.value,
        "interaction_route": envelope.correlation.route.value,
        "inbox_managed": inbox_managed,
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


def require_new_work_route(envelope: InteractionEnvelope) -> None:
    """Accept supported new work without granting later delivery semantics."""

    if envelope.correlation.route is not InteractionRoute.NEW_TASK:
        raise _routing_error("route_mismatch")
    if envelope.input_kind is not InteractionInputKind.USER_MESSAGE:
        raise _routing_error("new_work_input_unavailable")


def _routing_error(reason: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.VALIDATION_FAILED,
            message="Interaction input cannot use the requested route",
            details=SafeMetadata({"reason": reason}),
        )
    )


class InteractionChatSession:
    """Map bounded CLI envelopes into one Runtime chat session."""

    def __init__(
        self,
        session: PersistentChatSession,
        inbox: InteractionInboxCoordinator | None = None,
    ) -> None:
        self._session = session
        self._inbox = inbox

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
        if self._inbox is not None:
            duplicate = await self._inbox.admit(envelope)
            if duplicate is not None:
                return duplicate
        try:
            require_new_work_route(envelope)
            result = await self._session.submit(
                envelope.content,
                metadata=interaction_metadata(envelope, inbox_managed=self._inbox is not None),
            )
        except AnbanError as exc:
            if self._inbox is not None:
                await self._inbox.reject(envelope.id, _error_reason(exc), error_code=exc.info.code)
            raise
        if self._inbox is not None:
            await _finish_delivery(self._inbox, envelope.id, result)
        return result

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
        unit_of_work: UnitOfWorkFactory | None = None,
    ) -> None:
        self._runtime = runtime
        self._queries = queries
        self._inbox = None if unit_of_work is None else InteractionInboxCoordinator(unit_of_work)

    async def submit(self, envelope: InteractionEnvelope) -> ExecutionResult:
        duplicate = await self._admit(envelope)
        if duplicate is not None:
            return duplicate
        try:
            if envelope.correlation.route is InteractionRoute.NEW_TASK:
                require_new_work_route(envelope)
                result = await self._runtime_service().execute(
                    envelope.content,
                    metadata=interaction_metadata(envelope, inbox_managed=self._inbox is not None),
                )
            else:
                result = await self._submit_resume(envelope)
        except AnbanError as exc:
            await self._reject(envelope.id, exc)
            raise
        if self._inbox is not None:
            await _finish_delivery(self._inbox, envelope.id, result)
            if envelope.correlation.route is InteractionRoute.RESUME_ELIGIBLE_RUN:
                await self._inbox.complete_origin(result)
        return result

    async def start_async(
        self, envelope: InteractionEnvelope
    ) -> CorrelatedWaitingExecution | ExecutionResult:
        duplicate = await self._admit(envelope)
        if duplicate is not None:
            return duplicate
        try:
            require_new_work_route(envelope)
            result = await self._runtime_service().start_async(
                envelope.content,
                metadata=interaction_metadata(envelope, inbox_managed=self._inbox is not None),
            )
        except AnbanError as exc:
            await self._reject(envelope.id, exc)
            raise
        if isinstance(result, ExecutionResult) and self._inbox is not None:
            await _finish_delivery(self._inbox, envelope.id, result)
        return await self._correlate_waiting(result)

    async def resume_async(
        self, checkpoint_id: CheckpointId
    ) -> CorrelatedWaitingExecution | ExecutionResult:
        result = await self._runtime_service().resume_async(checkpoint_id)
        if isinstance(result, ExecutionResult) and self._inbox is not None:
            await self._inbox.complete_origin(result)
        return await self._correlate_waiting(result)

    async def cancel_async(self, checkpoint_id: CheckpointId) -> ExecutionResult:
        result = await self._runtime_service().cancel_async(checkpoint_id)
        if self._inbox is not None:
            await self._inbox.complete_origin(result)
        return result

    async def detach_async(self, checkpoint_id: CheckpointId) -> None:
        await self._runtime_service().detach_async(checkpoint_id)

    async def _correlate_waiting(
        self,
        result: WaitingExecution | ExecutionResult,
    ) -> CorrelatedWaitingExecution | ExecutionResult:
        if not isinstance(result, WaitingExecution):
            return result
        key = CorrelationKey(
            purpose=CorrelationPurpose.RESUME,
            namespace=_CONTINUATION_NAMESPACE,
            value=uuid4().hex,
        )
        await self._runtime_service().bind_resume_correlation(
            result.checkpoint_id,
            key.namespace,
            key.fingerprint,
        )
        return CorrelatedWaitingExecution(
            **result.model_dump(),
            resume_key=key,
        )

    async def _submit_resume(self, envelope: InteractionEnvelope) -> ExecutionResult:
        correlation = envelope.correlation
        if (
            correlation.route is not InteractionRoute.RESUME_ELIGIBLE_RUN
            or correlation.resume_key is None
        ):
            raise _routing_error("malformed")
        if envelope.input_kind not in _HUMAN_RESUME_INPUTS | _RESULT_RESUME_INPUTS:
            raise _routing_error("resume_input_unavailable")
        key = correlation.resume_key
        checkpoint_id = await self._runtime_service().resolve_resume_correlation(
            key.namespace,
            key.fingerprint,
        )
        return await self._runtime_service().apply_interaction_update(
            checkpoint_id,
            envelope.content,
            envelope.id,
            interaction_metadata(envelope, inbox_managed=self._inbox is not None),
            envelope.received_at,
        )

    def chat(self) -> InteractionChatSession:
        return InteractionChatSession(self._runtime_service().chat(), self._inbox)

    async def inbox(self, limit: int = 20) -> tuple[InteractionInboxDetail, ...]:
        if self._inbox is None:
            raise RuntimeError("Interaction inbox is not configured")
        return await self._inbox.list(limit)

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

    async def _admit(self, envelope: InteractionEnvelope) -> ExecutionResult | None:
        if self._inbox is None:
            return None
        return await self._inbox.admit(envelope)

    async def _reject(self, interaction_id: InteractionId, error: AnbanError) -> None:
        if self._inbox is not None:
            await self._inbox.reject(
                interaction_id,
                _error_reason(error),
                error_code=error.info.code,
            )


def _error_reason(error: AnbanError) -> str:
    reason = error.info.details.root.get("reason")
    return reason if isinstance(reason, str) else error.info.code.value


async def _finish_delivery(
    inbox: InteractionInboxCoordinator,
    interaction_id: InteractionId,
    result: ExecutionResult,
) -> None:
    if result.persisted:
        await inbox.complete(interaction_id, result)
        return
    await inbox.reject(
        interaction_id,
        "execution_not_persisted",
        error_code=(
            ErrorCode.PERSISTENCE_WRITE_FAILED
            if result.outcome.error is None
            else result.outcome.error.code
        ),
    )
