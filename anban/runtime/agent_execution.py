"""Capability execution, observation, and terminal helpers for the fixed Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityPort,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata
from anban.model import ModelTurn, ToolCall
from anban.runtime.contracts import (
    AgentLimits,
    AgentObservation,
    AgentOutcome,
    AgentOutcomeStatus,
    ExecutionStrategy,
    ObservationStatus,
)

_CANCELLATION_TIMEOUT_SECONDS = 2.0
_BACKGROUND_PROGRESS_INTERVAL_SECONDS = 1.0


@dataclass
class ExecutionProgress:
    model_turns: int = 0
    response_repairs: int = 0
    capability_calls: int = 0
    artifacts: list[ArtifactReference] = field(default_factory=lambda: list[ArtifactReference]())
    observations: list[AgentObservation] = field(default_factory=lambda: list[AgentObservation]())
    skill_context_chars: int = 0
    replans: int = 0

    @property
    def reasoning_turns(self) -> int:
        """Count ordinary reasoning turns separately from bounded response repairs."""

        return self.model_turns - self.response_repairs


class AgentExecutionSupport:
    """Shared implementation mixed into the one fixed General Agent."""

    _capabilities: CapabilityPort
    _limits: AgentLimits
    _observation_observer: Callable[[AgentObservation], Awaitable[None]] | None

    @staticmethod
    def _observation_target(call: ToolCall, descriptor: CapabilityDescriptor) -> str:
        if descriptor.kind is CapabilityKind.SKILL:
            target = call.arguments.get("name")
            if isinstance(target, str):
                return target
        return descriptor.name

    async def _record_result_observation(
        self,
        progress: ExecutionProgress,
        strategy: ExecutionStrategy,
        target: str | None,
        descriptor: CapabilityDescriptor,
        result: CapabilityResult,
        observation: str,
    ) -> AgentObservation:
        error_code = None if result.error is None else result.error.code
        retry_safe = error_code in {
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
            ErrorCode.CAPABILITY_UNAVAILABLE,
        }
        status = (
            ObservationStatus.COMPLETED
            if result.status is CapabilityResultStatus.COMPLETED
            else ObservationStatus.UNAVAILABLE
            if error_code is ErrorCode.CAPABILITY_UNAVAILABLE
            else ObservationStatus.FAILED
        )
        side_effect_completed = (
            result.status is CapabilityResultStatus.COMPLETED
            and descriptor.kind is not CapabilityKind.SKILL
        )
        return await self._record_observation(
            progress,
            strategy,
            target,
            status,
            observation,
            retry_safe=retry_safe,
            side_effect_completed=side_effect_completed,
        )

    async def _record_observation(
        self,
        progress: ExecutionProgress,
        strategy: ExecutionStrategy,
        target: str | None,
        status: ObservationStatus,
        raw_observation: str,
        *,
        retry_safe: bool,
        side_effect_completed: bool,
    ) -> AgentObservation:
        digest = hashlib.sha256(raw_observation.encode()).hexdigest()
        observation = AgentObservation(
            sequence=len(progress.observations) + 1,
            strategy=strategy,
            target=target,
            status=status,
            summary=f"Capability observation SHA-256 {digest} has status {status.value}.",
            retry_safe=retry_safe,
            side_effect_completed=side_effect_completed,
        )
        progress.observations.append(observation)
        if self._observation_observer is not None:
            await self._observation_observer(observation)
        return observation

    @staticmethod
    def _strategy(descriptor: CapabilityDescriptor) -> ExecutionStrategy:
        if descriptor.kind is CapabilityKind.SKILL:
            return ExecutionStrategy.ACTIVATE_SKILL
        return {
            InventoryKind.PROCESS: ExecutionStrategy.USE_PROCESS,
            InventoryKind.SUB_AGENT: ExecutionStrategy.DELEGATE,
            InventoryKind.CAPABILITY: ExecutionStrategy.USE_CAPABILITY,
            InventoryKind.MCP: ExecutionStrategy.USE_CAPABILITY,
            InventoryKind.MEMORY: ExecutionStrategy.USE_CAPABILITY,
        }.get(descriptor.inventory_kind, ExecutionStrategy.USE_CAPABILITY)

    @staticmethod
    def _replay_prevented_observation() -> str:
        return json.dumps(
            {
                "status": "failed",
                "error_code": ErrorCode.CAPABILITY_EXECUTION_FAILED.value,
                "reason": "completed_call_replay_prevented",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

    async def _invoke_capability(
        self,
        call: ToolCall,
        context: InvocationContext,
        progress: ExecutionProgress,
    ) -> CapabilityResult | AgentOutcome:
        progress.capability_calls += 1
        invocation = asyncio.create_task(self._invoke_to_terminal(call, context))
        try:
            result = await asyncio.shield(invocation)
        except asyncio.CancelledError:
            cancellation_error: ErrorInfo | None = None
            try:
                await asyncio.wait_for(
                    self._capabilities.cancel(context),
                    timeout=_CANCELLATION_TIMEOUT_SECONDS,
                )
            except AnbanError as exc:
                cancellation_error = exc.info
            except Exception:
                cancellation_error = self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability cancellation failed",
                    "cancellation_failure",
                )
            if not invocation.done():
                invocation.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(invocation, return_exceptions=True),
                    timeout=_CANCELLATION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                cancellation_error = self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability cancellation did not terminate execution",
                    "cancellation_timeout",
                )
            if cancellation_error is not None:
                raise AnbanError(cancellation_error) from None
            raise
        except AnbanError as exc:
            return self._failure_outcome(exc.info, progress)
        except Exception:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability execution failed",
                    "capability_exception",
                ),
            )
        if len(progress.artifacts) + len(result.artifacts) > 32:
            return self._limit_outcome(progress, "artifact_budget")
        progress.artifacts.extend(result.artifacts)
        if result.status is CapabilityResultStatus.COMPLETED:
            return result
        if result.status is CapabilityResultStatus.FAILED and result.observation is not None:
            return result
        if result.error is None:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability failed without a structured error",
                    "missing_capability_error",
                ),
            )
        return self._failure_outcome(result.error, progress, result.status)

    async def _invoke_to_terminal(
        self, call: ToolCall, context: InvocationContext
    ) -> CapabilityResult:
        result = await self._capabilities.invoke(call.name, call.arguments, context)
        if result.status is not CapabilityResultStatus.ACCEPTED:
            return result

        waiter: asyncio.Task[CapabilityResult] | None = None
        try:
            await self._capabilities.progress(context)
            waiter = asyncio.create_task(self._capabilities.wait(context))
            while True:
                done, _ = await asyncio.wait(
                    (waiter,), timeout=_BACKGROUND_PROGRESS_INTERVAL_SECONDS
                )
                if done:
                    return await waiter
                await self._capabilities.progress(context)
        except Exception:
            with suppress(Exception):
                await self._capabilities.cancel(context)
            if waiter is None:
                waiter = asyncio.create_task(self._capabilities.wait(context))
            await asyncio.gather(waiter, return_exceptions=True)
            raise

    @staticmethod
    def _validate_turn(turn: ModelTurn) -> ErrorInfo | None:
        if turn.structured_output is not None:
            return AgentExecutionSupport._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Model returned unsupported structured output",
                "unexpected_structured_output",
            )
        if turn.tool_calls and turn.finish_reason != "tool_calls":
            return AgentExecutionSupport._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Model Tool Call finish reason is invalid",
                "invalid_finish_reason",
            )
        if turn.content is not None and turn.finish_reason != "stop":
            return AgentExecutionSupport._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Model final finish reason is invalid",
                "invalid_finish_reason",
            )
        return None

    @staticmethod
    def _call_signature(call: ToolCall) -> str:
        canonical = json.dumps(call.arguments, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{call.name}:{canonical}".encode()).hexdigest()

    def _limit_outcome(self, progress: ExecutionProgress, reason: str) -> AgentOutcome:
        return self._outcome(
            AgentOutcomeStatus.FAILED,
            progress,
            error=self._error(
                ErrorCode.VALIDATION_FAILED,
                "General Agent execution limit was reached",
                reason,
            ),
        )

    def _failure_outcome(
        self,
        error: ErrorInfo,
        progress: ExecutionProgress,
        capability_status: CapabilityResultStatus | None = None,
    ) -> AgentOutcome:
        if error.code in {ErrorCode.MODEL_TIMEOUT, ErrorCode.EXECUTION_TIMED_OUT}:
            status = AgentOutcomeStatus.TIMED_OUT
        elif error.code is ErrorCode.EXECUTION_INTERRUPTED:
            status = AgentOutcomeStatus.CANCELLED
        elif capability_status is CapabilityResultStatus.TIMED_OUT:
            status = AgentOutcomeStatus.TIMED_OUT
        elif capability_status is CapabilityResultStatus.CANCELLED:
            status = AgentOutcomeStatus.CANCELLED
        else:
            status = AgentOutcomeStatus.FAILED
        return self._outcome(status, progress, error=error)

    @staticmethod
    def _outcome(
        status: AgentOutcomeStatus,
        progress: ExecutionProgress,
        *,
        final_text: str | None = None,
        error: ErrorInfo | None = None,
    ) -> AgentOutcome:
        return AgentOutcome(
            status=status,
            final_text=final_text,
            error=error,
            model_turn_count=progress.model_turns,
            capability_call_count=progress.capability_calls,
            artifacts=tuple(progress.artifacts),
        )

    @staticmethod
    def _error(code: ErrorCode, message: str, reason: str) -> ErrorInfo:
        return ErrorInfo(
            code=code,
            message=message,
            details=SafeMetadata({"reason": reason}),
        )
