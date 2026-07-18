"""Persistent synchronous execution of one routed TaskGraphSpec."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from pydantic import JsonValue, TypeAdapter, ValidationError

from anban.capability import ArtifactReference, CapabilityPort, InvocationContext
from anban.core import ErrorCode, ErrorInfo, SafeMetadata, TaskGraphNode, TaskGraphSpec
from anban.core.ids import new_node_run_id
from anban.core.models import NodeRun
from anban.runtime.agent import FixedGeneralAgent
from anban.runtime.capability_persistence import PersistedCapabilityPort
from anban.runtime.completion import CompletionEvaluator
from anban.runtime.contracts import (
    AgentInput,
    AgentLimits,
    AgentOutcome,
    AgentOutcomeStatus,
)
from anban.runtime.graph_execution import (
    TaskGraphExecutionError,
    TaskGraphExecutionFailureReason,
    TaskGraphExecutor,
)
from anban.runtime.model_persistence import PersistedModelPort
from anban.runtime.persistence import RunPersistence
from anban.runtime.sufficiency import CapabilitySufficiencyEvaluator

_MAX_NODE_REQUEST_CHARS = 32_768
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class PersistentGraphTaskRunner:
    """Bind generic graph actions to the existing real Agent and durable Run path."""

    def __init__(
        self,
        model: PersistedModelPort,
        capabilities: CapabilityPort,
        persistence: RunPersistence,
        graph_executor: TaskGraphExecutor,
        *,
        sufficiency: CapabilitySufficiencyEvaluator | None,
        limits: AgentLimits | None,
        response_repair_retries: int,
        artifact_cleanup: Callable[[InvocationContext, ArtifactReference], None] | None,
        metadata: SafeMetadata,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._persistence = persistence
        self._graph_executor = graph_executor
        self._sufficiency = sufficiency
        self._limits = limits
        self._response_repair_retries = response_repair_retries
        self._artifact_cleanup = artifact_cleanup
        self._metadata = metadata
        self._action_lock = asyncio.Lock()
        self._outcomes: list[AgentOutcome] = []

    async def execute(
        self,
        spec: TaskGraphSpec,
        graph_input: dict[str, JsonValue],
        *,
        routing_model_turns: int,
    ) -> AgentOutcome:
        """Execute the graph and reduce real node outcomes into one Run outcome."""

        try:
            result = await self._graph_executor.execute(
                spec,
                graph_input,
                action_executor=self._execute_action,
            )
            artifacts = self._artifacts()
            if len(artifacts) > 32:
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID)
            final_text = self._final_text(result.outputs)
            return AgentOutcome(
                status=AgentOutcomeStatus.SUCCEEDED,
                final_text=final_text,
                model_turn_count=routing_model_turns
                + sum(outcome.model_turn_count for outcome in self._outcomes),
                capability_call_count=sum(
                    outcome.capability_call_count for outcome in self._outcomes
                ),
                artifacts=artifacts,
            )
        except TaskGraphExecutionError as exc:
            return self._failure_outcome(exc.reason, routing_model_turns)

    async def _execute_action(
        self,
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        async with self._action_lock:
            node_run = NodeRun(
                id=new_node_run_id(),
                run_id=self._persistence.run.id,
                node_name=node.id,
                metadata=SafeMetadata(
                    {
                        **self._metadata.root,
                        "graph_node_id": node.id,
                        "graph_node_kind": node.kind.value,
                    }
                ),
            )
            await self._persistence.add_node(node_run)
            await self._persistence.start_node()
            request = self._node_request(node, node_input)
            if request is None:
                outcome = self._node_contract_failure(TaskGraphExecutionFailureReason.INPUT_INVALID)
                await self._persistence.finish_node(outcome)
                self._outcomes.append(outcome)
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.INPUT_INVALID)
            agent = FixedGeneralAgent(
                self._model,
                PersistedCapabilityPort(
                    self._capabilities,
                    self._persistence,
                    artifact_cleanup=self._artifact_cleanup,
                ),
                sufficiency=self._sufficiency,
                completion=(CompletionEvaluator() if self._sufficiency is not None else None),
                assessment_observer=self._persistence.agent_sufficiency_assessed,
                observation_observer=self._persistence.agent_observed,
                completion_observer=self._persistence.agent_completion_assessed,
                replan_observer=self._persistence.agent_replan_decided,
                limits=self._limits,
                response_repair_retries=self._response_repair_retries,
            )
            outcome = await agent.execute(
                AgentInput(
                    request=request,
                    run_id=self._persistence.run.id,
                    node_run_id=node_run.id,
                )
            )
            if outcome.status is not AgentOutcomeStatus.SUCCEEDED:
                await self._persistence.finish_node(outcome)
                self._outcomes.append(outcome)
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ACTION_FAILED)
            output = self._parse_node_output(outcome.final_text)
            if output is None:
                failure = self._node_contract_failure(
                    TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID,
                    source=outcome,
                )
                await self._persistence.finish_node(failure)
                self._outcomes.append(failure)
                raise TaskGraphExecutionError(TaskGraphExecutionFailureReason.ACTION_OUTPUT_INVALID)
            await self._persistence.finish_node(outcome)
            self._outcomes.append(outcome)
            return output

    @staticmethod
    def _node_request(
        node: TaskGraphNode,
        node_input: dict[str, JsonValue],
    ) -> str | None:
        output_names = json.dumps(node.outputs, ensure_ascii=False, separators=(",", ":"))
        input_json = json.dumps(node_input, ensure_ascii=False, separators=(",", ":"))
        request = (
            "Execute this bounded Task graph action using only real available paths.\n"
            f"Objective: {node.objective}\n"
            f"Declared inputs: {input_json}\n"
            "Return the completed action result as one JSON object and no surrounding prose. "
            f"The object must contain exactly these output keys: {output_names}."
        )
        return request if len(request) <= _MAX_NODE_REQUEST_CHARS else None

    @staticmethod
    def _parse_node_output(final_text: str | None) -> dict[str, JsonValue] | None:
        if final_text is None:
            return None
        try:
            parsed: object = json.loads(final_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        try:
            return _JSON_OBJECT.validate_python(parsed, strict=True)
        except ValidationError:
            return None

    @staticmethod
    def _final_text(outputs: dict[str, JsonValue]) -> str:
        if len(outputs) == 1:
            value = next(iter(outputs.values()))
            if isinstance(value, str):
                return value
        return json.dumps(outputs, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _artifacts(self) -> tuple[ArtifactReference, ...]:
        return tuple(artifact for outcome in self._outcomes for artifact in outcome.artifacts)

    def _failure_outcome(
        self,
        reason: TaskGraphExecutionFailureReason,
        routing_model_turns: int,
    ) -> AgentOutcome:
        timed_out = reason in {
            TaskGraphExecutionFailureReason.NODE_TIMED_OUT,
            TaskGraphExecutionFailureReason.GRAPH_TIMED_OUT,
        }
        return AgentOutcome(
            status=(AgentOutcomeStatus.TIMED_OUT if timed_out else AgentOutcomeStatus.FAILED),
            error=ErrorInfo(
                code=(ErrorCode.EXECUTION_TIMED_OUT if timed_out else ErrorCode.VALIDATION_FAILED),
                message="Task graph execution failed",
                details=SafeMetadata({"reason": reason.value}),
            ),
            model_turn_count=routing_model_turns
            + sum(outcome.model_turn_count for outcome in self._outcomes),
            capability_call_count=sum(outcome.capability_call_count for outcome in self._outcomes),
            artifacts=self._artifacts()[:32],
        )

    @staticmethod
    def _node_contract_failure(
        reason: TaskGraphExecutionFailureReason,
        *,
        source: AgentOutcome | None = None,
    ) -> AgentOutcome:
        return AgentOutcome(
            status=AgentOutcomeStatus.FAILED,
            error=ErrorInfo(
                code=ErrorCode.MODEL_RESPONSE_INVALID,
                message="Task graph action result was invalid",
                details=SafeMetadata({"reason": reason.value}),
            ),
            model_turn_count=0 if source is None else source.model_turn_count,
            capability_call_count=0 if source is None else source.capability_call_count,
            artifacts=() if source is None else source.artifacts,
        )
