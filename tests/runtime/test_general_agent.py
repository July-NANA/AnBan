"""Deterministic tests for the real fixed LangGraph execution path."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_execution_run_id, new_node_run_id
from anban.core.metadata import SafeMetadata
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import (
    AgentInput,
    AgentLimits,
    AgentOutcomeStatus,
    FixedGeneralAgent,
)


def constant_observation(count: int) -> str:
    return "observed"


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn | Exception]) -> None:
        self.turns = turns
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        if not self.turns:
            raise RuntimeError("unexpected model request")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class RecordingHandler:
    def __init__(
        self,
        *,
        result: CapabilityResult | None = None,
        observation: Callable[[int], str] | None = None,
        blocking: bool = False,
        capability_name: str = "file.read",
        argument_name: str = "path",
    ) -> None:
        self.descriptor = CapabilityDescriptor(
            name=capability_name,
            description="Read one bounded file.",
            input_schema={
                "type": "object",
                "properties": {argument_name: {"type": "string", "minLength": 1, "maxLength": 512}},
                "required": [argument_name],
                "additionalProperties": False,
            },
        )
        self.result = result
        self.observation: Callable[[int], str] = observation or constant_observation
        self.blocking = blocking
        self.calls: list[tuple[dict[str, JsonValue], InvocationContext]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        self.calls.append((arguments, context))
        self.started.set()
        if self.blocking:
            await self.release.wait()
        if self.result is not None:
            return self.result
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=self.observation(len(self.calls)),
        )

    async def cancel(self, context: InvocationContext) -> None:
        assert self.calls[-1][1] == context
        self.cancelled = True
        self.release.set()


def agent_input() -> AgentInput:
    return AgentInput(
        request="Read the bounded result and answer.",
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
    )


def call(identifier: str = "call-1", path: str = "result.txt") -> ToolCall:
    return ToolCall(id=identifier, name="file.read", arguments={"path": path})


def tool_turn(tool_call: ToolCall, *, finish_reason: str = "tool_calls") -> ModelTurn:
    return ModelTurn(tool_calls=(tool_call,), finish_reason=finish_reason)


def final_turn(content: str = "Final bounded answer.", *, finish_reason: str = "stop") -> ModelTurn:
    return ModelTurn(content=content, finish_reason=finish_reason)


def invalid_response(*, repairable: bool = True) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="model response shape is invalid",
            details=SafeMetadata(
                {
                    "diagnostic_reason": "empty_response",
                    "repairable": repairable,
                }
            ),
        )
    )


async def test_graph_topology_is_fixed_start_agent_end() -> None:
    agent = FixedGeneralAgent(ScriptedModel([final_turn()]), CapabilityRegistry())
    assert agent.graph_edges() == (
        ("__start__", "general_agent"),
        ("general_agent", "__end__"),
    )


async def test_valid_model_final_is_the_only_success_path() -> None:
    model = ScriptedModel([final_turn()])
    outcome = await FixedGeneralAgent(model, CapabilityRegistry()).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.final_text == "Final bounded answer."
    assert outcome.model_turn_count == 1
    assert outcome.capability_call_count == 0
    system_contract = model.requests[0].messages[0].content or ""
    assert "no non-whitespace assistant content" in system_contract
    assert "no tool_calls" in system_contract


async def test_tool_call_result_pairing_across_model_turns() -> None:
    model = ScriptedModel([tool_turn(call()), final_turn()])
    handler = RecordingHandler()
    registry = CapabilityRegistry((handler,))
    execution_input = agent_input()

    outcome = await FixedGeneralAgent(model, registry).execute(execution_input)

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.model_turn_count == 2
    assert outcome.capability_call_count == 1
    assert len(model.requests) == 2
    paired = model.requests[1].messages[-1].tool_result
    assert paired is not None
    assert paired.tool_call_id == "call-1"
    assert paired.content == "observed"
    _, invocation_context = handler.calls[0]
    assert invocation_context.run_id == execution_input.run_id
    assert invocation_context.node_run_id == execution_input.node_run_id


@pytest.mark.parametrize(
    ("tool_call", "expected"),
    [
        (ToolCall(id="unknown", name="unknown.tool", arguments={}), ErrorCode.CAPABILITY_UNKNOWN),
        (
            ToolCall(id="invalid", name="file.read", arguments={}),
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
        ),
    ],
)
async def test_unknown_or_invalid_tool_call_fails_closed(
    tool_call: ToolCall, expected: ErrorCode
) -> None:
    registry = CapabilityRegistry((RecordingHandler(),))
    outcome = await FixedGeneralAgent(ScriptedModel([tool_turn(tool_call)]), registry).execute(
        agent_input()
    )
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is expected


async def test_capability_failure_is_terminal_without_another_model_request() -> None:
    failure = CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        error=ErrorInfo(
            code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
            message="Capability execution failed",
        ),
    )
    model = ScriptedModel([tool_turn(call()), final_turn("must not run")])
    outcome = await FixedGeneralAgent(
        model, CapabilityRegistry((RecordingHandler(result=failure),))
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.CAPABILITY_EXECUTION_FAILED
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (
            AnbanError(ErrorInfo(code=ErrorCode.MODEL_REQUEST_FAILED, message="Model failed")),
            AgentOutcomeStatus.FAILED,
        ),
        (
            AnbanError(ErrorInfo(code=ErrorCode.MODEL_TIMEOUT, message="Model timed out")),
            AgentOutcomeStatus.TIMED_OUT,
        ),
        (RuntimeError("raw-provider-canary"), AgentOutcomeStatus.FAILED),
    ],
)
async def test_model_failures_are_safe_terminal_outcomes(
    failure: Exception, status: AgentOutcomeStatus
) -> None:
    outcome = await FixedGeneralAgent(ScriptedModel([failure]), CapabilityRegistry()).execute(
        agent_input()
    )
    assert outcome.status is status
    assert outcome.error is not None
    assert "raw-provider-canary" not in str(outcome.model_dump(mode="json"))


@pytest.mark.parametrize(
    "turn",
    [
        tool_turn(call(), finish_reason="stop"),
        final_turn(finish_reason="length"),
        ModelTurn(structured_output={"answer": "unexpected"}, finish_reason="stop"),
        final_turn("/private/host/result.txt"),
    ],
)
async def test_invalid_or_unsafe_model_turn_never_succeeds(turn: ModelTurn) -> None:
    outcome = await FixedGeneralAgent(
        ScriptedModel([turn]), CapabilityRegistry((RecordingHandler(),))
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.MODEL_RESPONSE_INVALID


async def test_duplicate_tool_call_identifiers_fail_before_execution() -> None:
    duplicate = ModelTurn(
        tool_calls=(call("same", "one.txt"), call("same", "two.txt")),
        finish_reason="tool_calls",
    )
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(
        ScriptedModel([duplicate]), CapabilityRegistry((handler,))
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert handler.calls == []


async def test_capability_budget_is_checked_before_a_tool_batch() -> None:
    batch = ModelTurn(
        tool_calls=(call("one", "one.txt"), call("two", "two.txt")),
        finish_reason="tool_calls",
    )
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(
        ScriptedModel([batch]),
        CapabilityRegistry((handler,)),
        limits=AgentLimits(max_capability_calls=1),
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "capability_call_budget"
    assert handler.calls == []


async def test_repeated_call_is_stopped_before_third_side_effect() -> None:
    model = ScriptedModel(
        [tool_turn(call("one")), tool_turn(call("two")), tool_turn(call("three"))]
    )
    handler = RecordingHandler(observation=lambda count: f"observation-{count}")
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "repeated_call"
    assert len(handler.calls) == 2


async def test_repeated_call_and_observation_detects_no_progress() -> None:
    model = ScriptedModel([tool_turn(call("one")), tool_turn(call("two"))])
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "no_progress"
    assert len(handler.calls) == 2


async def test_model_turn_budget_is_terminal() -> None:
    model = ScriptedModel([tool_turn(call("one", "one.txt")), tool_turn(call("two", "two.txt"))])
    outcome = await FixedGeneralAgent(
        model,
        CapabilityRegistry((RecordingHandler(),)),
        limits=AgentLimits(max_model_turns=2),
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "model_turn_budget"


async def test_total_timeout_cancels_active_capability() -> None:
    handler = RecordingHandler(blocking=True)
    outcome = await FixedGeneralAgent(
        ScriptedModel([tool_turn(call())]),
        CapabilityRegistry((handler,)),
        limits=AgentLimits(total_timeout_seconds=1),
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.TIMED_OUT
    assert handler.cancelled


async def test_external_interruption_cancels_active_capability() -> None:
    handler = RecordingHandler(blocking=True)
    agent = FixedGeneralAgent(ScriptedModel([tool_turn(call())]), CapabilityRegistry((handler,)))
    execution = asyncio.create_task(agent.execute(agent_input()))
    await handler.started.wait()
    execution.cancel()
    outcome = await execution
    assert outcome.status is AgentOutcomeStatus.CANCELLED
    assert handler.cancelled


def test_limits_cannot_exceed_v01_bounds() -> None:
    with pytest.raises(ValueError):
        AgentLimits(max_model_turns=9)
    with pytest.raises(ValueError):
        AgentLimits(max_capability_calls=9)
    with pytest.raises(ValueError):
        AgentLimits(total_timeout_seconds=181)


@pytest.mark.parametrize(
    ("invalid_count", "expected_turns"),
    [(1, 2), (2, 3), (3, 4)],
)
async def test_response_repair_succeeds_within_shared_budget(
    invalid_count: int, expected_turns: int
) -> None:
    turns: list[ModelTurn | Exception] = []
    turns.extend(invalid_response() for _ in range(invalid_count))
    turns.append(final_turn())
    model = ScriptedModel(turns)
    outcome = await FixedGeneralAgent(model, CapabilityRegistry()).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.model_turn_count == expected_turns
    assert [request.repair_attempt for request in model.requests] == list(range(0, expected_turns))
    repair_messages = [
        message.content
        for request in model.requests[1:]
        for message in request.messages
        if message.role == "system" and message.content and "violated" in message.content
    ]
    assert repair_messages
    assert all("Tool Call" in message for message in repair_messages)


async def test_original_and_three_repairs_fail_closed_after_four_requests() -> None:
    model = ScriptedModel([invalid_response() for _ in range(4)])
    outcome = await FixedGeneralAgent(model, CapabilityRegistry()).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert outcome.model_turn_count == 4
    assert [request.repair_attempt for request in model.requests] == [0, 1, 2, 3]


async def test_nonrepairable_invalid_response_never_retries_or_invokes() -> None:
    model = ScriptedModel([invalid_response(repairable=False), final_turn("must not run")])
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert len(model.requests) == 1
    assert handler.calls == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.MODEL_TRANSPORT_FAILED, ErrorCode.MODEL_TIMEOUT, ErrorCode.MODEL_REJECTED],
)
async def test_transport_or_http_failure_never_triggers_structure_repair(code: ErrorCode) -> None:
    model = ScriptedModel(
        [AnbanError(ErrorInfo(code=code, message="Model request failed")), final_turn()]
    )
    outcome = await FixedGeneralAgent(model, CapabilityRegistry()).execute(agent_input())
    assert outcome.status is not AgentOutcomeStatus.SUCCEEDED
    assert len(model.requests) == 1
    assert model.requests[0].repair_attempt == 0


async def test_repair_budget_is_node_shared_across_separate_invalid_responses() -> None:
    model = ScriptedModel([invalid_response(), tool_turn(call()), invalid_response(), final_turn()])
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert [request.repair_attempt for request in model.requests] == [0, 1, 0, 2]
    assert len(handler.calls) == 1


async def test_repair_cannot_replay_completed_skill_activation() -> None:
    activation_call = ToolCall(
        id="activate-1",
        name="skill.activate",
        arguments={"name": "@steipete/weather"},
    )
    replay_call = activation_call.model_copy(update={"id": "activate-2"})
    model = ScriptedModel([tool_turn(activation_call), invalid_response(), tool_turn(replay_call)])
    handler = RecordingHandler(
        capability_name="skill.activate",
        argument_name="name",
        observation=lambda count: "activated",
    )
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert outcome.error.details.root["reason"] == "repair_replayed_completed_call"
    assert len(handler.calls) == 1


async def test_invalid_response_never_executes_a_capability() -> None:
    model = ScriptedModel([invalid_response(), final_turn()])
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert handler.calls == []


async def test_repair_request_is_bounded_by_total_timeout() -> None:
    class BlockingRepairModel:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelTurn:
            self.requests.append(request)
            if len(self.requests) == 1:
                raise invalid_response()
            await asyncio.Event().wait()
            return final_turn("unreachable")

    model = BlockingRepairModel()
    outcome = await FixedGeneralAgent(
        model,
        CapabilityRegistry(),
        limits=AgentLimits(total_timeout_seconds=1),
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.TIMED_OUT
    assert [request.repair_attempt for request in model.requests] == [0, 1]
