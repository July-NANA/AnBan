"""Persistent v0.1 Runtime orchestration over authoritative Ports."""

from __future__ import annotations

from anban.capability import ArtifactReference, CapabilityPort
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_execution_run_id, new_node_run_id, new_task_id
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRun, NodeRun, Task
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
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._unit_of_work = unit_of_work
        self._limits = limits

    async def execute(self, request: str) -> ExecutionResult:
        task = Task(id=new_task_id(), request=request)
        run = ExecutionRun(id=new_execution_run_id(), task_id=task.id)
        node = NodeRun(id=new_node_run_id(), run_id=run.id, node_name="general_agent")
        persistence = RunPersistence(self._unit_of_work, task, run, node)
        try:
            await persistence.initialize()
            await persistence.start()
        except AnbanError as exc:
            outcome = persistence_failure_outcome(exc.info, stage="setup")
            persisted = await self._recover_terminal(persistence, outcome)
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
        )
        outcome = await agent.execute(
            AgentInput(request=request, run_id=run.id, node_run_id=node.id)
        )
        try:
            await persistence.finish(outcome)
        except AnbanError as exc:
            if await self._matches_terminal(persistence, outcome):
                persisted = True
            else:
                outcome = persistence_failure_outcome(
                    exc.info,
                    stage="finalize",
                    model_turn_count=outcome.model_turn_count,
                    capability_call_count=outcome.capability_call_count,
                    artifacts=outcome.artifacts,
                )
                persisted = await self._recover_terminal(persistence, outcome)
        else:
            persisted = True
        return ExecutionResult(
            task_id=task.id,
            run_id=run.id,
            node_run_id=node.id,
            outcome=outcome,
            persisted=persisted,
        )

    @staticmethod
    async def _matches_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
        try:
            aggregate = await persistence.load()
        except AnbanError:
            return False
        return aggregate is not None and aggregate.run.status.value == outcome.status.value

    @staticmethod
    async def _recover_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
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
            return await PersistentRuntime._matches_terminal(persistence, outcome)
        except AnbanError:
            return False


def persistence_failure_outcome(
    cause: ErrorInfo,
    *,
    stage: str,
    model_turn_count: int = 0,
    capability_call_count: int = 0,
    artifacts: tuple[ArtifactReference, ...] = (),
) -> AgentOutcome:
    code = (
        cause.code
        if cause.code in {ErrorCode.PERSISTENCE_UNAVAILABLE, ErrorCode.PERSISTENCE_WRITE_FAILED}
        else ErrorCode.PERSISTENCE_WRITE_FAILED
    )
    return AgentOutcome(
        status=AgentOutcomeStatus.FAILED,
        error=ErrorInfo(
            code=code,
            message="Runtime persistence failed",
            details=SafeMetadata({"stage": stage}),
        ),
        model_turn_count=model_turn_count,
        capability_call_count=capability_call_count,
        artifacts=artifacts,
    )
