"""Real parent/child Run delegation through the ordinary Agent Runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityProgress,
    CapabilityProgressStatus,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CapabilityInvocationId, ExecutionRunId, NodeRunId, TaskId
from anban.core.metadata import SafeMetadata
from anban.core.models import ExecutionRunStatus
from anban.core.persistence import ExecutionRunAggregate, UnitOfWorkFactory


class DelegateRunOutcome:
    """Safe terminal projection returned by the real child Runtime task."""

    def __init__(
        self,
        *,
        task_id: TaskId,
        run_id: ExecutionRunId,
        node_run_id: NodeRunId,
        status: ExecutionRunStatus,
        final_text: str | None,
        error: ErrorInfo | None,
        artifact_count: int,
        delegation_depth: int,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.node_run_id = node_run_id
        self.status = status
        self.final_text = final_text
        self.error = error
        self.artifact_count = artifact_count
        self.delegation_depth = delegation_depth


@dataclass(frozen=True)
class DelegateExecutionHandle:
    task_id: TaskId
    run_id: ExecutionRunId
    node_run_id: NodeRunId
    completion: asyncio.Task[DelegateRunOutcome]


DelegateRunner = Callable[
    [str, ExecutionRunId, CapabilityInvocationId, int],
    Awaitable[DelegateExecutionHandle],
]


@dataclass
class _DelegateState:
    handle: DelegateExecutionHandle
    parent_run_id: ExecutionRunId
    parent_invocation_id: CapabilityInvocationId
    objective_hash: str
    delegation_depth: int
    sequence: int = 0


class AgentDelegateCapability:
    """Create and observe one independently durable child Run per invocation."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        protected_values: tuple[str, ...] = (),
        available: bool = True,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._protected_values = tuple(value for value in protected_values if value)
        self._runner: DelegateRunner | None = None
        self._states: dict[str, _DelegateState] = {}
        self.descriptor = CapabilityDescriptor(
            name="agent.delegate",
            description=(
                "Delegate one bounded objective to an independently durable child Agent Run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": policy.SUBAGENT_OBJECTIVE_MAX_CHARS,
                    }
                },
                "required": ["objective"],
                "additionalProperties": False,
            },
            inventory_kind=InventoryKind.SUB_AGENT,
            available=available,
        )

    def bind(self, runner: DelegateRunner) -> None:
        if self._runner is not None:
            raise RuntimeError("Sub-agent runner is already bound")
        self._runner = runner

    async def invoke(
        self,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        runner = self._runner
        objective = arguments.get("objective")
        if runner is None or not isinstance(objective, str):
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Sub-agent execution is unavailable",
                "subagent_runner_unavailable",
            )
        depth = await self._parent_depth(context)
        if depth >= policy.SUBAGENT_DEPTH_MAX:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Sub-agent delegation depth is exhausted",
                "delegation_depth_exhausted",
            )
        child_depth = depth + 1
        objective_hash = hashlib.sha256(objective.encode()).hexdigest()
        handle = await runner(objective, context.run_id, context.invocation_id, child_depth)
        state = _DelegateState(
            handle=handle,
            parent_run_id=context.run_id,
            parent_invocation_id=context.invocation_id,
            objective_hash=objective_hash,
            delegation_depth=child_depth,
        )
        self._states[str(context.invocation_id)] = state
        return CapabilityResult(
            status=CapabilityResultStatus.ACCEPTED,
            metadata=self._metadata(state, None),
        )

    async def progress(self, context: InvocationContext) -> CapabilityProgress:
        state = self._state(context)
        state.sequence += 1
        ready = state.handle.completion.done()
        return CapabilityProgress(
            sequence=state.sequence,
            status=(
                CapabilityProgressStatus.RESULT_READY if ready else CapabilityProgressStatus.RUNNING
            ),
            metadata=self._metadata(state, None),
        )

    async def wait(self, context: InvocationContext) -> CapabilityResult:
        state = self._state(context)
        try:
            outcome = await state.handle.completion
            return self._result(state, outcome)
        finally:
            self._states.pop(str(context.invocation_id), None)

    async def cancel(self, context: InvocationContext) -> None:
        state = self._state(context)
        if not state.handle.completion.done():
            state.handle.completion.cancel()
            await asyncio.gather(state.handle.completion, return_exceptions=True)

    async def recover(self, context: InvocationContext, progress_sequence: int) -> None:
        aggregate = await self._load_child(context)
        outcome = self._outcome_from_aggregate(aggregate)

        async def completed() -> DelegateRunOutcome:
            return outcome

        handle = DelegateExecutionHandle(
            task_id=outcome.task_id,
            run_id=outcome.run_id,
            node_run_id=outcome.node_run_id,
            completion=asyncio.create_task(completed()),
        )
        event = next(
            event for event in aggregate.events if event.event_type == "subagent.child_created"
        )
        objective_hash = event.metadata.root.get("objective_hash")
        if not isinstance(objective_hash, str):
            raise self._error(
                ErrorCode.PERSISTENCE_UNAVAILABLE,
                "Sub-agent relationship is incomplete",
                "subagent_objective_missing",
            )
        self._states[str(context.invocation_id)] = _DelegateState(
            handle=handle,
            parent_run_id=context.run_id,
            parent_invocation_id=context.invocation_id,
            objective_hash=objective_hash,
            delegation_depth=outcome.delegation_depth,
            sequence=progress_sequence,
        )

    async def aclose(self) -> None:
        pending = tuple(
            state.handle.completion
            for state in self._states.values()
            if not state.handle.completion.done()
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._states.clear()

    async def _parent_depth(self, context: InvocationContext) -> int:
        try:
            async with self._unit_of_work() as unit:
                run = await unit.executions.get_run(context.run_id)
                invocation = await unit.executions.get_invocation(context.invocation_id)
        except AnbanError:
            raise
        except Exception:
            raise self._error(
                ErrorCode.PERSISTENCE_UNAVAILABLE,
                "Parent Run is unavailable",
                "parent_run_unavailable",
            ) from None
        if (
            run is None
            or invocation is None
            or invocation.run_id != context.run_id
            or run.status is not ExecutionRunStatus.RUNNING
        ):
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Parent Run is ineligible for delegation",
                "parent_run_ineligible",
            )
        return run.delegation_depth

    async def _load_child(self, context: InvocationContext) -> ExecutionRunAggregate:
        match = SafeMetadata(
            {
                "parent_run_id": str(context.run_id),
                "parent_invocation_id": str(context.invocation_id),
            }
        )
        try:
            async with self._unit_of_work() as unit:
                event = await unit.executions.find_event("subagent.child_created", match)
                aggregate = None if event is None else await unit.executions.load_run(event.run_id)
        except AnbanError:
            raise
        except Exception:
            raise self._error(
                ErrorCode.PERSISTENCE_UNAVAILABLE,
                "Sub-agent relationship query failed",
                "subagent_query_failed",
            ) from None
        if event is None or aggregate is None:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Delegated child Run is unavailable",
                "subagent_child_missing",
            )
        return aggregate

    def _outcome_from_aggregate(self, aggregate: ExecutionRunAggregate) -> DelegateRunOutcome:
        run = aggregate.run
        if run.status in {ExecutionRunStatus.CREATED, ExecutionRunStatus.RUNNING}:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Delegated child Run result is not ready",
                "subagent_result_not_ready",
            )
        if run.parent_run_id is None or run.parent_invocation_id is None or not aggregate.nodes:
            raise self._error(
                ErrorCode.PERSISTENCE_UNAVAILABLE,
                "Sub-agent relationship is incomplete",
                "subagent_relationship_incomplete",
            )
        error = (
            None
            if run.error_code is None
            else ErrorInfo(
                code=run.error_code,
                message="Delegated child Run did not succeed",
            )
        )
        return DelegateRunOutcome(
            task_id=run.task_id,
            run_id=run.id,
            node_run_id=aggregate.nodes[0].id,
            status=run.status,
            final_text=run.final_text,
            error=error,
            artifact_count=len(aggregate.artifacts),
            delegation_depth=run.delegation_depth,
        )

    def _result(
        self,
        state: _DelegateState,
        outcome: DelegateRunOutcome,
    ) -> CapabilityResult:
        payload: dict[str, JsonValue] = {
            "status": outcome.status.value,
            "child_task_id": str(outcome.task_id),
            "child_run_id": str(outcome.run_id),
            "artifact_count": outcome.artifact_count,
        }
        if outcome.final_text is not None:
            payload["result"] = outcome.final_text
        observation = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if any(value in observation for value in self._protected_values):
            return CapabilityResult(
                status=CapabilityResultStatus.FAILED,
                error=ErrorInfo(
                    code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    message="Delegated child Run result is unavailable",
                    details=SafeMetadata({"reason": "subagent_sensitive_output"}),
                ),
                metadata=self._metadata(state, outcome),
            )
        status = {
            ExecutionRunStatus.SUCCEEDED: CapabilityResultStatus.COMPLETED,
            ExecutionRunStatus.FAILED: CapabilityResultStatus.FAILED,
            ExecutionRunStatus.CANCELLED: CapabilityResultStatus.CANCELLED,
            ExecutionRunStatus.TIMED_OUT: CapabilityResultStatus.TIMED_OUT,
        }[outcome.status]
        return CapabilityResult(
            status=status,
            observation=observation,
            error=outcome.error,
            metadata=self._metadata(state, outcome),
        )

    @staticmethod
    def _metadata(
        state: _DelegateState,
        outcome: DelegateRunOutcome | None,
    ) -> SafeMetadata:
        return SafeMetadata(
            {
                "child_artifact_count": (None if outcome is None else outcome.artifact_count),
                "child_node_run_id": str(state.handle.node_run_id),
                "child_run_id": str(state.handle.run_id),
                "child_status": None if outcome is None else outcome.status.value,
                "child_task_id": str(state.handle.task_id),
                "delegation_depth": state.delegation_depth,
                "objective_hash": state.objective_hash,
                "parent_invocation_id": str(state.parent_invocation_id),
                "parent_run_id": str(state.parent_run_id),
            }
        )

    def _state(self, context: InvocationContext) -> _DelegateState:
        state = self._states.get(str(context.invocation_id))
        if state is None or state.parent_run_id != context.run_id:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Sub-agent invocation is unavailable",
                "subagent_invocation_unavailable",
            )
        return state

    @staticmethod
    def _error(code: ErrorCode, message: str, reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=code,
                message=message,
                details=SafeMetadata({"reason": reason}),
            )
        )
