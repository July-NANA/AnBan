"""Truthful completion assessment, alternative paths, and finite replanning."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
    UnifiedCapabilityInventory,
)
from anban.core import AnbanError, ErrorCode, ErrorInfo, new_execution_run_id, new_node_run_id
from anban.core.metadata import SafeMetadata
from anban.model import ModelMessage, ModelRequest, ModelTurn, ToolCall, ToolResult
from anban.runtime import (
    AgentInput,
    AgentLimits,
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    CompletionAssessment,
    CompletionEvaluator,
    ExecutionStrategy,
    FixedGeneralAgent,
    ReplanDecision,
)
from tests.runtime.test_persistent_runtime import assessment_turn, completion_turn, final_turn


class ScriptedCompletionModel:
    def __init__(self, turns: list[ModelTurn | Exception]) -> None:
        self.turns = turns
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class BoundedProcess:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.descriptor = CapabilityDescriptor(
            name=name,
            description="Perform one dynamically named bounded process step.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            inventory_kind=InventoryKind.PROCESS,
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        self.calls += 1
        if self.fail:
            error = ErrorInfo(
                code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                message="The bounded process path failed",
            )
            return CapabilityResult(
                status=CapabilityResultStatus.FAILED,
                observation='{"status":"failed","reason":"bounded_failure"}',
                error=error,
            )
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation='{"status":"completed","evidence":"bounded"}',
        )

    async def cancel(self, context: InvocationContext) -> None:
        return None


def process_turn(name: str, value: str) -> ModelTurn:
    return ModelTurn(
        tool_calls=(
            ToolCall(
                id=f"call-{uuid4().hex}",
                name=name,
                arguments={"value": value},
            ),
        ),
        finish_reason="tool_calls",
    )


def agent_input(goal: str) -> AgentInput:
    return AgentInput(
        request=goal,
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
    )


@pytest.mark.parametrize(
    ("goal", "premature", "final_text"),
    [
        (
            "Produce a verified bounded transformation.",
            "I can probably transform it.",
            "The bounded transformation completed with real evidence.",
        ),
        (
            "完成一个经过验证的有界转换。",
            "我准备执行这个转换。",
            "有界转换已经由真实执行证据完成。",
        ),
        (
            "Apply a changed requirement using an actual execution path.",
            "The requirement is understood.",
            "The changed requirement was applied through the real path.",
        ),
    ],
)
async def test_premature_final_replans_to_exact_ready_path_and_completes(
    goal: str,
    premature: str,
    final_text: str,
) -> None:
    process_name = f"fixture.{uuid4().hex}"
    process = BoundedProcess(process_name)
    registry = CapabilityRegistry((process,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    completions: list[CompletionAssessment] = []
    replans: list[ReplanDecision] = []

    async def completion_observer(value: CompletionAssessment) -> None:
        completions.append(value)

    async def replan_observer(value: ReplanDecision) -> None:
        replans.append(value)

    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn(premature),
            completion_turn(
                resolution="replan",
                unmet_condition="A real bounded execution result is still required.",
                next_strategy=ExecutionStrategy.USE_PROCESS.value,
                next_target=process_name,
            ),
            process_turn(process_name, uuid4().hex),
            final_turn("The real operation returned evidence."),
            completion_turn(final_text=final_text),
        ]
    )
    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        completion_observer=completion_observer,
        replan_observer=replan_observer,
        limits=AgentLimits(max_replans=1),
    ).execute(agent_input(goal))

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.final_text == final_text
    assert process.calls == 1
    assert [item.complete for item in completions] == [False, True]
    assert [(item.next_strategy, item.next_target) for item in replans] == [
        (ExecutionStrategy.USE_PROCESS, process_name)
    ]
    assert replans[0].remaining_attempts == 1
    assert model.requests[2].response_schema is not None
    assert model.requests[5].response_schema is not None


async def test_completion_shape_failures_use_shared_bounded_repair_budget() -> None:
    registry = CapabilityRegistry()
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    provider_invalid = AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="Model response shape is invalid",
            details=SafeMetadata({"repairable": True}),
        )
    )
    final_text = "The bounded response is supported by the supplied evidence."
    unsafe_domain_decision = completion_turn(final_text=final_text).model_copy(
        update={
            "structured_output": {
                **(completion_turn(final_text=final_text).structured_output or {}),
                "rationale": "Authorization: Bearer unsafe-test-canary",
            }
        }
    )
    path_rationale_decision = completion_turn(final_text=final_text).model_copy(
        update={
            "structured_output": {
                **(completion_turn(final_text=final_text).structured_output or {}),
                "rationale": "Evidence was produced under /private/host/workspace/output.txt.",
            }
        }
    )
    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn(final_text),
            provider_invalid,
            unsafe_domain_decision,
            path_rationale_decision,
        ]
    )

    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        response_repair_retries=2,
    ).execute(agent_input("Give one bounded evidence-based response."))

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.model_turn_count == 5
    assert [request.repair_attempt for request in model.requests] == [0, 0, 0, 1, 2]
    assert all(request.repair_limit == 2 for request in model.requests[2:])
    assert all(
        any(
            message.content is not None
            and "previous completion assessment" in message.content
            and "absolute host paths" in message.content
            for message in request.messages
        )
        for request in model.requests[3:]
    )


async def test_sufficiency_shape_failure_uses_shared_bounded_repair_budget() -> None:
    registry = CapabilityRegistry()
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    provider_invalid = AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="Model response shape is invalid",
            details=SafeMetadata({"repairable": True}),
        )
    )
    final_text = "The finite response completed from the supplied information."
    model = ScriptedCompletionModel(
        [
            provider_invalid,
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn(final_text),
            completion_turn(final_text=final_text),
        ]
    )

    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        response_repair_retries=1,
    ).execute(agent_input("Explain one finite bounded fact."))

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.model_turn_count == 4
    assert [request.repair_attempt for request in model.requests] == [0, 1, 0, 0]
    assert any(
        message.content is not None and "previous sufficiency response" in message.content
        for message in model.requests[1].messages
    )


async def test_unexecuted_initial_strategy_mismatch_is_repaired_to_selected_path() -> None:
    selected_name = f"fixture.{uuid4().hex}"
    other_name = f"fixture.{uuid4().hex}"
    selected = BoundedProcess(selected_name)
    other = BoundedProcess(other_name)
    registry = CapabilityRegistry((selected, other))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    final_text = "The selected bounded path completed with real evidence."
    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.USE_PROCESS, target=selected_name),
            process_turn(other_name, uuid4().hex),
            process_turn(selected_name, uuid4().hex),
            final_turn(final_text),
            completion_turn(final_text=final_text),
        ]
    )

    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        response_repair_retries=1,
    ).execute(agent_input("Run the selected bounded process path."))

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert selected.calls == 1
    assert other.calls == 0
    assert [request.repair_attempt for request in model.requests] == [0, 0, 1, 0, 0]
    assert any(
        message.content is not None and "authoritative initial path" in message.content
        for message in model.requests[2].messages
    )


async def test_exhausted_alternative_fails_without_retrying_side_effect() -> None:
    process_name = f"fixture.{uuid4().hex}"
    process = BoundedProcess(process_name, fail=True)
    registry = CapabilityRegistry((process,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn("The task should be complete soon."),
            completion_turn(
                resolution="replan",
                unmet_condition="One real execution attempt is required.",
                next_strategy=ExecutionStrategy.USE_PROCESS.value,
                next_target=process_name,
            ),
            process_turn(process_name, uuid4().hex),
            completion_turn(
                resolution="fail",
                unmet_condition="The only safe ready path failed and no budget remains.",
            ),
        ]
    )
    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        limits=AgentLimits(max_replans=1),
    ).execute(agent_input(f"Complete bounded task {uuid4().hex}."))

    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "completion_failed"
    assert process.calls == 1
    assert outcome.capability_call_count == 1
    assert model.turns == []


async def test_incomplete_goal_can_request_clarification_without_execution() -> None:
    process_name = f"fixture.{uuid4().hex}"
    process = BoundedProcess(process_name)
    registry = CapabilityRegistry((process,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn("I selected an unspecified destination."),
            completion_turn(
                resolution="clarify",
                unmet_condition="The user must supply the destination identity.",
            ),
        ]
    )
    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
    ).execute(agent_input("Apply the result to my destination."))

    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "completion_clarification_required"
    assert process.calls == 0


async def test_model_cannot_substitute_an_unselected_ready_alternative() -> None:
    selected_name = f"fixture.{uuid4().hex}"
    substituted_name = f"fixture.{uuid4().hex}"
    selected = BoundedProcess(selected_name)
    substituted = BoundedProcess(substituted_name)
    registry = CapabilityRegistry((selected, substituted))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    model = ScriptedCompletionModel(
        [
            assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target=""),
            final_turn("A real path has not run yet."),
            completion_turn(
                resolution="replan",
                unmet_condition="The selected real path must run.",
                next_strategy=ExecutionStrategy.USE_PROCESS.value,
                next_target=selected_name,
            ),
            process_turn(substituted_name, uuid4().hex),
        ]
    )
    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
        completion=CompletionEvaluator(),
        limits=AgentLimits(max_replans=1),
    ).execute(agent_input("Run one explicitly selected bounded alternative."))

    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "replan_strategy_mismatch"
    assert selected.calls == 0
    assert substituted.calls == 0


async def test_completion_evaluator_rejects_unavailable_or_unproven_resolution() -> None:
    process_name = f"fixture.{uuid4().hex}"
    process = BoundedProcess(process_name)
    registry = CapabilityRegistry((process,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    evaluator = CapabilitySufficiencyEvaluator(inventory)
    assessment_model = ScriptedCompletionModel(
        [assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target="")]
    )
    assessment = await evaluator.assess("Evaluate a bounded goal.", assessment_model)
    unavailable = ScriptedCompletionModel(
        [
            completion_turn(
                resolution="replan",
                unmet_condition="Another path is required.",
                next_strategy=ExecutionStrategy.USE_PROCESS.value,
                next_target=f"missing.{uuid4().hex}",
            )
        ]
    )
    with pytest.raises(AnbanError) as failure:
        await CompletionEvaluator().assess(
            transcript=(
                ModelMessage(role="system", content="Use only real bounded evidence."),
                ModelMessage(role="user", content="Evaluate a bounded goal."),
            ),
            assessment=assessment,
            observations=(),
            proposed_final="A candidate answer.",
            remaining_replans=1,
            model=unavailable,
        )
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert failure.value.info.details.root["reason"] == "replan_path_unavailable"


async def test_completion_transcript_bound_preserves_complete_tool_exchanges() -> None:
    process_name = f"fixture.{uuid4().hex}"
    process = BoundedProcess(process_name)
    registry = CapabilityRegistry((process,))
    inventory = UnifiedCapabilityInventory(registry, model_available=True)
    assessment_model = ScriptedCompletionModel(
        [assessment_turn(ExecutionStrategy.DIRECT_ANSWER, target="")]
    )
    assessment = await CapabilitySufficiencyEvaluator(inventory).assess(
        "Evaluate a long bounded transcript.", assessment_model
    )
    messages: list[ModelMessage] = [
        ModelMessage(role="system", content="Use governed evidence."),
        ModelMessage(role="user", content="Evaluate a long bounded transcript."),
    ]
    for index in range(20):
        call_id = f"call-{index}-{uuid4().hex}"
        argument_value = "a" * 10_000 if index == 19 else str(index)
        result_content = (
            "start-marker" + ("b" * 60_000) + "end-marker"
            if index == 19
            else '{"status":"completed"}'
        )
        messages.extend(
            (
                ModelMessage(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            id=call_id,
                            name=process_name,
                            arguments={"value": argument_value},
                        ),
                    ),
                ),
                ModelMessage(
                    role="tool",
                    tool_result=ToolResult(
                        tool_call_id=call_id,
                        content=result_content,
                    ),
                ),
                ModelMessage(role="system", content="Continue bounded reasoning."),
            )
        )
    completion_model = ScriptedCompletionModel(
        [completion_turn(final_text="The bounded transcript was assessed.")]
    )

    result = await CompletionEvaluator().assess(
        transcript=tuple(messages),
        assessment=assessment,
        observations=(),
        proposed_final="The bounded transcript was assessed.",
        remaining_replans=1,
        model=completion_model,
    )

    assert result.completion.complete
    request = completion_model.requests[0]
    assert len(request.messages) <= 64
    assert request.response_schema is not None
    assert request.tools == ()
    assert not any(message.tool_calls or message.role == "tool" for message in request.messages)
    evidence_messages = tuple(
        message
        for message in request.messages
        if message.content is not None and message.content.startswith("HISTORICAL_TOOL_EVIDENCE")
    )
    assert evidence_messages
    assert all(message.role == "user" for message in evidence_messages)
    last_evidence = evidence_messages[-1].content
    assert last_evidence is not None
    assert len(last_evidence) <= 32_768
    assert process_name in last_evidence
    assert "HISTORICAL_TOOL_EVIDENCE" in last_evidence
    assert "bounded evidence omitted" in last_evidence
    assert "start-marker" in last_evidence
    assert "end-marker" in last_evidence
