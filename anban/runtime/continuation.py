"""In-process pause, resume, and cancellation over durable Checkpoint facts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from anban.capability import CapabilityResult, InvocationContext
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CheckpointId
from anban.core.metadata import SafeMetadata
from anban.runtime.contracts import ExecutionResult, WaitingExecution
from anban.runtime.persistence import RunPersistence

ContinuationResult = WaitingExecution | ExecutionResult
ContinuationExecutor = Callable[["ContinuationControl"], Awaitable[ExecutionResult]]


@dataclass
class _WaitingState:
    value: WaitingExecution
    persistence: RunPersistence
    gate: asyncio.Event


@dataclass
class _ActiveContinuation:
    control: ContinuationControl
    execution: asyncio.Task[ExecutionResult]
    waiting: _WaitingState
    resumed: bool = False


class ContinuationControl:
    """One execution's internal handoff channel; durable facts remain authoritative."""

    def __init__(self) -> None:
        self._waiting: asyncio.Queue[_WaitingState] = asyncio.Queue(maxsize=1)

    async def pause(
        self,
        context: InvocationContext,
        result: CapabilityResult,
        persistence: RunPersistence,
    ) -> None:
        raw_checkpoint = result.metadata.root.get("checkpoint_id")
        try:
            checkpoint_id = CheckpointId(UUID(str(raw_checkpoint)))
        except (TypeError, ValueError, AttributeError):
            raise self._error("checkpoint_identity_missing") from None
        waiting = _WaitingState(
            value=WaitingExecution(
                task_id=persistence.task.id,
                run_id=context.run_id,
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
                checkpoint_id=checkpoint_id,
            ),
            persistence=persistence,
            gate=asyncio.Event(),
        )
        await self._waiting.put(waiting)
        await waiting.gate.wait()

    async def next_waiting(self) -> _WaitingState:
        return await self._waiting.get()

    @staticmethod
    def _error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Async continuation checkpoint is invalid",
                details=SafeMetadata({"reason": reason}),
            )
        )


class ContinuationManager:
    """Own live coroutine handles while Checkpoints own durable coordination history."""

    def __init__(self) -> None:
        self._active: dict[CheckpointId, _ActiveContinuation] = {}

    async def start(self, executor: ContinuationExecutor) -> ContinuationResult:
        control = ContinuationControl()

        async def run() -> ExecutionResult:
            return await executor(control)

        execution = asyncio.create_task(run())
        return await self._next(control, execution)

    async def resume(self, checkpoint_id: CheckpointId) -> ContinuationResult:
        active = self._get(checkpoint_id)
        if active.resumed:
            raise self._error("checkpoint_already_resumed")
        await active.waiting.persistence.checkpoints.resume(checkpoint_id)
        active.resumed = True
        active.waiting.gate.set()
        try:
            result = await self._next(active.control, active.execution)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._active.pop(checkpoint_id, None)
            raise
        self._active.pop(checkpoint_id, None)
        return result

    async def cancel(self, checkpoint_id: CheckpointId) -> ExecutionResult:
        active = self._get(checkpoint_id)
        try:
            await active.waiting.persistence.checkpoints.request_cancel(checkpoint_id)
        except Exception:
            raise
        active.waiting.gate.set()
        active.execution.cancel()
        try:
            return await active.execution
        finally:
            self._active.pop(checkpoint_id, None)

    async def _next(
        self,
        control: ContinuationControl,
        execution: asyncio.Task[ExecutionResult],
    ) -> ContinuationResult:
        waiting = asyncio.create_task(control.next_waiting())
        try:
            done, _ = await asyncio.wait((execution, waiting), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            raise
        if execution in done:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            return await execution
        state = await waiting
        checkpoint_id = state.value.checkpoint_id
        if checkpoint_id in self._active:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise self._error("checkpoint_already_active")
        self._active[checkpoint_id] = _ActiveContinuation(control, execution, state)
        return state.value

    def _get(self, checkpoint_id: CheckpointId) -> _ActiveContinuation:
        active = self._active.get(checkpoint_id)
        if active is None:
            raise self._error("checkpoint_not_active")
        return active

    @staticmethod
    def _error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Async continuation is unavailable",
                details=SafeMetadata({"reason": reason}),
            )
        )
