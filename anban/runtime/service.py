"""Persistent v0.1 Runtime orchestration over authoritative Ports."""

from __future__ import annotations

from datetime import timedelta

from anban.capability import ArtifactReference, CapabilityPort
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_execution_run_id, new_node_run_id, new_task_id
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRun, NodeRun, Task, now_utc
from anban.core.persistence import UnitOfWorkFactory
from anban.model import ModelPort
from anban.runtime.agent import FixedGeneralAgent
from anban.runtime.contracts import (
    AgentInput,
    AgentLimits,
    AgentOutcome,
    AgentOutcomeStatus,
    ExecutionResult,
)
from anban.runtime.persistence import (
    PersistedCapabilityPort,
    PersistedModelPort,
    RunPersistence,
)


class PersistentRuntime:
    """Create, execute, and durably finalize one fixed General Agent Run."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        unit_of_work: UnitOfWorkFactory,
        *,
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._unit_of_work = unit_of_work
        self._limits = limits
        self._response_repair_retries = response_repair_retries

    async def execute(
        self, request: str, *, metadata: SafeMetadata | None = None
    ) -> ExecutionResult:
        safe_metadata = metadata or SafeMetadata()
        task = Task(id=new_task_id(), request=request, metadata=safe_metadata)
        run = ExecutionRun(id=new_execution_run_id(), task_id=task.id, metadata=safe_metadata)
        node = NodeRun(
            id=new_node_run_id(),
            run_id=run.id,
            node_name="general_agent",
            metadata=safe_metadata,
        )
        persistence = RunPersistence(self._unit_of_work, task, run, node)
        try:
            await persistence.initialize()
            await persistence.start()
        except AnbanError as exc:
            outcome = storage_failure_outcome(exc.info, stage="setup")
            persisted = await self.recover_terminal(persistence, outcome)
            return ExecutionResult(
                task_id=task.id,
                run_id=run.id,
                node_run_id=node.id,
                outcome=outcome,
                persisted=persisted,
            )

        agent = FixedGeneralAgent(
            PersistedModelPort(self._model, persistence),
            PersistedCapabilityPort(self._capabilities, persistence),
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
        )
        outcome = await agent.execute(
            AgentInput(request=request, run_id=run.id, node_run_id=node.id)
        )
        try:
            await persistence.finish(outcome)
        except AnbanError as exc:
            if await self.matches_terminal(persistence, outcome):
                persisted = True
            else:
                outcome = storage_failure_outcome(
                    exc.info,
                    stage="finalize",
                    model_turn_count=outcome.model_turn_count,
                    capability_call_count=outcome.capability_call_count,
                    artifacts=outcome.artifacts,
                )
                persisted = await self.recover_terminal(persistence, outcome)
        else:
            persisted = True
        return ExecutionResult(
            task_id=task.id,
            run_id=run.id,
            node_run_id=node.id,
            outcome=outcome,
            persisted=persisted,
        )

    def chat(self) -> PersistentChatSession:
        return PersistentChatSession(
            self._model,
            self._capabilities,
            self._unit_of_work,
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
        )

    @staticmethod
    async def matches_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
        try:
            aggregate = await persistence.load()
        except AnbanError:
            return False
        return aggregate is not None and aggregate.run.status.value == outcome.status.value

    @staticmethod
    async def recover_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
        """Confirm an ambiguous commit or persist a safe failure without side-effect replay."""

        try:
            aggregate = await persistence.load()
            if aggregate is None:
                return False
            if aggregate.run.status.value == outcome.status.value:
                return True
            if aggregate.run.status.value == "created":
                await persistence.start()
            await persistence.finish(outcome)
            return await PersistentRuntime.matches_terminal(persistence, outcome)
        except AnbanError:
            return False


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
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._unit_of_work = unit_of_work
        self._limits = limits
        self._response_repair_retries = response_repair_retries
        self._started_at = now_utc()
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
        safe_metadata = metadata or SafeMetadata()
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
                persisted = await PersistentRuntime.recover_terminal(persistence, outcome)
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
            PersistedCapabilityPort(self._capabilities, persistence),
            limits=self._limits,
            response_repair_retries=self._response_repair_retries,
        )
        outcome = await agent.execute(
            AgentInput(
                request=agent_request,
                run_id=persistence.run.id,
                node_run_id=persistence.node.id,
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
            if await PersistentRuntime.matches_terminal(persistence, outcome):
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
            return await PersistentRuntime.matches_terminal(persistence, outcome)

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


def storage_failure_outcome(
    cause: ErrorInfo,
    *,
    stage: str,
    model_turn_count: int = 0,
    capability_call_count: int = 0,
    artifacts: tuple[ArtifactReference, ...] = (),
) -> AgentOutcome:
    code = (
        cause.code
        if cause.code
        in {
            ErrorCode.PERSISTENCE_UNAVAILABLE,
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            ErrorCode.AUDIT_TRACE_WRITE_FAILED,
        }
        else ErrorCode.PERSISTENCE_WRITE_FAILED
    )
    return AgentOutcome(
        status=AgentOutcomeStatus.FAILED,
        error=ErrorInfo(
            code=code,
            message=(
                "Runtime Event persistence failed"
                if code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
                else "Runtime persistence failed"
            ),
            details=SafeMetadata({"stage": stage}),
        ),
        model_turn_count=model_turn_count,
        capability_call_count=capability_call_count,
        artifacts=artifacts,
    )
