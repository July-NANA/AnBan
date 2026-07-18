"""Fixed LangGraph General Agent with one bounded model-Capability loop."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from anban.capability import (
    CapabilityKind,
    CapabilityPort,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import new_capability_invocation_id
from anban.core.metadata import SafeMetadata, safe_text_violation_reason, validate_safe_text
from anban.core.models import now_utc
from anban.model import (
    ModelMessage,
    ModelPort,
    ModelRequest,
    ToolDefinition,
    ToolResult,
)
from anban.runtime.agent_decisions import assessment_guidance, matches_initial_decision
from anban.runtime.agent_execution import AgentExecutionSupport, ExecutionProgress
from anban.runtime.completion import (
    CompletionEvaluation,
    CompletionEvaluator,
    completion_guidance,
    matches_replan_decision,
)
from anban.runtime.contracts import (
    AgentInput,
    AgentLimits,
    AgentObservation,
    AgentOutcome,
    AgentOutcomeStatus,
    CapabilitySufficiencyAssessment,
    CompletionAssessment,
    ExecutionStrategy,
    ObservationStatus,
    ReplanDecision,
)
from anban.runtime.sufficiency import CapabilitySufficiencyEvaluator

GENERAL_AGENT_NODE = "general_agent"
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
    "Tool Calls. An activated Skill, stored context, successful intermediate, or narrated intent "
    "is not by itself completion of the original goal."
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


class AgentGraphState(TypedDict):
    agent_input: AgentInput
    deadline_at: datetime
    progress: ExecutionProgress
    outcome: AgentOutcome | None


class AgentGraphUpdate(TypedDict, total=False):
    outcome: AgentOutcome


class FixedGeneralAgent(AgentExecutionSupport):
    """The only v0.1 Agent graph: START -> General Agent -> END."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        *,
        sufficiency: CapabilitySufficiencyEvaluator | None = None,
        completion: CompletionEvaluator | None = None,
        assessment_observer: Callable[[CapabilitySufficiencyAssessment], Awaitable[None]]
        | None = None,
        observation_observer: Callable[[AgentObservation], Awaitable[None]] | None = None,
        completion_observer: Callable[[CompletionAssessment], Awaitable[None]] | None = None,
        replan_observer: Callable[[ReplanDecision], Awaitable[None]] | None = None,
        limits: AgentLimits | None = None,
        response_repair_retries: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._sufficiency = sufficiency
        self._completion = completion
        self._assessment_observer = assessment_observer
        self._observation_observer = observation_observer
        self._completion_observer = completion_observer
        self._replan_observer = replan_observer
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
        guidance: ModelMessage | None = None
        assessment: CapabilitySufficiencyAssessment | None = None
        initial_skill_targets: frozenset[str] = frozenset()
        if self._sufficiency is not None:
            initial_skill_targets = self._sufficiency.ready_skill_targets()
            if progress.model_turns >= self._limits.max_model_turns:
                return self._limit_outcome(progress, "model_turn_budget")
            progress.model_turns += 1
            assessment = await self._sufficiency.assess(agent_input.request, self._model)
            if self._assessment_observer is not None:
                await self._assessment_observer(assessment)
            if assessment.must_fail:
                await self._record_initial_resolution(assessment, clarify=False)
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.CAPABILITY_UNAVAILABLE,
                        "No sufficient execution strategy is available",
                        "sufficiency_failed",
                    ),
                )
            if assessment.requires_clarification:
                await self._record_initial_resolution(assessment, clarify=True)
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.VALIDATION_FAILED,
                        "Task requires clarification before execution",
                        "clarification_required",
                    ),
                )
            guidance = ModelMessage(
                role="system",
                content=assessment_guidance(assessment),
            )
        acquired_skill_activated = False
        selected_path_disproved = False
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
            *(() if guidance is None else (guidance,)),
            ModelMessage(role="user", content=agent_input.request),
        ]
        last_signature: str | None = None
        repeated_calls = 0
        completed_rounds: set[str] = set()
        observed_signatures: set[str] = set()
        signature_observations: dict[str, AgentObservation] = {}
        repair_attempts_used = 0
        request_is_repair = False
        pending_replan: ReplanDecision | None = None

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
                if (
                    pending_replan is not None
                    and pending_replan.next_strategy is not ExecutionStrategy.DIRECT_ANSWER
                ):
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.MODEL_RESPONSE_INVALID,
                            "Model did not attempt the selected alternative path",
                            "replan_path_not_attempted",
                        ),
                    )
                if (
                    assessment is not None
                    and assessment.sufficient
                    and assessment.selected.strategy is not ExecutionStrategy.DIRECT_ANSWER
                    and progress.capability_calls == 0
                ):
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.MODEL_RESPONSE_INVALID,
                            "Model did not attempt the selected execution path",
                            "selected_path_not_attempted",
                        ),
                    )
                if (
                    assessment is not None
                    and assessment.should_acquire_skill
                    and not acquired_skill_activated
                ):
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.VALIDATION_FAILED,
                            "Skill acquisition did not produce an activated new Skill",
                            "skill_acquisition_incomplete",
                        ),
                    )
                stripped_content = turn.content.strip()
                try:
                    final_text = validate_safe_text(
                        stripped_content,
                        label="Agent final text",
                        max_length=32_768,
                        allow_absolute_paths=True,
                    )
                except ValueError:
                    violation = (
                        safe_text_violation_reason(
                            stripped_content,
                            max_length=32_768,
                            allow_absolute_paths=True,
                        )
                        or "unknown"
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
                if self._completion is None or assessment is None:
                    return self._outcome(
                        AgentOutcomeStatus.SUCCEEDED,
                        progress,
                        final_text=final_text,
                    )
                evaluated = await self._assess_completion(
                    messages,
                    assessment,
                    progress,
                    proposed_final=final_text,
                )
                if isinstance(evaluated, AgentOutcome):
                    return evaluated
                if evaluated.completion.complete:
                    return self._outcome(
                        AgentOutcomeStatus.SUCCEEDED,
                        progress,
                        final_text=evaluated.completion.final_text,
                    )
                if evaluated.replan is None:
                    return self._completion_failure(progress, "completion_resolution_missing")
                terminal = self._replan_terminal_outcome(evaluated.replan, progress)
                if terminal is not None:
                    return terminal
                pending_replan = evaluated.replan
                progress.replans += 1
                messages.append(
                    ModelMessage(
                        role="system",
                        content=completion_guidance(
                            evaluated.completion,
                            evaluated.replan,
                        ),
                    )
                )
                continue

            calls = turn.tool_calls
            following_replan = pending_replan is not None
            if pending_replan is not None:
                if (
                    pending_replan.next_strategy is ExecutionStrategy.DIRECT_ANSWER
                    or not matches_replan_decision(
                        calls[0], pending_replan, self._capabilities.describe
                    )
                ):
                    return self._outcome(
                        AgentOutcomeStatus.FAILED,
                        progress,
                        error=self._error(
                            ErrorCode.MODEL_RESPONSE_INVALID,
                            "Model did not follow the selected alternative path",
                            "replan_strategy_mismatch",
                        ),
                    )
                pending_replan = None
            elif (
                assessment is not None
                and assessment.sufficient
                and progress.capability_calls == 0
                and not matches_initial_decision(calls[0], assessment, self._capabilities.describe)
            ):
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_RESPONSE_INVALID,
                        "Model did not follow the selected initial execution path",
                        "initial_strategy_mismatch",
                    ),
                )
            if (
                assessment is not None
                and assessment.sufficient
                and not selected_path_disproved
                and not following_replan
                and assessment.selected.strategy is not ExecutionStrategy.ACTIVATE_SKILL
                and any(
                    self._capabilities.describe(call.name).kind is CapabilityKind.SKILL
                    for call in calls
                )
            ):
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_RESPONSE_INVALID,
                        "Model attempted unnecessary Skill search after selecting a "
                        "sufficient path",
                        "unnecessary_skill_search",
                    ),
                )
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
            new_signatures = {
                signature for signature in signatures if signature not in observed_signatures
            }
            if progress.capability_calls + len(new_signatures) > self._limits.max_capability_calls:
                return self._limit_outcome(progress, "capability_call_budget")
            if repaired_response and any(
                signature in observed_signatures for signature in signatures
            ):
                return self._outcome(
                    AgentOutcomeStatus.FAILED,
                    progress,
                    error=self._error(
                        ErrorCode.MODEL_RESPONSE_INVALID,
                        "Model repair replayed a previously observed Capability call",
                        "repair_replayed_observed_call",
                    ),
                )
            messages.append(ModelMessage(role="assistant", tool_calls=calls))
            round_facts: list[str] = []
            round_failed = False
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
                if signature in observed_signatures:
                    observation = self._replay_prevented_observation()
                    previous = signature_observations[signature]
                    await self._record_observation(
                        progress,
                        previous.strategy,
                        previous.target,
                        ObservationStatus.FAILED,
                        observation,
                        retry_safe=False,
                        side_effect_completed=previous.side_effect_completed,
                    )
                    messages.append(
                        ModelMessage(
                            role="tool",
                            tool_result=ToolResult(tool_call_id=call.id, content=observation),
                        )
                    )
                    round_facts.append(
                        f"{signature}:{hashlib.sha256(observation.encode()).hexdigest()}"
                    )
                    continue
                context = InvocationContext(
                    run_id=agent_input.run_id,
                    node_run_id=agent_input.node_run_id,
                    invocation_id=new_capability_invocation_id(),
                    deadline_at=deadline_at,
                    metadata=SafeMetadata(
                        {}
                        if agent_input.session_id is None
                        else {"session_id": str(agent_input.session_id)}
                    ),
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
                descriptor = self._capabilities.describe(call.name)
                strategy = self._strategy(descriptor)
                recorded = await self._record_result_observation(
                    progress,
                    strategy,
                    self._observation_target(call, descriptor),
                    descriptor,
                    result,
                    observation,
                )
                round_failed = round_failed or recorded.status is not ObservationStatus.COMPLETED
                if (
                    assessment is not None
                    and assessment.sufficient
                    and matches_initial_decision(call, assessment, self._capabilities.describe)
                    and result.status is not CapabilityResultStatus.COMPLETED
                ):
                    selected_path_disproved = True
                activated_slug = result.metadata.root.get("skill_slug")
                if (
                    assessment is not None
                    and assessment.should_acquire_skill
                    and descriptor.kind is CapabilityKind.SKILL
                    and result.status is CapabilityResultStatus.COMPLETED
                    and isinstance(activated_slug, str)
                    and activated_slug not in initial_skill_targets
                ):
                    acquired_skill_activated = True
                if (
                    descriptor.kind is CapabilityKind.SKILL
                    and result.status is CapabilityResultStatus.COMPLETED
                ):
                    skill_context_chars = progress.skill_context_chars + len(observation)
                    if skill_context_chars > policy.AGENT_SKILL_CONTEXT_MAX_CHARS:
                        return self._limit_outcome(progress, "skill_context_budget")
                    progress.skill_context_chars = skill_context_chars
                messages.append(
                    ModelMessage(
                        role="tool",
                        tool_result=ToolResult(tool_call_id=call.id, content=observation),
                    )
                )
                observed_signatures.add(signature)
                signature_observations[signature] = recorded
                round_facts.append(
                    f"{signature}:{hashlib.sha256(observation.encode()).hexdigest()}"
                )
            round_fingerprint = hashlib.sha256("|".join(round_facts).encode()).hexdigest()
            if round_fingerprint in completed_rounds:
                return self._limit_outcome(progress, "no_progress")
            completed_rounds.add(round_fingerprint)
            if round_failed and self._completion is not None and assessment is not None:
                evaluated = await self._assess_completion(
                    messages,
                    assessment,
                    progress,
                    proposed_final=None,
                )
                if isinstance(evaluated, AgentOutcome):
                    return evaluated
                if evaluated.replan is None:
                    return self._completion_failure(progress, "failure_resolution_missing")
                terminal = self._replan_terminal_outcome(evaluated.replan, progress)
                if terminal is not None:
                    return terminal
                pending_replan = evaluated.replan
                progress.replans += 1
                messages.append(
                    ModelMessage(
                        role="system",
                        content=completion_guidance(
                            evaluated.completion,
                            evaluated.replan,
                        ),
                    )
                )
                continue
            messages.append(ModelMessage(role="system", content=_RESPONSE_CONTRACT_REMINDER))

        return self._limit_outcome(progress, "model_turn_budget")

    async def _record_initial_resolution(
        self,
        assessment: CapabilitySufficiencyAssessment,
        *,
        clarify: bool,
    ) -> None:
        if self._replan_observer is None:
            return
        await self._replan_observer(
            ReplanDecision(
                should_replan=False,
                rationale=assessment.rationale,
                remaining_attempts=self._limits.max_replans,
                requires_clarification=clarify,
                must_fail=not clarify,
            )
        )

    async def _assess_completion(
        self,
        messages: list[ModelMessage],
        assessment: CapabilitySufficiencyAssessment,
        progress: ExecutionProgress,
        *,
        proposed_final: str | None,
    ) -> CompletionEvaluation | AgentOutcome:
        if self._completion is None:
            return self._completion_failure(progress, "completion_evaluator_missing")
        if progress.model_turns >= self._limits.max_model_turns:
            return self._limit_outcome(progress, "model_turn_budget")
        progress.model_turns += 1
        try:
            evaluated = await self._completion.assess(
                transcript=tuple(messages),
                assessment=assessment,
                observations=tuple(progress.observations),
                proposed_final=proposed_final,
                remaining_replans=max(0, self._limits.max_replans - progress.replans),
                model=self._model,
            )
        except AnbanError as exc:
            return self._failure_outcome(exc.info, progress)
        except Exception:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.MODEL_REQUEST_FAILED,
                    "Completion assessment failed",
                    "completion_exception",
                ),
            )
        if self._completion_observer is not None:
            await self._completion_observer(evaluated.completion)
        if evaluated.replan is not None and self._replan_observer is not None:
            await self._replan_observer(evaluated.replan)
        return evaluated

    def _replan_terminal_outcome(
        self,
        replan: ReplanDecision,
        progress: ExecutionProgress,
    ) -> AgentOutcome | None:
        if replan.should_replan:
            return None
        if replan.requires_clarification:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.VALIDATION_FAILED,
                    "Task requires clarification before completion",
                    "completion_clarification_required",
                ),
            )
        if replan.must_fail:
            return self._outcome(
                AgentOutcomeStatus.FAILED,
                progress,
                error=self._error(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "No safe alternative can complete the task",
                    "completion_failed",
                ),
            )
        return self._completion_failure(progress, "completion_resolution_invalid")

    def _completion_failure(self, progress: ExecutionProgress, reason: str) -> AgentOutcome:
        return self._outcome(
            AgentOutcomeStatus.FAILED,
            progress,
            error=self._error(
                ErrorCode.MODEL_RESPONSE_INVALID,
                "Completion assessment did not produce a valid resolution",
                reason,
            ),
        )
