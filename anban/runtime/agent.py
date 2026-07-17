"""Fixed LangGraph General Agent with one bounded model-Capability loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from anban.capability import (
    ArtifactReference,
    CapabilityPort,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_capability_invocation_id
from anban.core.metadata import SafeMetadata, safe_text_violation_reason, validate_safe_text
from anban.core.models import now_utc
from anban.model import (
    ModelMessage,
    ModelPort,
    ModelRequest,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from anban.runtime.contracts import AgentInput, AgentLimits, AgentOutcome, AgentOutcomeStatus

GENERAL_AGENT_NODE = "general_agent"
_CANCELLATION_TIMEOUT_SECONDS = 2.0
_SYSTEM_INSTRUCTIONS = (
    "You are the fixed Anban v0.1 General Agent. Use only the listed Capabilities. "
    "Choose appropriate Skills for the user's goal and follow activated SKILL.md instructions. "
    "Use process.execute for command-line programs, scripts, file operations, network operations, "
    "and package tools. Never invent a Capability or claim an operation ran when it did not. "
    "Treat nonzero exits, timeouts, cancellation, and Artifact collection failures as failures. "
    "Do not replay a completed side effect while repairing a model response. Use Tool Results as "
    "observations, then return one concise final answer. When an action is required, use native "
    "Tool Calls. Narrated actions are not evidence of execution. Assistant text accompanying "
    "valid Tool Calls is non-authoritative and may be ignored. A final answer must not contain "
    "Tool Calls."
)
_REPAIR_INSTRUCTION = (
    "Your previous response violated the response contract. When an action is required, return "
    "valid native Tool Calls with complete IDs, function names, and JSON object arguments. "
    "Otherwise return one non-empty final assistant message. Narrated actions are not evidence of "
    "execution, and text accompanying valid Tool Calls is non-authoritative."
)
_RESPONSE_CONTRACT_REMINDER = (
    "Response contract reminder: use native Tool Calls for actions and one non-empty assistant "
    "message for the final answer. Text accompanying valid Tool Calls is non-authoritative. Do not "
    "replay any Capability call that already completed."
)


@dataclass
class ExecutionProgress:
    model_turns: int = 0
    capability_calls: int = 0
    artifacts: list[ArtifactReference] = field(default_factory=lambda: list[ArtifactReference]())


class AgentGraphState(TypedDict):
    agent_input: AgentInput
    deadline_at: datetime
    progress: ExecutionProgress
    outcome: AgentOutcome | None


class AgentGraphUpdate(TypedDict, total=False):
    outcome: AgentOutcome


class FixedGeneralAgent:
    """The only v0.1 Agent graph: START -> General Agent -> END."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        *,
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._limits = limits or AgentLimits()
        if not (
            policy.MODEL_RESPONSE_REPAIR_RETRIES_MIN
            <= response_repair_retries
            <= policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX
        ):
            raise ValueError("response repair budget is outside the safety policy")
        self._response_repair_retries = response_repair_retries
        builder = StateGraph(AgentGraphState)
        builder.add_node(GENERAL_AGENT_NODE, self._general_agent_node)
        builder.add_edge(START, GENERAL_AGENT_NODE)
        builder.add_edge(GENERAL_AGENT_NODE, END)
        self._graph = builder.compile(name="anban_v01_general_agent")

    async def execute(self, agent_input: AgentInput) -> AgentOutcome:
        progress = ExecutionProgress()
        deadline = now_utc() + timedelta(seconds=self._limits.total_timeout_seconds)
        state: AgentGraphState = {
            "agent_input": agent_input,
            "deadline_at": deadline,
            "progress": progress,
            "outcome": None,
        }
        try:
            result = await asyncio.wait_for(
                self._graph.ainvoke(state),
                timeout=self._limits.total_timeout_seconds,
            )
        except TimeoutError:
            return self._outcome(
                AgentOutcomeStatus.TIMED_OUT,
                progress,
                error=self._error(
                    ErrorCode.EXECUTION_TIMED_OUT,
                    "General Agent execution timed out",
                    "total_timeout",
                ),
            )
        except asyncio.CancelledError:
            return self._outcome(
                AgentOutcomeStatus.CANCELLED,
                progress,
                error=self._error(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "General Agent execution was interrupted",
                    "interrupted",
                ),
            )
        except AnbanError as exc:
            return self._failure_outcome(exc.info, progress)
        except Exception:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.VALIDATION_FAILED,
                    "General Agent execution failed",
                    "graph_failure",
                ),
            )
        outcome = result.get("outcome")
        if outcome is None:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.VALIDATION_FAILED,
                    "General Agent produced no terminal outcome",
                    "missing_outcome",
                ),
            )
        return outcome

    def graph_edges(self) -> tuple[tuple[str, str], ...]:
        """Expose topology facts for deterministic acceptance without a second scheduler."""

        graph = self._graph.get_graph()
        return tuple((edge.source, edge.target) for edge in graph.edges)

    async def _general_agent_node(self, state: AgentGraphState) -> AgentGraphUpdate:
        return {
            "outcome": await self._run_loop(
                state["agent_input"],
                state["deadline_at"],
                state["progress"],
            )
        }

    async def _run_loop(
        self,
        agent_input: AgentInput,
        deadline_at: datetime,
        progress: ExecutionProgress,
    ) -> AgentOutcome:
        descriptors = tuple(item for item in self._capabilities.search() if item.available)
        tools = tuple(
            ToolDefinition(
                name=descriptor.name,
                description=descriptor.description,
                input_schema=descriptor.input_schema,
            )
            for descriptor in descriptors
        )
        messages: list[ModelMessage] = [
            ModelMessage(role="system", content=_SYSTEM_INSTRUCTIONS),
            ModelMessage(role="user", content=agent_input.request),
        ]
        last_signature: str | None = None
        repeated_calls = 0
        completed_rounds: set[str] = set()
        completed_signatures: set[str] = set()
        repair_attempts_used = 0
        request_is_repair = False

        while progress.model_turns < self._limits.max_model_turns:
            request_repair_attempt = repair_attempts_used if request_is_repair else 0
            progress.model_turns += 1
            try:
                turn = await self._model.complete(
                    ModelRequest(
                        messages=tuple(messages),
                        tools=tools,
                        repair_attempt=request_repair_attempt,
                        repair_limit=self._response_repair_retries,
                    )
                )
            except AnbanError as exc:
                if (
                    exc.info.code is ErrorCode.MODEL_RESPONSE_INVALID
                    and exc.info.details.root.get("repairable") is True
                    and repair_attempts_used < self._response_repair_retries
                    and progress.model_turns < self._limits.max_model_turns
                    and now_utc() < deadline_at
                ):
                    repair_attempts_used += 1
                    request_is_repair = True
                    messages.append(ModelMessage(role="system", content=_REPAIR_INSTRUCTION))
                    continue
                return self._failure_outcome(exc.info, progress)
            except Exception:
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_REQUEST_FAILED,
                        "Model request failed",
                        "model_exception",
                    ),
                )
            repaired_response = request_is_repair
            request_is_repair = False
            invalid = self._validate_turn(turn)
            if invalid is not None:
                return self._outcome(AgentOutcomeStatus.FAILED, progress, error=invalid)
            if turn.content is not None:
                stripped_content = turn.content.strip()
                try:
                    final_text = validate_safe_text(
                        stripped_content, label="Agent final text", max_length=32_768
                    )
                except ValueError:
                    violation = (
                        safe_text_violation_reason(stripped_content, max_length=32_768) or "unknown"
                    )
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.MODEL_RESPONSE_INVALID,
                            "Model final response is unsafe",
                            f"unsafe_final_{violation}",
                        ),
                    )
                if not final_text:
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.MODEL_RESPONSE_INVALID,
                            "Model final response is empty",
                            "empty_final",
                        ),
                    )
                return self._outcome(
                    AgentOutcomeStatus.SUCCEEDED,
                    progress,
                    final_text=final_text,
                )

            calls = turn.tool_calls
            if progress.capability_calls + len(calls) > self._limits.max_capability_calls:
                return self._limit_outcome(progress, "capability_call_budget")
            call_ids = {call.id for call in calls}
            if len(call_ids) != len(calls):
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_RESPONSE_INVALID,
                        "Model returned duplicate Tool Call identities",
                        "duplicate_tool_call",
                    ),
                )
            signatures = tuple(self._call_signature(call) for call in calls)
            if repaired_response and any(
                signature in completed_signatures for signature in signatures
            ):
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_RESPONSE_INVALID,
                        "Model repair replayed a completed Capability call",
                        "repair_replayed_completed_call",
                    ),
                )
            messages.append(ModelMessage(role="assistant", tool_calls=calls))
            round_facts: list[str] = []
            for call, signature in zip(calls, signatures, strict=True):
                if signature == last_signature:
                    repeated_calls += 1
                else:
                    last_signature = signature
                    repeated_calls = 1
                if (
                    self._limits.repeated_call_limit
                    and repeated_calls >= self._limits.repeated_call_limit
                ):
                    return self._limit_outcome(progress, "repeated_call")
                context = InvocationContext(
                    run_id=agent_input.run_id,
                    node_run_id=agent_input.node_run_id,
                    invocation_id=new_capability_invocation_id(),
                    deadline_at=deadline_at,
                )
                result = await self._invoke_capability(call, context, progress)
                if isinstance(result, AgentOutcome):
                    return result
                observation = result.observation
                if observation is None:
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.CAPABILITY_EXECUTION_FAILED,
                            "Capability completed without an observation",
                            "missing_observation",
                        ),
                    )
                messages.append(
                    ModelMessage(
                        role="tool",
                        tool_result=ToolResult(tool_call_id=call.id, content=observation),
                    )
                )
                completed_signatures.add(signature)
                round_facts.append(
                    f"{signature}:{hashlib.sha256(observation.encode()).hexdigest()}"
                )
            round_fingerprint = hashlib.sha256("|".join(round_facts).encode()).hexdigest()
            if round_fingerprint in completed_rounds:
                return self._limit_outcome(progress, "no_progress")
            completed_rounds.add(round_fingerprint)
            messages.append(ModelMessage(role="system", content=_RESPONSE_CONTRACT_REMINDER))

        return self._limit_outcome(progress, "model_turn_budget")

    async def _invoke_capability(
        self,
        call: ToolCall,
        context: InvocationContext,
        progress: ExecutionProgress,
    ) -> CapabilityResult | AgentOutcome:
        progress.capability_calls += 1
        invocation = asyncio.create_task(
            self._capabilities.invoke(call.name, call.arguments, context)
        )
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

    @staticmethod
    def _validate_turn(turn: ModelTurn) -> ErrorInfo | None:
        if turn.structured_output is not None:
            return FixedGeneralAgent._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Model returned unsupported structured output",
                "unexpected_structured_output",
            )
        if turn.tool_calls and turn.finish_reason != "tool_calls":
            return FixedGeneralAgent._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Model Tool Call finish reason is invalid",
                "invalid_finish_reason",
            )
        if turn.content is not None and turn.finish_reason != "stop":
            return FixedGeneralAgent._error(
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
