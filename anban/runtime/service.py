"""Persistent v0.1 Runtime orchestration over authoritative Ports."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityInventoryPort,
    CapabilityPort,
    CapabilityResult,
    DelegateExecutionHandle,
    DelegateRunOutcome,
    InvocationContext,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.graph import GraphRevision
from anban.core.ids import (
    CapabilityInvocationId,
    CheckpointId,
    ExecutionRunId,
    InteractionId,
    NodeRunId,
    SessionId,
    TaskId,
    new_execution_run_id,
    new_node_run_id,
    new_session_id,
    new_task_id,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    Task,
    UtcDateTime,
    now_utc,
)
from anban.core.persistence import UnitOfWorkFactory
from anban.model import ModelPort
from anban.runtime.agent import FixedGeneralAgent
from anban.runtime.capability_persistence import PersistedCapabilityPort
from anban.runtime.completion import CompletionEvaluator
from anban.runtime.continuation import (
    ContinuationControl,
    ContinuationManager,
    ContinuationResult,
)
from anban.runtime.contracts import (
    AgentInput,
    AgentLimits,
    AgentOutcome,
    AgentOutcomeStatus,
    ExecutionResult,
    WaitingExecution,
)
from anban.runtime.failure_outcomes import (
    matches_terminal,
    recover_run_terminal,
    recover_terminal,
    routing_failure_outcome,
    storage_failure_outcome,
)
from anban.runtime.graph_execution import TaskGraphExecutor
from anban.runtime.graph_routing import (
    TASK_REQUEST_INPUT,
    TaskExecutionRoute,
    TaskRouteEvaluator,
)
from anban.runtime.graph_task_runtime import PersistentGraphTaskRunner
from anban.runtime.model_persistence import PersistedModelPort
from anban.runtime.persistence import RunPersistence
from anban.runtime.recovery import RuntimeRecovery
from anban.runtime.sufficiency import CapabilitySufficiencyEvaluator
from anban.runtime.update_service import RESULT_INPUT_KINDS, RuntimeUpdateService


class PersistentRuntime:
    """Route, execute, and durably finalize one fixed-Agent or Task-graph Run."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        unit_of_work: UnitOfWorkFactory,
        *,
        inventory: CapabilityInventoryPort | None = None,
        sufficiency: CapabilitySufficiencyEvaluator | None = None,
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
        artifact_cleanup: Callable[[InvocationContext, ArtifactReference], None] | None = None,
        route_evaluator: TaskRouteEvaluator | None = None,
        graph_executor: TaskGraphExecutor | None = None,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._unit_of_work = unit_of_work
        self._inventory = inventory
        self._sufficiency = sufficiency
        self._limits = limits
        self._response_repair_retries = response_repair_retries
        self._artifact_cleanup = artifact_cleanup
        self._route_evaluator = route_evaluator
        self._graph_executor = graph_executor or TaskGraphExecutor()
        self._continuations = ContinuationManager()
        self._updates = RuntimeUpdateService(
            model, capabilities, unit_of_work, response_repair_retries=response_repair_retries
        )

    @property
    def inventory(self) -> CapabilityInventoryPort:
        if self._inventory is None:
            raise RuntimeError("Capability inventory is not configured")
        return self._inventory

    @property
    def sufficiency(self) -> CapabilitySufficiencyEvaluator:
        if self._sufficiency is None:
            raise RuntimeError("Capability sufficiency evaluator is not configured")
        return self._sufficiency

    async def execute(
        self, request: str, *, metadata: SafeMetadata | None = None
    ) -> ExecutionResult:
        return await self._execute(request, metadata=metadata)

    async def start_child(
        self,
        objective: str,
        parent_run_id: ExecutionRunId,
        parent_invocation_id: CapabilityInvocationId,
        delegation_depth: int,
    ) -> DelegateExecutionHandle:
        task_id = new_task_id()
        run_id = new_execution_run_id()
        node_run_id = new_node_run_id()
        initialized = asyncio.get_running_loop().create_future()

        async def execute() -> DelegateRunOutcome:
            result = await self._execute(
                objective,
                task_id=task_id,
                run_id=run_id,
                node_run_id=node_run_id,
                parent_run_id=parent_run_id,
                parent_invocation_id=parent_invocation_id,
                delegation_depth=delegation_depth,
                initialized=initialized,
            )
            return DelegateRunOutcome(
                task_id=result.task_id,
                run_id=result.run_id,
                node_run_id=result.node_run_id,
                status=ExecutionRunStatus(result.outcome.status.value),
                final_text=result.outcome.final_text,
                error=result.outcome.error,
                artifact_count=len(result.outcome.artifacts),
                delegation_depth=delegation_depth,
            )

        completion = asyncio.create_task(execute())
        if not await initialized:
            outcome = await completion
            raise AnbanError(
                outcome.error
                or ErrorInfo(
                    code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                    message="Delegated child Run could not be created",
                )
            )
        return DelegateExecutionHandle(task_id, run_id, node_run_id, completion)

    async def start_async(
        self, request: str, *, metadata: SafeMetadata | None = None
    ) -> ContinuationResult:
        return await self._continuations.start(
            lambda control: self._execute(request, metadata=metadata, continuation=control)
        )

    async def resume_async(self, checkpoint_id: CheckpointId) -> ContinuationResult:
        if self._continuations.contains(checkpoint_id):
            return await self._continuations.resume(checkpoint_id)
        return await self._recovery().resume(checkpoint_id)

    async def cancel_async(self, checkpoint_id: CheckpointId) -> ExecutionResult:
        if self._continuations.contains(checkpoint_id):
            return await self._continuations.cancel(checkpoint_id)
        return await self._recovery().resume(checkpoint_id, cancel=True)

    async def detach_async(self, checkpoint_id: CheckpointId) -> None:
        await self._continuations.abandon(checkpoint_id)

    async def bind_resume_correlation(
        self, checkpoint_id: CheckpointId, namespace: str, fingerprint: str
    ) -> None:
        await self._updates.bind_resume(checkpoint_id, namespace, fingerprint)

    async def resolve_resume_correlation(self, namespace: str, fingerprint: str) -> CheckpointId:
        return await self._updates.resolve_resume(namespace, fingerprint)

    async def apply_interaction_update(
        self,
        checkpoint_id: CheckpointId,
        content: str,
        interaction_id: InteractionId,
        metadata: SafeMetadata,
        received_at: UtcDateTime,
    ) -> ExecutionResult:
        await self._updates.apply(checkpoint_id, content, interaction_id, metadata, received_at)
        if self._continuations.contains(checkpoint_id):
            if metadata.root.get("input_kind") in RESULT_INPUT_KINDS:
                current = await self._continuations.resume(checkpoint_id)
                while isinstance(current, WaitingExecution):
                    current = await self._continuations.resume(current.checkpoint_id)
                return current
            await self._continuations.abandon(checkpoint_id)
        return await self._recovery().resume(checkpoint_id)

    def _recovery(self) -> RuntimeRecovery:
        return RuntimeRecovery(
            self._model,
            self._capabilities,
            self._unit_of_work,
            self._sufficiency,
            artifact_cleanup=self._artifact_cleanup,
            response_repair_retries=self._response_repair_retries,
        )

    async def _execute(
        self,
        request: str,
        *,
        metadata: SafeMetadata | None = None,
        continuation: ContinuationControl | None = None,
        task_id: TaskId | None = None,
        run_id: ExecutionRunId | None = None,
        node_run_id: NodeRunId | None = None,
        parent_run_id: ExecutionRunId | None = None,
        parent_invocation_id: CapabilityInvocationId | None = None,
        delegation_depth: int = 0,
        initialized: asyncio.Future[bool] | None = None,
    ) -> ExecutionResult:
        safe_metadata = metadata or SafeMetadata()
        delegated = parent_run_id is not None or parent_invocation_id is not None
        if delegated:
            if parent_run_id is None or parent_invocation_id is None:
                raise ValueError("delegated child identity must be complete")
            safe_metadata = SafeMetadata(
                {
                    "delegation_depth": delegation_depth,
                    "objective_hash": hashlib.sha256(request.encode()).hexdigest(),
                    "parent_invocation_id": str(parent_invocation_id),
                    "parent_run_id": str(parent_run_id),
                }
            )
        task = Task(id=task_id or new_task_id(), request=request, metadata=safe_metadata)
        run = ExecutionRun(
            id=run_id or new_execution_run_id(),
            task_id=task.id,
            parent_run_id=parent_run_id,
            parent_invocation_id=parent_invocation_id,
            delegation_depth=delegation_depth,
            metadata=safe_metadata,
        )
        node = NodeRun(
            id=node_run_id or new_node_run_id(),
            run_id=run.id,
            node_name="general_agent",
            metadata=safe_metadata,
        )
        persistence = RunPersistence(self._unit_of_work, task, run, node)
        try:
            await persistence.initialize()
            if initialized is not None and not initialized.done():
                initialized.set_result(True)
            await persistence.start()
        except AnbanError as exc:
            if initialized is not None and not initialized.done():
                initialized.set_result(False)
            outcome = storage_failure_outcome(exc.info, stage="setup")
            persisted = await recover_terminal(persistence, outcome)
            return ExecutionResult(
                task_id=task.id,
                run_id=run.id,
                node_run_id=node.id,
                outcome=outcome,
                persisted=persisted,
            )
        finally:
            if initialized is not None and not initialized.done():
                initialized.set_result(False)

        persisted_model = PersistedModelPort(self._model, persistence)
        graph_execution = False
        if self._route_evaluator is not None:
            try:
                decision = await self._route_evaluator.decide(
                    request,
                    persisted_model,
                    repair_limit=self._response_repair_retries,
                )
                revision = (
                    None
                    if decision.graph_spec is None
                    else GraphRevision.create(
                        task_id=task.id,
                        reason="Initial validated Main Agent graph route.",
                        spec=decision.graph_spec,
                    )
                )
                await persistence.record_task_route(
                    decision.route.value,
                    rationale_hash=hashlib.sha256(decision.rationale.encode()).hexdigest(),
                    revision=revision,
                )
            except AnbanError as exc:
                outcome = routing_failure_outcome(exc.info, persisted_model.turn_count)
                persisted = await recover_terminal(persistence, outcome)
                return ExecutionResult(
                    task_id=task.id,
                    run_id=run.id,
                    node_run_id=node.id,
                    outcome=outcome,
                    persisted=persisted,
                )
            except (ValueError, TypeError):
                outcome = routing_failure_outcome(
                    ErrorInfo(
                        code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                        message="Task route persistence failed",
                    ),
                    persisted_model.turn_count,
                )
                persisted = await recover_terminal(persistence, outcome)
                return ExecutionResult(
                    task_id=task.id,
                    run_id=run.id,
                    node_run_id=node.id,
                    outcome=outcome,
                    persisted=persisted,
                )
            if decision.route is TaskExecutionRoute.TASK_GRAPH:
                graph_execution = True
                planning_outcome = AgentOutcome(
                    status=AgentOutcomeStatus.SUCCEEDED,
                    final_text="Validated Task graph route selected.",
                    model_turn_count=decision.model_turn_count,
                    capability_call_count=0,
                )
                try:
                    await persistence.finish_node(planning_outcome)
                except AnbanError as exc:
                    outcome = storage_failure_outcome(
                        exc.info,
                        stage="graph_planning_finalize",
                        model_turn_count=decision.model_turn_count,
                    )
                    persisted = await recover_run_terminal(persistence, outcome)
                    return ExecutionResult(
                        task_id=task.id,
                        run_id=run.id,
                        node_run_id=node.id,
                        outcome=outcome,
                        persisted=persisted,
                    )
                spec = decision.graph_spec
                if spec is None:
                    raise RuntimeError("validated graph route lost its spec")
                runner = PersistentGraphTaskRunner(
                    persisted_model,
                    self._capabilities,
                    persistence,
                    self._graph_executor,
                    sufficiency=self._sufficiency,
                    limits=self._limits,
                    response_repair_retries=self._response_repair_retries,
                    artifact_cleanup=self._artifact_cleanup,
                    metadata=safe_metadata,
                    continuation_waiter=self._continuation_waiter(continuation, persistence),
                    checkpoint_background=continuation is not None,
                )
                graph_input: dict[str, JsonValue] = (
                    {TASK_REQUEST_INPUT: request}
                    if spec.input_keys == (TASK_REQUEST_INPUT,)
                    else {}
                )
                outcome = await runner.execute(
                    spec,
                    graph_input,
                    routing_model_turns=decision.model_turn_count,
                )
            else:
                outcome = await self._fixed_agent(
                    persisted_model, persistence, continuation
                ).execute(AgentInput(request=request, run_id=run.id, node_run_id=node.id))
                outcome = outcome.model_copy(
                    update={
                        "model_turn_count": outcome.model_turn_count + decision.model_turn_count
                    }
                )
        else:
            outcome = await self._fixed_agent(persisted_model, persistence, continuation).execute(
                AgentInput(request=request, run_id=run.id, node_run_id=node.id)
            )
        try:
            if graph_execution:
                await persistence.finish_run(outcome)
            else:
                await persistence.finish(outcome)
        except AnbanError as exc:
            if await matches_terminal(persistence, outcome):
                persisted = True
            else:
                outcome = storage_failure_outcome(
                    exc.info,
                    stage="finalize",
                    model_turn_count=outcome.model_turn_count,
                    capability_call_count=outcome.capability_call_count,
                    artifacts=outcome.artifacts,
                )
                persisted = await (
                    recover_run_terminal(persistence, outcome)
                    if graph_execution
                    else recover_terminal(persistence, outcome)
                )
        else:
            persisted = True
        return ExecutionResult(
            task_id=task.id,
            run_id=run.id,
            node_run_id=node.id,
            outcome=outcome,
            persisted=persisted,
        )

    def _fixed_agent(
        self,
        model: ModelPort,
        persistence: RunPersistence,
        continuation: ContinuationControl | None = None,
    ) -> FixedGeneralAgent:
        return FixedGeneralAgent(
            model,
            PersistedCapabilityPort(
                self._capabilities,
                persistence,
                artifact_cleanup=self._artifact_cleanup,
                checkpoint_background=continuation is not None,
            ),
            sufficiency=self._sufficiency,
            completion=(CompletionEvaluator() if self._sufficiency is not None else None),
            assessment_observer=persistence.agent_sufficiency_assessed,
            observation_observer=persistence.agent_observed,
            completion_observer=persistence.agent_completion_assessed,
            replan_observer=persistence.agent_replan_decided,
            continuation_waiter=self._continuation_waiter(continuation, persistence),
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
        )

    @staticmethod
    def _continuation_waiter(
        continuation: ContinuationControl | None,
        persistence: RunPersistence,
    ) -> Callable[[InvocationContext, CapabilityResult], Awaitable[None]] | None:
        if continuation is None:
            return None

        async def wait(context: InvocationContext, result: CapabilityResult) -> None:
            await continuation.pause(context, result, persistence)

        return wait

    def chat(self) -> PersistentChatSession:
        return PersistentChatSession(
            self._model,
            self._capabilities,
            self._unit_of_work,
            sufficiency=self._sufficiency,
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
            artifact_cleanup=self._artifact_cleanup,
        )


class PersistentChatSession:
    """One bounded in-process chat mapped to one Task/Run and one Node per input."""

    max_user_inputs = 8
    timeout = timedelta(minutes=15)

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        unit_of_work: UnitOfWorkFactory,
        *,
        sufficiency: CapabilitySufficiencyEvaluator | None = None,
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
        artifact_cleanup: Callable[[InvocationContext, ArtifactReference], None] | None = None,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._unit_of_work = unit_of_work
        self._sufficiency = sufficiency
        self._limits = limits
        self._response_repair_retries = response_repair_retries
        self._artifact_cleanup = artifact_cleanup
        self._started_at = now_utc()
        self._session_id = new_session_id()
        self._persistence: RunPersistence | None = None
        self._history: list[tuple[str, str]] = []
        self._last_outcome: AgentOutcome | None = None
        self._model_turn_count = 0
        self._capability_call_count = 0
        self._artifact_count = 0
        self._closed = False
        self._terminal_result: ExecutionResult | None = None

    @property
    def user_input_count(self) -> int:
        return len(self._history)

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def remaining_seconds(self) -> float:
        remaining = self.timeout - (now_utc() - self._started_at)
        return max(0.0, remaining.total_seconds())

    @property
    def can_continue(self) -> bool:
        return (
            not self._closed
            and self.user_input_count < self.max_user_inputs
            and self.remaining_seconds > 0
        )

    async def submit(
        self, request: str, *, metadata: SafeMetadata | None = None
    ) -> ExecutionResult:
        if not self.can_continue:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="Chat session limit was reached",
                )
            )
        agent_request = self._conversation_request(request)
        supplied_metadata = metadata or SafeMetadata()
        safe_metadata = SafeMetadata(
            {**supplied_metadata.root, "session_id": str(self._session_id)}
        )
        persistence = self._persistence
        if persistence is None:
            task = Task(id=new_task_id(), request=request, metadata=safe_metadata)
            run = ExecutionRun(id=new_execution_run_id(), task_id=task.id, metadata=safe_metadata)
            node = NodeRun(
                id=new_node_run_id(),
                run_id=run.id,
                node_name="general_agent",
                metadata=safe_metadata,
            )
            persistence = RunPersistence(self._unit_of_work, task, run, node)
            self._persistence = persistence
            try:
                await persistence.initialize()
                await persistence.start()
            except AnbanError as exc:
                outcome = storage_failure_outcome(exc.info, stage="chat_setup")
                persisted = await recover_terminal(persistence, outcome)
                return self._stop(persistence, outcome, persisted)
        else:
            node = NodeRun(
                id=new_node_run_id(),
                run_id=persistence.run.id,
                node_name="general_agent",
                metadata=safe_metadata,
            )
            try:
                await persistence.add_node(node)
                await persistence.start_node()
            except (AnbanError, ValueError) as exc:
                cause = (
                    exc.info
                    if isinstance(exc, AnbanError)
                    else ErrorInfo(
                        code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                        message="Runtime persistence failed",
                    )
                )
                outcome = storage_failure_outcome(cause, stage="chat_node_setup")
                persisted = await self._finish_run_failure(persistence, outcome)
                return self._stop(persistence, outcome, persisted)

        agent = FixedGeneralAgent(
            PersistedModelPort(self._model, persistence),
            PersistedCapabilityPort(
                self._capabilities,
                persistence,
                artifact_cleanup=self._artifact_cleanup,
            ),
            sufficiency=self._sufficiency,
            completion=(CompletionEvaluator() if self._sufficiency is not None else None),
            assessment_observer=persistence.agent_sufficiency_assessed,
            observation_observer=persistence.agent_observed,
            completion_observer=persistence.agent_completion_assessed,
            replan_observer=persistence.agent_replan_decided,
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
        )
        outcome = await agent.execute(
            AgentInput(
                request=agent_request,
                run_id=persistence.run.id,
                node_run_id=persistence.node.id,
                session_id=self._session_id,
            )
        )
        self._model_turn_count += outcome.model_turn_count
        self._capability_call_count += outcome.capability_call_count
        self._artifact_count += len(outcome.artifacts)
        try:
            await persistence.finish_node(outcome)
        except AnbanError as exc:
            outcome = storage_failure_outcome(
                exc.info,
                stage="chat_node_finalize",
                model_turn_count=outcome.model_turn_count,
                capability_call_count=outcome.capability_call_count,
                artifacts=outcome.artifacts,
            )
            persisted = await self._finish_after_node_failure(persistence, outcome)
            return self._stop(persistence, outcome, persisted)

        self._last_outcome = outcome
        if outcome.status is not AgentOutcomeStatus.SUCCEEDED:
            persisted = await self._finish_run_failure(persistence, outcome)
            return self._stop(persistence, outcome, persisted)
        self._history.append((request, outcome.final_text or ""))
        return self._result(persistence, outcome, persisted=True)

    async def close(self) -> ExecutionResult | None:
        if self._terminal_result is not None:
            return self._terminal_result
        persistence = self._persistence
        outcome = self._last_outcome
        if persistence is None or outcome is None:
            self._closed = True
            return None
        try:
            await persistence.finish_run(
                outcome,
                model_turn_count=self._model_turn_count,
                capability_call_count=self._capability_call_count,
                artifact_count=self._artifact_count,
            )
        except AnbanError as exc:
            if await matches_terminal(persistence, outcome):
                persisted = True
            else:
                outcome = storage_failure_outcome(
                    exc.info,
                    stage="chat_finalize",
                    model_turn_count=outcome.model_turn_count,
                    capability_call_count=outcome.capability_call_count,
                    artifacts=outcome.artifacts,
                )
                persisted = await self._finish_run_failure(persistence, outcome)
        else:
            persisted = True
        return self._stop(persistence, outcome, persisted)

    async def expire(self) -> ExecutionResult | None:
        return await self._terminate_session(
            AgentOutcomeStatus.TIMED_OUT,
            ErrorInfo(
                code=ErrorCode.EXECUTION_TIMED_OUT,
                message="Chat session timed out",
            ),
        )

    async def interrupt(self) -> ExecutionResult | None:
        return await self._terminate_session(
            AgentOutcomeStatus.CANCELLED,
            ErrorInfo(
                code=ErrorCode.EXECUTION_INTERRUPTED,
                message="Chat session was interrupted",
            ),
        )

    def _conversation_request(self, request: str) -> str:
        if not self._history:
            return request
        parts = [
            "Use this bounded temporary chat context only for the current response.",
        ]
        for user, assistant in self._history:
            parts.extend((f"Previous user: {user}", f"Previous assistant: {assistant}"))
        parts.append(f"Current user: {request}")
        combined = "\n".join(parts)
        if len(combined) > 32_768:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="Chat context exceeds its bounded limit",
                )
            )
        return combined

    async def _finish_after_node_failure(
        self, persistence: RunPersistence, outcome: AgentOutcome
    ) -> bool:
        try:
            await persistence.finish(
                outcome,
                model_turn_count=self._model_turn_count,
                capability_call_count=self._capability_call_count,
                artifact_count=self._artifact_count,
            )
            return True
        except AnbanError:
            return await self._finish_run_failure(persistence, outcome)

    async def _terminate_session(
        self, status: AgentOutcomeStatus, error: ErrorInfo
    ) -> ExecutionResult | None:
        if self._terminal_result is not None:
            return self._terminal_result
        persistence = self._persistence
        if persistence is None:
            self._closed = True
            return None
        previous = self._last_outcome
        outcome = AgentOutcome(
            status=status,
            error=error,
            model_turn_count=0 if previous is None else previous.model_turn_count,
            capability_call_count=0 if previous is None else previous.capability_call_count,
            artifacts=() if previous is None else previous.artifacts,
        )
        persisted = await self._finish_run_failure(persistence, outcome)
        return self._stop(persistence, outcome, persisted)

    async def _finish_run_failure(self, persistence: RunPersistence, outcome: AgentOutcome) -> bool:
        try:
            await persistence.finish_run(
                outcome,
                model_turn_count=self._model_turn_count,
                capability_call_count=self._capability_call_count,
                artifact_count=self._artifact_count,
            )
            return True
        except AnbanError:
            return await matches_terminal(persistence, outcome)

    def _stop(
        self, persistence: RunPersistence, outcome: AgentOutcome, persisted: bool
    ) -> ExecutionResult:
        self._closed = True
        result = self._result(persistence, outcome, persisted=persisted)
        self._terminal_result = result
        return result

    @staticmethod
    def _result(
        persistence: RunPersistence, outcome: AgentOutcome, *, persisted: bool
    ) -> ExecutionResult:
        return ExecutionResult(
            task_id=persistence.task.id,
            run_id=persistence.run.id,
            node_run_id=persistence.node.id,
            outcome=outcome,
            persisted=persisted,
        )
