"""Deterministic tests for the real fixed LangGraph execution path."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
    UnifiedCapabilityInventory,
    WorkspaceSkillCatalog,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_execution_run_id, new_node_run_id
from anban.core.metadata import SafeMetadata
from anban.model import ModelRequest, ModelTurn, ToolCall
from anban.runtime import (
    AgentInput,
    AgentLimits,
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionStrategy,
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
        capability_name: str = "test.action",
        argument_name: str = "path",
        capability_kind: CapabilityKind = CapabilityKind.TOOL,
    ) -> None:
        self.descriptor = CapabilityDescriptor(
            name=capability_name,
            description="Read one bounded file.",
            kind=capability_kind,
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
    return ToolCall(id=identifier, name="test.action", arguments={"path": path})


def tool_turn(tool_call: ToolCall, *, finish_reason: str = "tool_calls") -> ModelTurn:
    return ModelTurn(tool_calls=(tool_call,), finish_reason=finish_reason)


def final_turn(content: str = "Final bounded answer.", *, finish_reason: str = "stop") -> ModelTurn:
    return ModelTurn(content=content, finish_reason=finish_reason)


def assessment_turn(
    strategy: ExecutionStrategy,
    *,
    target: str = "",
    missing_condition: str = "",
) -> ModelTurn:
    return ModelTurn(
        structured_output={
            "strategy": strategy.value,
            "target": target,
            "rationale": "The bounded inventory supports this initial strategy.",
            "confidence": 0.84,
            "missing_condition": missing_condition,
            "substantial_temporary_code": False,
            "complex_domain_workflow": False,
            "high_improvisation_risk": False,
            "low_implementation_confidence": False,
            "repeated_reusable_need": False,
            "existing_process_path_unreasonable": False,
        },
        finish_reason="stop",
    )


def skill_catalog(tmp_path: Path) -> WorkspaceSkillCatalog:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace = tmp_path / "workspace"
    for _ in range(2):
        name = f"skill-{uuid4().hex[:12]}"
        root = workspace / "skills" / "@fixture" / name
        root.mkdir(parents=True)
        root.joinpath("SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use dynamic instructions.\n---\n",
            encoding="utf-8",
        )
    return WorkspaceSkillCatalog(workspace, package_skills_root=package_root)


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
    assert "Narrated actions are not evidence of execution" in system_contract
    assert "text accompanying valid Tool Calls" in system_contract
    assert "final answer must not contain Tool Calls" in system_contract
    assert "Use process.execute" in system_contract
    assert "file operations, network operations" in system_contract


async def test_sufficiency_assessment_guides_the_same_fixed_loop() -> None:
    handler = RecordingHandler()
    registry = CapabilityRegistry((handler,))
    model = ScriptedModel(
        [
            assessment_turn(ExecutionStrategy.USE_CAPABILITY, target="test.action"),
            tool_turn(call()),
            final_turn(),
        ]
    )
    agent = FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(
            UnifiedCapabilityInventory(registry, model_available=True)
        ),
    )

    outcome = await agent.execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.model_turn_count == 3
    assert outcome.capability_call_count == 1
    guidance = model.requests[1].messages[1].content or ""
    assert "strategy=use_capability" in guidance
    assert "target=test.action" in guidance
    assert agent.graph_edges() == (
        ("__start__", "general_agent"),
        ("general_agent", "__end__"),
    )


@pytest.mark.parametrize(
    ("strategy", "code"),
    [
        (ExecutionStrategy.CLARIFY, ErrorCode.VALIDATION_FAILED),
        (ExecutionStrategy.FAIL, ErrorCode.CAPABILITY_UNAVAILABLE),
    ],
)
async def test_terminal_sufficiency_resolution_never_executes(
    strategy: ExecutionStrategy,
    code: ErrorCode,
) -> None:
    handler = RecordingHandler()
    registry = CapabilityRegistry((handler,))
    model = ScriptedModel(
        [
            assessment_turn(
                strategy,
                missing_condition="A required execution condition is unavailable.",
            )
        ]
    )
    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(
            UnifiedCapabilityInventory(registry, model_available=True)
        ),
    ).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is code
    assert handler.calls == []
    assert outcome.model_turn_count == 1


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.CAPABILITY_EXECUTION_FAILED,
        ErrorCode.CAPABILITY_UNAVAILABLE,
    ],
)
async def test_nonterminal_observation_can_select_a_distinct_capability_path(
    code: ErrorCode,
) -> None:
    first_name, second_name = (f"fixture.{uuid4().hex}" for _ in range(2))
    failure = CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        observation='{"status":"failed","reason":"bounded_failure"}',
        error=ErrorInfo(
            code=code,
            message="Capability path did not complete",
        ),
    )
    first = RecordingHandler(result=failure, capability_name=first_name)
    second = RecordingHandler(capability_name=second_name)
    registry = CapabilityRegistry((first, second))
    model = ScriptedModel(
        [
            assessment_turn(ExecutionStrategy.USE_CAPABILITY, target=first_name),
            tool_turn(ToolCall(id="first", name=first_name, arguments={"path": "one"})),
            tool_turn(ToolCall(id="second", name=second_name, arguments={"path": "two"})),
            final_turn("Alternative path completed."),
        ]
    )

    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(
            UnifiedCapabilityInventory(registry, model_available=True)
        ),
    ).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.capability_call_count == 2
    assert len(first.calls) == len(second.calls) == 1
    assert any(
        message.tool_result is not None and "bounded_failure" in message.tool_result.content
        for message in model.requests[2].messages
    )


async def test_one_run_can_activate_multiple_dynamic_skills(tmp_path: Path) -> None:
    catalog = skill_catalog(tmp_path)
    first, second = catalog.refresh()
    handler = RecordingHandler(
        capability_name="skill.activate",
        argument_name="name",
        capability_kind=CapabilityKind.SKILL,
        observation=lambda count: f"activated-{count}",
    )
    registry = CapabilityRegistry((handler,))
    model = ScriptedModel(
        [
            assessment_turn(ExecutionStrategy.ACTIVATE_SKILL, target=first.slug),
            tool_turn(
                ToolCall(id="first-skill", name="skill.activate", arguments={"name": first.slug})
            ),
            tool_turn(
                ToolCall(id="second-skill", name="skill.activate", arguments={"name": second.slug})
            ),
            final_turn("Both dynamic Skill instructions were applied."),
        ]
    )

    outcome = await FixedGeneralAgent(
        model,
        registry,
        sufficiency=CapabilitySufficiencyEvaluator(
            UnifiedCapabilityInventory(
                registry,
                catalog,
                model_available=True,
            )
        ),
    ).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.capability_call_count == 2
    assert [call[0]["name"] for call in handler.calls] == [first.slug, second.slug]
    composed = [
        message.tool_result.content
        for message in model.requests[-1].messages
        if message.tool_result is not None
    ]
    assert composed == ["activated-1", "activated-2"]


async def test_multi_skill_context_fails_closed_at_the_shared_hard_limit() -> None:
    payload = "x" * (policy.AGENT_SKILL_CONTEXT_MAX_CHARS // 5)
    handler = RecordingHandler(
        capability_kind=CapabilityKind.SKILL,
        observation=lambda count: f"skill-{count}:{payload}",
    )
    turns: list[ModelTurn | Exception] = [
        tool_turn(call(f"call-{index}", f"skill-{index}")) for index in range(1, 6)
    ]

    outcome = await FixedGeneralAgent(
        ScriptedModel(turns),
        CapabilityRegistry((handler,)),
    ).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "skill_context_budget"
    assert outcome.capability_call_count == 5
    assert len(handler.calls) == 5


async def test_user_visible_final_may_report_an_absolute_result_path() -> None:
    final_text = "Installed result at /private/workspace/skills/example/SKILL.md."
    outcome = await FixedGeneralAgent(
        ScriptedModel([final_turn(final_text)]), CapabilityRegistry()
    ).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.final_text == final_text


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
    paired = next(
        message.tool_result
        for message in reversed(model.requests[1].messages)
        if message.role == "tool"
    )
    assert paired is not None
    assert paired.tool_call_id == "call-1"
    assert paired.content == "observed"
    assert "Response contract reminder" in (model.requests[1].messages[-1].content or "")
    _, invocation_context = handler.calls[0]
    assert invocation_context.run_id == execution_input.run_id
    assert invocation_context.node_run_id == execution_input.node_run_id


@pytest.mark.parametrize(
    ("tool_call", "expected"),
    [
        (ToolCall(id="unknown", name="unknown.tool", arguments={}), ErrorCode.CAPABILITY_UNKNOWN),
        (
            ToolCall(id="invalid", name="test.action", arguments={}),
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


async def test_explainable_capability_failure_is_returned_to_model_for_recovery() -> None:
    failure = CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        observation='{"status":"failed","exit_code":1}',
        error=ErrorInfo(
            code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
            message="Capability execution failed",
        ),
    )
    model = ScriptedModel([tool_turn(call()), final_turn("The failed command was handled.")])
    handler = RecordingHandler(result=failure)

    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())

    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.capability_call_count == 1
    assert len(handler.calls) == 1
    assert len(model.requests) == 2
    result_message = next(
        message for message in model.requests[1].messages if message.role == "tool"
    )
    assert result_message.tool_result is not None
    assert result_message.tool_result.content == failure.observation


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
        final_turn("Bearer canary-value"),
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


async def test_repeated_call_is_stopped_without_replaying_the_side_effect() -> None:
    model = ScriptedModel(
        [tool_turn(call("one")), tool_turn(call("two")), tool_turn(call("three"))]
    )
    handler = RecordingHandler(observation=lambda count: f"observation-{count}")
    outcome = await FixedGeneralAgent(model, CapabilityRegistry((handler,))).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.details.root["reason"] == "repeated_call"
    assert len(handler.calls) == 1


async def test_prevented_replay_is_observed_and_allows_a_distinct_next_step() -> None:
    model = ScriptedModel(
        [tool_turn(call("one")), tool_turn(call("two")), final_turn("Recovered without replay.")]
    )
    handler = RecordingHandler()
    outcome = await FixedGeneralAgent(
        model,
        CapabilityRegistry((handler,)),
        limits=AgentLimits(max_capability_calls=1),
    ).execute(agent_input())
    assert outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert outcome.final_text == "Recovered without replay."
    assert len(handler.calls) == 1
    prevented = next(
        message.tool_result
        for message in model.requests[2].messages
        if message.role == "tool"
        and message.tool_result is not None
        and message.tool_result.tool_call_id == "two"
    )
    assert prevented is not None
    assert "completed_call_replay_prevented" in prevented.content


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
        AgentLimits(max_model_turns=25)
    with pytest.raises(ValueError):
        AgentLimits(max_capability_calls=33)
    with pytest.raises(ValueError):
        AgentLimits(total_timeout_seconds=1801)
    with pytest.raises(ValueError):
        AgentLimits(repeated_call_limit=1)
    assert AgentLimits(repeated_call_limit=0).repeated_call_limit == 0


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


async def test_repair_cannot_replay_observed_skill_activation() -> None:
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
    assert outcome.error.details.root["reason"] == "repair_replayed_observed_call"
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
